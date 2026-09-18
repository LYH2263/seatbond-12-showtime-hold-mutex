"""Concurrency tests for showtime-scoped hold mutual exclusion.

These hit a real PostgreSQL: a threading.Barrier (installed through the
``pre_lock_hook`` test seam) makes N contenders compute the same wanted
span from one snapshot and then pile onto the showtime row lock
simultaneously — a deterministic version of the production race.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models.models import ConflictLog, Hall, SeatHold, Showtime
from app.services import hold_service
from app.services.hold_service import SeatUnavailable, place_hold


# ---------------------------------------------------------------- helpers

def _make_showtime(rows=4, cols=10, aisle_cols="5", film="并发测试片"):
    db = SessionLocal()
    try:
        hall = Hall(name=f"厅-{threading.get_ident()}-{datetime.utcnow().timestamp()}",
                    rows=rows, cols=cols, aisle_cols=aisle_cols)
        db.add(hall)
        db.flush()
        st = Showtime(hall_id=hall.id, film_title=film,
                      start_at=datetime.utcnow() + timedelta(hours=2))
        db.add(st)
        db.commit()
        return st.id
    finally:
        db.close()


def _count(table, showtime_id):
    db = SessionLocal()
    try:
        return db.scalar(
            select(func.count()).select_from(table).where(table.showtime_id == showtime_id)
        )
    finally:
        db.close()


def _all_holds(showtime_id):
    db = SessionLocal()
    try:
        return [
            (h.row, h.start_col, h.end_col)
            for h in db.scalars(
                select(SeatHold).where(SeatHold.showtime_id == showtime_id)
            ).all()
        ]
    finally:
        db.close()


def _contend(showtime_id, n, party=3, preferred_row=None):
    """Fire n place_hold calls that race onto one showtime lock."""
    barrier = threading.Barrier(n)
    hold_service.pre_lock_hook = barrier.wait

    def one(_i):
        db = SessionLocal()
        try:
            return ("ok", place_hold(
                db, showtime_id=showtime_id, party_size=party,
                preferred_row=preferred_row,
            ))
        except SeatUnavailable as exc:
            return ("conflict", exc)
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected
            return ("error", exc)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=n) as pool:
        try:
            return list(pool.map(one, range(n)))
        finally:
            hold_service.pre_lock_hook = None


# ----------------------------------------------------------- service tests

def test_concurrent_same_span_one_winner_one_loser():
    sid = _make_showtime()
    results = _contend(sid, n=2, party=3)

    winners = [r for r in results if r[0] == "ok"]
    losers = [r for r in results if r[0] == "conflict"]
    assert len(winners) == 1, results
    assert len(losers) == 1, results

    hold = winners[0][1]
    # Hall: aisle at col 5, empty -> leftmost block is row 1, cols 1-3.
    assert (hold.row, hold.start_col, hold.end_col) == (1, 1, 3)

    rejected = losers[0][1]
    assert rejected.code == "overlap"
    assert rejected.span == hold_service.HoldSpan(row=1, start_col=1, end_col=3)
    assert "非网络问题" in rejected.message


@pytest.mark.parametrize("n", [4, 8])
def test_n_racers_exactly_one_wins_no_bloat(n):
    sid = _make_showtime()
    results = _contend(sid, n=n, party=4)

    assert sum(1 for r in results if r[0] == "ok") == 1
    assert sum(1 for r in results if r[0] == "conflict") == n - 1
    assert all(r[0] != "error" for r in results), results

    # Persistent state: one hold, N-1 conflict rows — nothing inflated.
    assert _count(SeatHold, sid) == 1
    assert _count(ConflictLog, sid) == n - 1


def test_loser_conflict_rows_record_showtime_party_and_span():
    sid = _make_showtime()
    _contend(sid, n=3, party=2)

    db = SessionLocal()
    try:
        logs = db.scalars(select(ConflictLog).where(ConflictLog.showtime_id == sid)).all()
        assert len(logs) == 2
        for log in logs:
            assert log.kind == "overlap"
            assert log.party_size == 2
            assert (log.row, log.start_col, log.end_col) == (1, 1, 2)
            assert "重叠" in log.reason
    finally:
        db.close()


def test_race_repeated_rounds_never_double_books():
    """Stress: 20 tight rounds; each round must yield exactly one hold."""
    for _ in range(20):
        sid = _make_showtime()
        results = _contend(sid, n=3, party=2)
        assert sum(1 for r in results if r[0] == "ok") == 1, results
        assert _count(SeatHold, sid) == 1
        assert _count(ConflictLog, sid) == 2


def test_retry_after_conflict_lands_on_next_free_block():
    """The loser's retry must see only the winner's occupancy and move on."""
    sid = _make_showtime()  # aisle col 5 -> runs [1-4], [6-10]
    results = _contend(sid, n=2, party=3)
    assert sum(1 for r in results if r[0] == "ok") == 1

    # Fresh request after the dust settles: cols 1-3 taken, the next
    # fitting block is row 1 cols 6-8 (aisle 5 breaks the run).
    db = SessionLocal()
    try:
        follow = place_hold(db, showtime_id=sid, party_size=3)
        assert (follow.row, follow.start_col, follow.end_col) == (1, 6, 8)
    finally:
        db.close()
    assert _count(SeatHold, sid) == 2


def test_non_overlapping_spans_both_succeed():
    """Different preferred rows compute disjoint spans -> both win."""
    sid = _make_showtime()

    def one(pref):
        db = SessionLocal()
        try:
            return place_hold(db, showtime_id=sid, party_size=3, preferred_row=pref)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        h1, h2 = pool.map(one, [1, 2])
    assert (h1.row, h1.start_col, h1.end_col) == (1, 1, 3)
    assert (h2.row, h2.start_col, h2.end_col) == (2, 1, 3)
    assert _count(SeatHold, sid) == 2
    assert _count(ConflictLog, sid) == 0


def test_different_showtimes_do_not_block_each_other():
    s1 = _make_showtime()
    s2 = _make_showtime()
    barrier = threading.Barrier(2)
    hold_service.pre_lock_hook = barrier.wait

    def one(sid):
        db = SessionLocal()
        try:
            return place_hold(db, showtime_id=sid, party_size=3)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        h1, h2 = list(pool.map(one, [s1, s2]))
    assert (h1.row, h1.start_col, h1.end_col) == (1, 1, 3)
    assert (h2.row, h2.start_col, h2.end_col) == (1, 1, 3)
    assert _count(SeatHold, s1) == 1
    assert _count(SeatHold, s2) == 1


def test_party_too_large_logs_no_seats():
    # Hall width 10, aisle at 5 -> longest run is 4; ask for 8.
    sid = _make_showtime(rows=2, cols=10, aisle_cols="5")
    db = SessionLocal()
    try:
        with pytest.raises(SeatUnavailable) as ei:
            place_hold(db, showtime_id=sid, party_size=8)
        assert ei.value.code == "no_seats"
    finally:
        db.close()
    assert _count(SeatHold, sid) == 0
    db = SessionLocal()
    try:
        log = db.scalars(select(ConflictLog).where(ConflictLog.showtime_id == sid)).one()
        assert log.kind == "no_seats"
        assert log.party_size == 8
        assert (log.row, log.start_col, log.end_col) == (None, None, None)
    finally:
        db.close()


# -------------------------------------------------------------- HTTP (E2E)

@pytest.fixture(scope="module")
def live_server():
    import socket
    import uvicorn

    from app.main import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_http_concurrent_requests_one_201_rest_409(live_server):
    sid = _make_showtime()
    n = 5
    barrier = threading.Barrier(n)
    hold_service.pre_lock_hook = barrier.wait
    body = {"showtime_id": sid, "party_size": 3}

    def post(_i):
        return httpx.post(f"{live_server}/api/holds", json=body, timeout=10)

    with ThreadPoolExecutor(max_workers=n) as pool:
        responses = list(pool.map(post, range(n)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201] + [409] * (n - 1), statuses

    created = [r for r in responses if r.status_code == 201][0].json()
    assert (created["row"], created["start_col"], created["end_col"]) == (1, 1, 3)

    for r in responses:
        if r.status_code == 409:
            detail = r.json()["detail"]
            assert detail["code"] == "overlap"
            assert detail["showtime_id"] == sid
            assert detail["party_size"] == 3
            assert detail["span"] == {"row": 1, "start_col": 1, "end_col": 3}
            assert "冲突" in detail["message"]

    # Seat map reflects ONLY the winner; exactly 3 occupied cells.
    seatmap = httpx.get(f"{live_server}/api/seatmap/{sid}", timeout=10).json()
    occupied = {(c["row"], c["col"]) for c in seatmap["cells"] if c["occupied"]}
    assert occupied == {(1, 1), (1, 2), (1, 3)}

    # Orders list: one hold; conflicts list: n-1 rows.
    holds = httpx.get(f"{live_server}/api/holds", timeout=10).json()
    sid_holds = [h for h in holds if h["showtime_id"] == sid]
    assert len(sid_holds) == 1
    conflicts = httpx.get(f"{live_server}/api/conflicts", timeout=10).json()
    sid_conflicts = [c for c in conflicts if c["showtime_id"] == sid]
    assert len(sid_conflicts) == n - 1
    for c in sid_conflicts:
        assert c["kind"] == "overlap"
        assert c["party_size"] == 3
        assert (c["row"], c["start_col"], c["end_col"]) == (1, 1, 3)
        assert c["film_title"] == "并发测试片"


def test_http_unknown_showtime_returns_404(live_server):
    r = httpx.post(f"{live_server}/api/holds",
                   json={"showtime_id": 999999, "party_size": 2}, timeout=10)
    assert r.status_code == 404


# ----------------------------------------------- natural races (no test seam)

def _fire(sid, n, party):
    """Plain concurrent fire, NO barrier — the real production interleaving."""
    def one(_i):
        db = SessionLocal()
        try:
            return ("ok", place_hold(db, showtime_id=sid, party_size=party))
        except SeatUnavailable:
            return ("conflict", None)
        except Exception as exc:  # noqa: BLE001
            return ("error", exc)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, range(n)))


def _assert_holds_are_pairwise_disjoint(showtime_id):
    spans = _all_holds(showtime_id)
    # No two committed holds on the same row may share a column.
    seen: set[tuple[int, int]] = set()
    for row, s, e in spans:
        for col in range(s, e + 1):
            assert (row, col) not in seen, f"double-booked seat ({row},{col})"
            seen.add((row, col))
    return seen


def test_natural_burst_party_one_fills_every_seat_without_double_book():
    # 2 rows x 6 cols, aisle col 5 -> 10 bookable seats.
    sid = _make_showtime(rows=2, cols=6, aisle_cols="5")
    n = 40
    results = _fire(sid, n, party=1)

    assert {r[0] for r in results} <= {"ok", "conflict"}, results
    wins = sum(1 for r in results if r[0] == "ok")
    held = _count(SeatHold, sid)

    # Core safety: every committed hold sits on a distinct bookable seat.
    occupied = _assert_holds_are_pairwise_disjoint(sid)
    bookable = {(r, c) for r in (1, 2) for c in (1, 2, 3, 4, 6)}
    assert occupied <= bookable
    assert held == wins and held <= 10  # no bloat, never more seats than exist

    # Deterministic fill: losers never block later winners, so drive one
    # more *sequential* wave per remaining bookable seat — all must land.
    for _ in range(10):
        db = SessionLocal()
        try:
            place_hold(db, showtime_id=sid, party_size=1)
        except SeatUnavailable:
            pass
        finally:
            db.close()
    occupied = _assert_holds_are_pairwise_disjoint(sid)
    assert occupied == bookable
    assert _count(SeatHold, sid) == 10
    assert _count(ConflictLog, sid) == n + 10 - 10


def test_natural_burst_party_three_never_double_books():
    # Each row's longest run is [1-4] (aisle 5) -> one 3-block per row.
    sid = _make_showtime(rows=2, cols=6, aisle_cols="5")
    results = _fire(sid, 12, party=3)

    assert {r[0] for r in results} <= {"ok", "conflict"}, results
    wins = sum(1 for r in results if r[0] == "ok")
    held = _count(SeatHold, sid)

    _assert_holds_are_pairwise_disjoint(sid)
    assert set(_all_holds(sid)) <= {(1, 1, 3), (2, 1, 3)}
    assert held == wins and held <= 2
    assert _count(ConflictLog, sid) == 12 - wins


def test_rapid_back_to_back_sequential_requests():
    """Back-to-back (non-threaded) requests on one session also stay sound."""
    sid = _make_showtime(rows=1, cols=6, aisle_cols="5")  # runs [1-4],[6]
    db = SessionLocal()
    outcomes = []
    try:
        for _ in range(3):
            try:
                h = place_hold(db, showtime_id=sid, party_size=3)
                outcomes.append(("ok", (h.row, h.start_col, h.end_col)))
            except SeatUnavailable as exc:
                outcomes.append((exc.code, None))
    finally:
        db.close()
    # First takes 1-3; second can take 6? no — run [6] length 1, and
    # [1-4] now only has col4 free -> no 3-block -> no_seats.
    assert outcomes[0] == ("ok", (1, 1, 3))
    assert all(o[0] in ("overlap", "no_seats") for o in outcomes[1:])
    assert _count(SeatHold, sid) == 1
