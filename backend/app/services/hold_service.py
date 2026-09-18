"""Transactional hold placement.

Mutual exclusion strategy
-------------------------
A request first computes the contiguous block it *wants* from a quick
preview snapshot (two concurrent requests therefore compute the same
leftmost block). It then opens a transaction and takes a row-level lock
on the ``showtimes`` row (``SELECT ... FOR UPDATE``). Under that lock it
re-reads every existing hold and checks the wanted block against them:

* winner (no overlap)  -> insert, commit, return the hold;
* loser  (overlap)     -> insert a ``conflict_logs`` row carrying the
                          refused span, commit THAT, and raise a 409.

Concurrent requests for one showtime serialize on the row lock, so the
loser always sees the winner's committed hold and can never double-book.
The ``uq_hold_span`` unique constraint is a further backstop; any
``IntegrityError`` is logged and rejected the same way.

After a conflict the client retries: the preview/search then reflects
only the winner's occupancy and the request is offered the next free
block ("自动搜索只体现成功方占用").
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.models import ConflictLog, Hall, SeatHold, Showtime
from app.services.bond_engine import (
    HoldSpan,
    SeatCell,
    conflicts_with,
    find_bond_across_rows,
    find_contiguous_block,
)

# Max wait on the showtime row lock. Critical sections here are
# sub-millisecond, so this only trips under pathological stalls.
LOCK_TIMEOUT_MS = 3000

# Test seam: invoked once per request after the candidate span has been
# computed from the preview snapshot and before the showtime lock is
# taken. Concurrency tests point it at a threading barrier so every
# contender computes from the same snapshot (deterministic race); it is
# never set in production.
pre_lock_hook: "object | None" = None


class ShowtimeNotFound(Exception):
    """Raised when the target showtime does not exist (maps to 404)."""

    def __init__(self, showtime_id: int):
        super().__init__(f"场次不存在: {showtime_id}")
        self.showtime_id = showtime_id


@dataclass
class SeatUnavailable(Exception):
    """A hold attempt was rejected; the conflict row is already committed.

    Maps to HTTP 409 with a structured, human-readable payload so the UI
    can tell a seat conflict apart from a network failure.
    """

    code: str  # "overlap" | "no_seats"
    message: str
    showtime_id: int
    party_size: int
    conflict_id: int
    span: HoldSpan | None = None

    @property
    def detail(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "showtime_id": self.showtime_id,
            "party_size": self.party_size,
            "conflict_id": self.conflict_id,
            "span": None
            if self.span is None
            else {
                "row": self.span.row,
                "start_col": self.span.start_col,
                "end_col": self.span.end_col,
            },
        }


def _aisles(hall: Hall) -> set[int]:
    if not hall.aisle_cols.strip():
        return set()
    return {int(x) for x in hall.aisle_cols.split(",") if x.strip()}


def _order_code() -> str:
    return f"SB-{int(time.time() * 1000) % 100000000:08d}{random.randint(0, 99):02d}"


def _load_halls(db: Session, showtime_id: int) -> tuple[Showtime | None, Hall | None]:
    st = db.get(Showtime, showtime_id)
    hall = db.get(Hall, st.hall_id) if st else None
    return st, hall


def _holds_as_spans(rows: list[SeatHold]) -> list[HoldSpan]:
    return [HoldSpan(row=h.row, start_col=h.start_col, end_col=h.end_col) for h in rows]


def _compute_block(
    hall: Hall,
    holds: list[HoldSpan],
    party_size: int,
    preferred_row: int | None,
) -> HoldSpan | None:
    aisles = _aisles(hall)
    seats_by_row: dict[int, list[SeatCell]] = {
        r: [SeatCell(row=r, col=c, is_aisle=c in aisles) for c in range(1, hall.cols + 1)]
        for r in range(1, hall.rows + 1)
    }
    if preferred_row:
        block = find_contiguous_block(
            seats_by_row.get(preferred_row, []), holds, preferred_row, party_size
        )
        if block is not None:
            return block
    return find_bond_across_rows(seats_by_row, holds, party_size)


def _log_conflict(
    db: Session,
    *,
    showtime_id: int,
    party_size: int,
    kind: str,
    reason: str,
    span: HoldSpan | None,
) -> int:
    entry = ConflictLog(
        showtime_id=showtime_id,
        party_size=party_size,
        kind=kind,
        reason=reason,
        row=span.row if span else None,
        start_col=span.start_col if span else None,
        end_col=span.end_col if span else None,
    )
    db.add(entry)
    db.flush()  # populate entry.id while still in the transaction
    return entry.id


def _overlap_reason(block: HoldSpan, existing: list[SeatHold], hits: list[HoldSpan]) -> str:
    hit = hits[0]
    winner = next(
        (h for h in existing if h.row == hit.row and h.start_col == hit.start_col and h.end_col == hit.end_col),
        None,
    )
    who = f"订单 {winner.order_code}" if winner else "另一笔并发请求"
    return (
        f"并发锁座冲突：意向第{block.row}排 {block.start_col}-{block.end_col} 座"
        f"与{who}已锁定的第{hit.row}排 {hit.start_col}-{hit.end_col} 座重叠"
    )


def place_hold(
    db: Session,
    *,
    showtime_id: int,
    party_size: int,
    preferred_row: int | None = None,
) -> SeatHold:
    """Place a hold with showtime-level mutual exclusion.

    Raises ``ShowtimeNotFound`` (404) or committed ``SeatUnavailable``
    (409 + conflict log). On success the SeatHold is committed.
    """
    # --- Phase 1: preview (no transaction held) — the "offer" to claim. ---
    st, hall = _load_halls(db, showtime_id)
    if st is None:
        raise ShowtimeNotFound(showtime_id)
    assert hall is not None
    preview = db.scalars(select(SeatHold).where(SeatHold.showtime_id == showtime_id)).all()
    candidate = _compute_block(hall, _holds_as_spans(preview), party_size, preferred_row)
    db.rollback()  # end the implicit read transaction cleanly

    if pre_lock_hook is not None:  # pragma: no cover - exercised by concurrency tests
        pre_lock_hook()

    unavailable: SeatUnavailable | None = None
    winner: SeatHold | None = None

    try:
        with db.begin():
            # Bound how long we queue behind another holder of this showtime.
            db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))

            # --- Phase 2: serialize every writer on the showtime row. ---
            locked = db.execute(
                select(Showtime).where(Showtime.id == showtime_id).with_for_update()
            ).scalar_one_or_none()
            if locked is None:
                raise ShowtimeNotFound(showtime_id)

            # Fresh snapshot AFTER acquiring the lock: queued requests see
            # the winner's committed hold.
            existing = db.scalars(
                select(SeatHold).where(SeatHold.showtime_id == showtime_id)
            ).all()
            fresh = _holds_as_spans(existing)

            if candidate is not None:
                hits = conflicts_with(fresh, candidate)
                if hits:
                    reason = _overlap_reason(candidate, existing, hits)
                    conflict_id = _log_conflict(
                        db,
                        showtime_id=showtime_id,
                        party_size=party_size,
                        kind="overlap",
                        reason=reason,
                        span=candidate,
                    )
                    unavailable = SeatUnavailable(
                        code="overlap",
                        message=(
                            f"锁座冲突：第{candidate.row}排 {candidate.start_col}-{candidate.end_col} 座"
                            "刚被并发请求锁定，请重试以改选其他座位（非网络问题）"
                        ),
                        showtime_id=showtime_id,
                        party_size=party_size,
                        conflict_id=conflict_id,
                        span=candidate,
                    )
                else:
                    winner = SeatHold(
                        showtime_id=showtime_id,
                        order_code=_order_code(),
                        row=candidate.row,
                        start_col=candidate.start_col,
                        end_col=candidate.end_col,
                        party_size=party_size,
                    )
                    db.add(winner)
                    db.flush()
            else:
                # Preview saw no fitting block; double-check under the lock
                # (cheap and defensive — seats are only ever added).
                rechecked = _compute_block(hall, fresh, party_size, preferred_row)
                if rechecked is None:
                    conflict_id = _log_conflict(
                        db,
                        showtime_id=showtime_id,
                        party_size=party_size,
                        kind="no_seats",
                        reason=f"无足够连续空座（{party_size} 人，过道列断开连座）",
                        span=None,
                    )
                    unavailable = SeatUnavailable(
                        code="no_seats",
                        message=f"锁座失败：该场次没有 {party_size} 个连续空座（非网络问题）",
                        showtime_id=showtime_id,
                        party_size=party_size,
                        conflict_id=conflict_id,
                    )
                else:
                    winner = SeatHold(
                        showtime_id=showtime_id,
                        order_code=_order_code(),
                        row=rechecked.row,
                        start_col=rechecked.start_col,
                        end_col=rechecked.end_col,
                        party_size=party_size,
                    )
                    db.add(winner)
                    db.flush()
        # with-block exits here: exactly one of conflict log / hold commits.
    except ShowtimeNotFound:
        raise
    except IntegrityError:
        # Unique-constraint backstop (uq_hold_span): an identical span got
        # inserted by a concurrent winner. Record and reject identically.
        db.rollback()
        raise _fallback_conflict(
            db,
            showtime_id,
            party_size,
            "唯一约束拦截：相同座位区间已被并发请求锁定",
            "锁座冲突：所选座位刚被他人锁定，请改选其他座位（非网络问题）",
        )
    except OperationalError as exc:  # lock_timeout / deadlock victim
        db.rollback()
        raise _fallback_conflict(
            db,
            showtime_id,
            party_size,
            f"并发抢座等待场次锁超时（{LOCK_TIMEOUT_MS}ms）：{exc.orig.__class__.__name__}",
            "锁座冲突：同时抢座人数过多，请稍后重试（非网络问题）",
        )

    if unavailable is not None:
        raise unavailable
    assert winner is not None
    db.refresh(winner)
    return winner


def _fallback_conflict(
    db: Session, showtime_id: int, party_size: int, reason: str, message: str
) -> SeatUnavailable:
    # The aborted transaction cannot write; persist the rejection in a
    # fresh transaction so failed attempts stay observable.
    with db.begin():
        conflict_id = _log_conflict(
            db,
            showtime_id=showtime_id,
            party_size=party_size,
            kind="overlap",
            reason=reason,
            span=None,
        )
    return SeatUnavailable(
        code="overlap",
        message=message,
        showtime_id=showtime_id,
        party_size=party_size,
        conflict_id=conflict_id,
    )
