from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
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
from app.services.hold_service import SeatUnavailable, ShowtimeNotFound, place_hold

api_router = APIRouter()


def _aisles(hall: Hall) -> list[int]:
    if not hall.aisle_cols.strip():
        return []
    return [int(x) for x in hall.aisle_cols.split(",") if x.strip()]


def _hall_out(h: Hall) -> HallOut:
    return HallOut(id=h.id, name=h.name, rows=h.rows, cols=h.cols, aisle_cols=_aisles(h))


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
    show_cache: dict[int, Showtime] = {}
    hall_cache: dict[int, Hall] = {}
    out: list[ConflictOut] = []
    for log in logs:
        st = show_cache.get(log.showtime_id) or db.get(Showtime, log.showtime_id)
        hall_name = None
        if st:
            show_cache[st.id] = st
            hall = hall_cache.get(st.hall_id) or db.get(Hall, st.hall_id)
            if hall:
                hall_cache[hall.id] = hall
                hall_name = hall.name
        out.append(
            ConflictOut(
                id=log.id,
                showtime_id=log.showtime_id,
                film_title=st.film_title if st else None,
                hall_name=hall_name,
                party_size=log.party_size,
                kind=log.kind,
                row=log.row,
                start_col=log.start_col,
                end_col=log.end_col,
                reason=log.reason,
                created_at=log.created_at,
            )
        )
    return out


@api_router.post("/holds", response_model=HoldOut, status_code=201)
def create_hold(body: HoldRequest, db: Session = Depends(get_db)):
    try:
        hold = place_hold(
            db,
            showtime_id=body.showtime_id,
            party_size=body.party_size,
            preferred_row=body.preferred_row,
        )
    except ShowtimeNotFound:
        raise HTTPException(404, "场次不存在")
    except SeatUnavailable as rejected:
        # 409 with a structured body: UI distinguishes conflict from network errors.
        raise HTTPException(status_code=409, detail=rejected.detail)
    return hold
