"""同场次并发/紧邻锁座的互斥测试。

通过在候选区间计算后设置线程屏障，强制两笔（或多笔）请求都基于同一份旧快照
算出重叠区间，再依次进入临界区，精确复现“同时抢同一连座”的竞态。
"""

import threading

from fastapi.testclient import TestClient

import app.api.router as router_mod
from app.database import SessionLocal
from app.main import app
from app.models.models import ConflictLog, SeatHold
from sqlalchemy import select


def _holds_for(showtime_id: int) -> list[SeatHold]:
    db = SessionLocal()
    try:
        return db.scalars(
            select(SeatHold).where(SeatHold.showtime_id == showtime_id)
        ).all()
    finally:
        db.close()


def _conflicts_for(showtime_id: int) -> list[ConflictLog]:
    db = SessionLocal()
    try:
        return db.scalars(
            select(ConflictLog).where(ConflictLog.showtime_id == showtime_id)
        ).all()
    finally:
        db.close()


def _post(client, showtime_id, party, preferred_row=None):
    body = {"showtime_id": showtime_id, "party_size": party}
    if preferred_row is not None:
        body["preferred_row"] = preferred_row
    return client.post("/api/holds", json=body)


def _synced_chooser(parties: int):
    """让 parties 个线程都算完候选后再放行进入临界区。"""
    barrier = threading.Barrier(parties)
    original = router_mod._choose_block

    def wrapped(*args, **kwargs):
        block = original(*args, **kwargs)
        barrier.wait(timeout=10)
        return block

    router_mod._choose_block = wrapped
    return original


def _run_concurrent(n: int, sid: int, party: int, preferred_row=None):
    """n 个线程同时发起同一参数的锁座请求（候选计算处屏障同步）。"""
    bodies = [
        {"showtime_id": sid, "party_size": party, **({"preferred_row": preferred_row} if preferred_row is not None else {})}
        for _ in range(n)
    ]
    return _run_concurrent_bodies(bodies)


def _run_concurrent_bodies(bodies: list[dict]):
    """多个线程在候选计算处屏障同步后同时提交，精确复现同快照竞态。"""
    n = len(bodies)
    original = _synced_chooser(n)
    results: list = []

    def worker(body):
        tc = TestClient(app)  # 不进入 with：不触发 lifespan，表已由夹具建好
        results.append(tc.post("/api/holds", json=body))

    try:
        threads = [threading.Thread(target=worker, args=(b,)) for b in bodies]
        [t.start() for t in threads]
        [t.join() for t in threads]
    finally:
        router_mod._choose_block = original
    return results


def _span_overlap(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    r1, s1, e1 = a
    r2, s2, e2 = b
    return r1 == r2 and not (e1 < s2 or e2 < s1)


def test_concurrent_overlapping_one_wins_one_loses(client, make_showtime):
    sid, _ = make_showtime(rows=3, cols=10)
    results = _run_concurrent(2, sid, party=5, preferred_row=1)

    codes = [r.status_code for r in results]
    assert codes.count(200) == 1 and codes.count(409) == 1, codes

    win = next(r for r in results if r.status_code == 200).json()
    lose = next(r for r in results if r.status_code == 409).json()["detail"]

    # 成功方坐标正确：第 1 排 1-5
    assert (win["row"], win["start_col"], win["end_col"]) == (1, 1, 5)
    assert win["party_size"] == 5

    # 失败方可读：明确是冲突而非网络错误，且带被拒绝请求号/场次/人数/坐标
    assert lose["code"] == "SEAT_CONFLICT"
    assert "冲突" in lose["message"]
    assert lose["showtime_id"] == sid
    assert lose["party_size"] == 5
    assert lose["requested"] == {"row": 1, "start_col": 1, "end_col": 5}
    assert lose["blocking_order_code"] == win["order_code"]
    assert lose["request_code"] != win["order_code"]

    # 库中持座条数不膨胀：只有成功方一条
    holds = _holds_for(sid)
    assert len(holds) == 1
    assert (holds[0].row, holds[0].start_col, holds[0].end_col) == (1, 1, 5)

    # 失败方有冲突记录：场次、人数、被拒绝单号、坐标、挡住它的单号齐全
    conflicts = _conflicts_for(sid)
    assert len(conflicts) == 1
    clog = conflicts[0]
    assert clog.request_code == lose["request_code"]
    assert clog.party_size == 5
    assert (clog.requested_row, clog.requested_start_col, clog.requested_end_col) == (1, 1, 5)
    assert clog.blocking_order_code == win["order_code"]
    assert "冲突" in clog.reason


def test_concurrent_partial_overlap_one_wins_one_loses(client, make_showtime):
    # 3 人算出第1排 1-3；5 人算出第1排 1-5。二者在 1-3 列部分重叠 → 仍须一胜一负
    sid, _ = make_showtime(rows=2, cols=10)
    results = _run_concurrent_bodies([
        {"showtime_id": sid, "party_size": 3, "preferred_row": 1},
        {"showtime_id": sid, "party_size": 5, "preferred_row": 1},
    ])
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1 and codes.count(409) == 1, codes

    win = next(r for r in results if r.status_code == 200).json()
    lose = next(r for r in results if r.status_code == 409).json()["detail"]
    assert lose["code"] == "SEAT_CONFLICT"
    assert _span_overlap(
        (win["row"], win["start_col"], win["end_col"]),
        (lose["requested"]["row"], lose["requested"]["start_col"], lose["requested"]["end_col"]),
    )
    holds = _holds_for(sid)
    assert len(holds) == 1  # 部分重叠也不允许双成功、条数不膨胀
    assert len(_conflicts_for(sid)) == 1


def test_concurrent_non_overlapping_different_rows_both_succeed(client, make_showtime):
    # 不同排、区间不重叠：不应被误伤，两笔都应成功
    sid, _ = make_showtime(rows=2, cols=10)
    results = _run_concurrent_bodies([
        {"showtime_id": sid, "party_size": 3, "preferred_row": 1},
        {"showtime_id": sid, "party_size": 3, "preferred_row": 2},
    ])
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 200], [(r.status_code, r.text) for r in results]
    holds = _holds_for(sid)
    assert len(holds) == 2
    assert _conflicts_for(sid) == []
    spans = [(h.row, h.start_col, h.end_col) for h in holds]
    assert not _span_overlap(spans[0], spans[1])


def test_seatmap_reflects_only_winner(client, make_showtime):
    sid, _ = make_showtime(rows=2, cols=8)
    results = _run_concurrent(2, sid, party=4, preferred_row=1)
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1 and codes.count(409) == 1, codes

    sm = client.get(f"/api/seatmap/{sid}").json()
    occupied = {(c["row"], c["col"]) for c in sm["cells"] if c["occupied"]}
    # 座位图只体现成功方占用（第 1 排 1-4），不含失败方
    assert occupied == {(1, c) for c in range(1, 5)}


def test_consecutive_search_avoids_winner_coords(client, make_showtime):
    # 紧邻的连续请求（无重叠快照）：后者自动搜索必须绕开前者已占座，两笔都成
    sid, _ = make_showtime(rows=1, cols=10)
    r1 = _post(client, sid, party=3, preferred_row=1)
    r2 = _post(client, sid, party=3, preferred_row=1)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    a, b = r1.json(), r2.json()
    assert (a["row"], a["start_col"], a["end_col"]) == (1, 1, 3)
    assert (b["row"], b["start_col"], b["end_col"]) == (1, 4, 6)
    assert len(_holds_for(sid)) == 2
    assert _conflicts_for(sid) == []


def test_burst_never_double_succeeds_and_no_inflation(client, make_showtime):
    sid, _ = make_showtime(rows=2, cols=10)
    n = 6
    results = _run_concurrent(n, sid, party=2, preferred_row=1)

    # 所有候选都固定在第 1 排 1-2，故恰好一胜，其余皆冲突失败
    wins = [r for r in results if r.status_code == 200]
    loses = [r for r in results if r.status_code == 409]
    assert len(wins) == 1, [r.status_code for r in results]
    assert len(loses) == n - 1
    assert all(r.json()["detail"]["code"] == "SEAT_CONFLICT" for r in loses)

    holds = _holds_for(sid)
    assert len(holds) == 1  # 条数不膨胀
    assert (holds[0].row, holds[0].start_col, holds[0].end_col) == (1, 1, 2)
    assert len(_conflicts_for(sid)) == n - 1

    # 持座两两区间绝不重叠（含部分重叠）
    spans = [(h.row, h.start_col, h.end_col) for h in holds]
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            r1, s1, e1 = spans[i]
            r2, s2, e2 = spans[j]
            assert r1 != r2 or e1 < s2 or e2 < s1


def test_conflicts_page_lists_rejected_request(client, make_showtime):
    sid, _ = make_showtime(rows=1, cols=6)
    results = _run_concurrent(2, sid, party=4, preferred_row=1)
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1 and codes.count(409) == 1, codes

    page = client.get("/api/conflicts").json()
    rejected = next(c for c in page if c["showtime_id"] == sid)
    assert rejected["party_size"] == 4
    assert rejected["film_title"] == "并发测试片"
    assert rejected["request_code"]
    assert (
        rejected["requested_row"],
        rejected["requested_start_col"],
        rejected["requested_end_col"],
    ) == (1, 1, 4)
    assert rejected["blocking_order_code"]
