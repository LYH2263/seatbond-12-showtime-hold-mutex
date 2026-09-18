import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db, get_immediate_db
from app.models.models import ConflictLog, Hall, SeatHold, Showtime
from app.schemas.schemas import (
    ConflictOut,
    HallOut,
    HoldOut,
    HoldRequest,
    SeatMapCell,
    SeatMapOut,
    ShowtimeOut,
)
from app.services.bond_engine import (
    HoldSpan,
    SeatCell,
    conflicts_with,
    find_bond_across_rows,
    find_contiguous_block,
)

api_router = APIRouter()


def _aisles(hall: Hall) -> list[int]:
    if not hall.aisle_cols.strip():
        return []
    return [int(x) for x in hall.aisle_cols.split(",") if x.strip()]


def _hall_out(h: Hall) -> HallOut:
    return HallOut(id=h.id, name=h.name, rows=h.rows, cols=h.cols, aisle_cols=_aisles(h))


def _new_request_code() -> str:
    stamp = int(datetime.utcnow().timestamp()) % 100000
    return f"SB-{stamp:05d}-{uuid.uuid4().hex[:4]}"


def _seats_by_row(hall: Hall, aisles: set[int]) -> dict[int, list[SeatCell]]:
    return {
        r: [SeatCell(row=r, col=c, is_aisle=c in aisles) for c in range(1, hall.cols + 1)]
        for r in range(1, hall.rows + 1)
    }


def _choose_block(
    hall: Hall, aisles: set[int], holds: list[HoldSpan], party_size: int, preferred_row: int | None
) -> HoldSpan | None:
    seats_by_row = _seats_by_row(hall, aisles)
    block = None
    if preferred_row:
        block = find_contiguous_block(
            seats_by_row.get(preferred_row, []), holds, preferred_row, party_size
        )
    if block is None:
        block = find_bond_across_rows(seats_by_row, holds, party_size)
    return block


def _conflict_detail(
    *,
    code: str,
    message: str,
    request_code: str,
    showtime_id: int,
    party_size: int,
    block: HoldSpan | None,
    blocking_order_code: str | None = None,
) -> dict:
    return {
        "code": code,
        "message": message,
        "request_code": request_code,
        "showtime_id": showtime_id,
        "party_size": party_size,
        "requested": (
            None
            if block is None
            else {"row": block.row, "start_col": block.start_col, "end_col": block.end_col}
        ),
        "blocking_order_code": blocking_order_code,
    }


@api_router.get("/health")
def health():
    return {"status": "ok"}


@api_router.get("/halls", response_model=list[HallOut])
def list_halls(db: Session = Depends(get_db)):
    return [_hall_out(h) for h in db.scalars(select(Hall).order_by(Hall.id)).all()]


@api_router.get("/showtimes", response_model=list[ShowtimeOut])
def list_showtimes(db: Session = Depends(get_db)):
    rows = db.scalars(select(Showtime).order_by(Showtime.start_at)).all()
    out = []
    for s in rows:
        hall = db.get(Hall, s.hall_id)
        out.append(
            ShowtimeOut(
                id=s.id,
                hall_id=s.hall_id,
                film_title=s.film_title,
                start_at=s.start_at,
                hall_name=hall.name if hall else None,
            )
        )
    return out


@api_router.get("/seatmap/{showtime_id}", response_model=SeatMapOut)
def seatmap(showtime_id: int, db: Session = Depends(get_db)):
    st = db.get(Showtime, showtime_id)
    if not st:
        raise HTTPException(404, "场次不存在")
    hall = db.get(Hall, st.hall_id)
    assert hall
    aisles = set(_aisles(hall))
    holds = db.scalars(select(SeatHold).where(SeatHold.showtime_id == showtime_id)).all()
    occupied: set[tuple[int, int]] = set()
    for h in holds:
        for c in range(h.start_col, h.end_col + 1):
            occupied.add((h.row, c))
    cells: list[SeatMapCell] = []
    for r in range(1, hall.rows + 1):
        for c in range(1, hall.cols + 1):
            occ = (r, c) in occupied
            cells.append(
                SeatMapCell(
                    row=r,
                    col=c,
                    is_aisle=c in aisles,
                    occupied=occ,
                    heat=1.0 if occ else (0.15 if c in aisles else 0.0),
                )
            )
    return SeatMapOut(
        showtime_id=showtime_id,
        hall_name=hall.name,
        rows=hall.rows,
        cols=hall.cols,
        cells=cells,
    )


@api_router.get("/holds", response_model=list[HoldOut])
def list_holds(db: Session = Depends(get_db)):
    return db.scalars(select(SeatHold).order_by(SeatHold.id.desc())).all()


@api_router.get("/conflicts", response_model=list[ConflictOut])
def list_conflicts(db: Session = Depends(get_db)):
    logs = db.scalars(select(ConflictLog).order_by(ConflictLog.id.desc())).all()
    show_ids = {c.showtime_id for c in logs}
    titles = (
        {
            s.id: s.film_title
            for s in db.scalars(select(Showtime).where(Showtime.id.in_(show_ids))).all()
        }
        if show_ids
        else {}
    )
    return [
        ConflictOut(
            id=c.id,
            showtime_id=c.showtime_id,
            party_size=c.party_size,
            reason=c.reason,
            request_code=c.request_code,
            requested_row=c.requested_row,
            requested_start_col=c.requested_start_col,
            requested_end_col=c.requested_end_col,
            blocking_order_code=c.blocking_order_code,
            film_title=titles.get(c.showtime_id),
            created_at=c.created_at,
        )
        for c in logs
    ]


@api_router.post("/holds", response_model=HoldOut)
def create_hold(
    body: HoldRequest,
    snap_db: Session = Depends(get_db),
    db: Session = Depends(get_immediate_db),
):
    """两笔同场次请求若算出的占用区间重叠，保证至多一笔落库。

    分两段事务：
    1. 快照段（短只读）：按当时可见持座算出本笔想要的固定区间 candidate；
    2. 临界段（Postgres FOR UPDATE 行锁 / SQLite BEGIN IMMEDIATE）：串行进入，
       锁内重读持座，只对固定 candidate 做重叠校验——重叠即拒并写冲突日志，
       绝不重新选座挪到别处，否则后到者会变成第二笔成功。区间不重叠才落库。
    """
    request_code = _new_request_code()

    # ---- 快照段：算候选区间（不持锁，尽快结束读事务）----
    st = snap_db.get(Showtime, body.showtime_id)
    if not st:
        raise HTTPException(404, "场次不存在")
    hall = snap_db.get(Hall, st.hall_id)
    assert hall
    aisles = set(_aisles(hall))
    snapshot_holds = [
        HoldSpan(row=h.row, start_col=h.start_col, end_col=h.end_col)
        for h in snap_db.scalars(
            select(SeatHold).where(SeatHold.showtime_id == body.showtime_id)
        ).all()
    ]
    candidate = _choose_block(hall, aisles, snapshot_holds, body.party_size, body.preferred_row)
    snap_db.rollback()  # 释放快照读锁，临界段使用独立会话

    if candidate is None:
        reason = f"无足够连续空座（人数 {body.party_size}）"
        db.add(
            ConflictLog(
                showtime_id=body.showtime_id,
                party_size=body.party_size,
                reason=reason,
                request_code=request_code,
            )
        )
        db.commit()
        raise HTTPException(
            status_code=409,
            detail=_conflict_detail(
                code="NO_CONTIGUOUS_SEATS",
                message=reason,
                request_code=request_code,
                showtime_id=body.showtime_id,
                party_size=body.party_size,
                block=None,
                blocking_order_code=None,
            ),
        )

    # ---- 临界段：同场次串行，锁内只校验固定 candidate ----
    lock_stmt = select(Showtime).where(Showtime.id == body.showtime_id)
    if db.get_bind().dialect.name != "sqlite":
        lock_stmt = lock_stmt.with_for_update()  # 同场次并发请求在此行锁上排队
    locked_st = db.scalars(lock_stmt).first()
    if not locked_st:
        db.rollback()
        raise HTTPException(404, "场次不存在")

    fresh = db.scalars(select(SeatHold).where(SeatHold.showtime_id == body.showtime_id)).all()
    fresh_spans = [HoldSpan(row=h.row, start_col=h.start_col, end_col=h.end_col) for h in fresh]
    hits = conflicts_with(fresh_spans, candidate)

    if hits:
        winner = hits[0]
        winner_hold = next(
            (h for h in fresh if h.row == winner.row and h.start_col == winner.start_col),
            None,
        )
        blocking_code = winner_hold.order_code if winner_hold else None
        reason = (
            f"座位冲突：请求第{candidate.row}排 {candidate.start_col}-{candidate.end_col} 座，"
            f"已被持座 {blocking_code or '（另一笔请求）'} 占用的"
            f"第{winner.row}排 {winner.start_col}-{winner.end_col} 座重叠"
        )
        db.add(
            ConflictLog(
                showtime_id=body.showtime_id,
                party_size=body.party_size,
                reason=reason,
                request_code=request_code,
                requested_row=candidate.row,
                requested_start_col=candidate.start_col,
                requested_end_col=candidate.end_col,
                blocking_order_code=blocking_code,
            )
        )
        db.commit()  # 先落冲突日志、释放行锁，再返回失败
        raise HTTPException(
            status_code=409,
            detail=_conflict_detail(
                code="SEAT_CONFLICT",
                message=reason,
                request_code=request_code,
                showtime_id=body.showtime_id,
                party_size=body.party_size,
                block=candidate,
                blocking_order_code=blocking_code,
            ),
        )

    hold = SeatHold(
        showtime_id=body.showtime_id,
        order_code=request_code,
        row=candidate.row,
        start_col=candidate.start_col,
        end_col=candidate.end_col,
        party_size=body.party_size,
    )
    db.add(hold)
    try:
        db.commit()
    except Exception:
        # 唯一约束等并发兜底：宁失败不双成功，补写冲突日志
        db.rollback()
        _log_blocked(
            showtime_id=body.showtime_id,
            party_size=body.party_size,
            request_code=request_code,
            block=candidate,
        )
        raise HTTPException(
            status_code=409,
            detail=_conflict_detail(
                code="SEAT_CONFLICT",
                message=(
                    f"座位冲突：第{candidate.row}排 {candidate.start_col}-"
                    f"{candidate.end_col} 座刚被其他请求锁定，请改选"
                ),
                request_code=request_code,
                showtime_id=body.showtime_id,
                party_size=body.party_size,
                block=candidate,
                blocking_order_code=None,
            ),
        )
    db.refresh(hold)
    return hold


def _log_blocked(
    *, showtime_id: int, party_size: int, request_code: str, block: HoldSpan
) -> None:
    from app.database import ImmediateSessionLocal

    db = ImmediateSessionLocal()
    try:
        db.add(
            ConflictLog(
                showtime_id=showtime_id,
                party_size=party_size,
                reason=f"并发抢座失败：第{block.row}排 {block.start_col}-{block.end_col} 座已被锁定",
                request_code=request_code,
                requested_row=block.row,
                requested_start_col=block.start_col,
                requested_end_col=block.end_col,
            )
        )
        db.commit()
    finally:
        db.close()
