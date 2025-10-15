from datetime import date, datetime
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import delete, func, select
from sqlmodel import Session

from .database import get_session, init_db
from .models import QueueDay, QueueEntry


app = FastAPI(
    title="Queue Service",
    openapi_url="/queue/openapi.json",
    docs_url="/queue/docs",
    redoc_url="/queue/redoc",
)
templates = Jinja2Templates(directory="app/templates")


def get_db_session() -> Session:
    with get_session() as session:
        yield session


def parse_service_date_value(value: object) -> Optional[date]:
    if value in (None, "", "null"):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("service_date must use YYYY-MM-DD format") from exc
    raise ValueError("Unsupported service_date value")


class StartDayRequest(BaseModel):
    service_date: Optional[date] = None
    overwrite: bool = False

    @field_validator("service_date", mode="before")
    @classmethod
    def parse_service_date(cls, value: object) -> Optional[date]:
        return parse_service_date_value(value)


class QueueEntryCreate(BaseModel):
    name: str
    phone: str
    service_date: Optional[date] = None

    @field_validator("service_date", mode="before")
    @classmethod
    def parse_service_date(cls, value: object) -> Optional[date]:
        return parse_service_date_value(value)


class QueueEntryRead(BaseModel):
    id: int
    service_date: date
    ticket_index: int
    ticket_number: str
    name: str
    phone: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class QueueDayRead(BaseModel):
    service_date: date
    started_at: datetime

    model_config = ConfigDict(from_attributes=True)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/queue/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


def resolve_service_day(value: Optional[str]) -> date:
    if value is None or value == "":
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid service_date format. Use YYYY-MM-DD."
        ) from exc


@app.post("/queue/start-day", response_model=QueueDayRead, status_code=status.HTTP_201_CREATED)
def start_day(
    payload: StartDayRequest, session: Session = Depends(get_db_session)
) -> QueueDayRead:
    service_date = payload.service_date or date.today()
    existing = session.get(QueueDay, service_date)
    if existing and not payload.overwrite:
        raise HTTPException(status_code=400, detail="Queue for this date already started")
    if existing and payload.overwrite:
        session.delete(existing)
        session.exec(delete(QueueEntry).where(QueueEntry.service_date == service_date))
        session.commit()

    queue_day = QueueDay(service_date=service_date)
    session.add(queue_day)
    session.commit()
    session.refresh(queue_day)
    return QueueDayRead.model_validate(queue_day)


@app.post("/queue/entries", response_model=QueueEntryRead, status_code=status.HTTP_201_CREATED)
def create_entry(
    payload: QueueEntryCreate, session: Session = Depends(get_db_session)
) -> QueueEntryRead:
    service_date = payload.service_date or date.today()
    queue_day = session.get(QueueDay, service_date)
    if queue_day is None:
        queue_day = QueueDay(service_date=service_date)
        session.add(queue_day)
        session.commit()

    name = payload.name.strip()
    phone = payload.phone.strip()
    if not name or not phone:
        raise HTTPException(status_code=400, detail="Name and phone are required")

    max_index = session.exec(
        select(func.max(QueueEntry.ticket_index)).where(QueueEntry.service_date == service_date)
    ).one()
    current_index = max_index[0] or 0
    next_index = current_index + 1
    if next_index > 999:
        raise HTTPException(status_code=400, detail="Queue number limit reached for the day")

    entry = QueueEntry(
        service_date=service_date,
        ticket_index=next_index,
        ticket_number=f"{next_index:03d}",
        name=name,
        phone=phone,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return QueueEntryRead.model_validate(entry)


@app.get("/queue/entries", response_model=list[QueueEntryRead])
def list_entries(
    service_date: Optional[str] = Query(default=None),
    session: Session = Depends(get_db_session),
) -> list[QueueEntryRead]:
    service_day = resolve_service_day(service_date)
    entries = session.exec(
        select(QueueEntry)
        .where(QueueEntry.service_date == service_day)
        .order_by(QueueEntry.ticket_index.asc())
    ).all()
    return [QueueEntryRead.model_validate(entry) for entry in entries]


@app.get("/queue/display")
def display_payload(
    service_date: Optional[str] = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    service_day = resolve_service_day(service_date)
    entries = session.exec(
        select(QueueEntry)
        .where(QueueEntry.service_date == service_day)
        .order_by(QueueEntry.ticket_index.asc())
    ).all()
    return {
        "service_date": service_day.isoformat(),
        "count": len(entries),
        "queue": [
            {
                "ticket": entry.ticket_number,
                "name": entry.name,
                "phone": entry.phone,
            }
            for entry in entries
        ],
    }


@app.post("/queue/xibo", response_model=QueueEntryRead)
def create_entry_from_form(
    name: str = Form(...),
    phone: str = Form(...),
    service_date: Optional[date] = Form(default=None),
    session: Session = Depends(get_db_session),
) -> QueueEntryRead:
    payload = QueueEntryCreate(name=name, phone=phone, service_date=service_date)
    return create_entry(payload, session)


@app.get("/queue/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("admin.html", {"request": request})
