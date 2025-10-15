from datetime import date, datetime
import re
from typing import Optional

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import delete, func
from sqlmodel import Session, select

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
    birthday: Optional[date] = None

    @field_validator("service_date", mode="before")
    @classmethod
    def parse_service_date(cls, value: object) -> Optional[date]:
        return parse_service_date_value(value)

    @field_validator("birthday", mode="before")
    @classmethod
    def parse_birthday(cls, value: object) -> Optional[date]:
        if value in (None, "", "null"):
            return None
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError("birthday must use YYYY-MM-DD format") from exc
        raise ValueError("Unsupported birthday value")


class QueueEntryUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    birthday: Optional[date] = None

    @field_validator("birthday", mode="before")
    @classmethod
    def parse_birthday(cls, value: object) -> Optional[date]:
        if value in (None, "", "null"):
            return None
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError("birthday must use YYYY-MM-DD format") from exc
        raise ValueError("Unsupported birthday value")


class QueueEntryRead(BaseModel):
    id: int
    service_date: date
    ticket_index: int
    ticket_number: str
    name: str
    phone: str
    created_at: datetime
    status: str
    birthday: Optional[date] = None

    model_config = ConfigDict(from_attributes=True)


class QueueDayRead(BaseModel):
    service_date: date
    started_at: datetime

    model_config = ConfigDict(from_attributes=True)


class QueueFlowResponse(BaseModel):
    active: Optional[QueueEntryRead] = None
    served: Optional[QueueEntryRead] = None
    detail: str


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


def normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[\s\-()]+", "", phone or "")
    if not cleaned:
        raise HTTPException(status_code=400, detail="Phone number is required")

    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]

    if cleaned.startswith("+"):
        digits = cleaned[1:]
    else:
        digits = cleaned

    if digits.startswith("98"):
        local = digits[2:]
    elif digits.startswith("0") and len(digits) == 11 and digits[1] == "9":
        local = digits[1:]
    elif digits.startswith("9") and len(digits) == 10:
        local = digits
    else:
        raise HTTPException(status_code=400, detail="Invalid Iranian mobile number format")

    if len(local) != 10 or not local.isdigit() or not local.startswith("9"):
        raise HTTPException(status_code=400, detail="Invalid Iranian mobile number format")

    return f"+98{local}"


def get_active_entry(session: Session, service_day: date) -> Optional[QueueEntry]:
    return session.exec(
        select(QueueEntry)
        .where(
            QueueEntry.service_date == service_day,
            QueueEntry.status == "active",
        )
        .order_by(QueueEntry.ticket_index.asc())
    ).first()


def get_next_waiting_entry(session: Session, service_day: date, after_index: int = 0) -> Optional[QueueEntry]:
    entry = session.exec(
        select(QueueEntry)
        .where(
            QueueEntry.service_date == service_day,
            QueueEntry.status == "waiting",
            QueueEntry.ticket_index > after_index,
        )
        .order_by(QueueEntry.ticket_index.asc())
    ).first()
    if entry is not None:
        return entry
    return session.exec(
        select(QueueEntry)
        .where(
            QueueEntry.service_date == service_day,
            QueueEntry.status == "waiting",
        )
        .order_by(QueueEntry.ticket_index.asc())
    ).first()


def get_last_served_entry(session: Session, service_day: date) -> Optional[QueueEntry]:
    return session.exec(
        select(QueueEntry)
        .where(
            QueueEntry.service_date == service_day,
            QueueEntry.status == "served",
        )
        .order_by(QueueEntry.ticket_index.desc())
    ).first()


@app.post("/queue/start-day", response_model=QueueDayRead, status_code=status.HTTP_201_CREATED)
def start_day(
    payload: StartDayRequest, session: Session = Depends(get_db_session)
) -> QueueDayRead:
    service_date = payload.service_date or date.today()
    existing = session.get(QueueDay, service_date)
    entries_exist = session.exec(
        select(QueueEntry.id).where(QueueEntry.service_date == service_date)
    ).first() is not None
    if existing and not payload.overwrite:
        raise HTTPException(status_code=400, detail="صف برای این تاریخ قبلاً آغاز شده است")
    if existing and entries_exist:
        raise HTTPException(
            status_code=400,
            detail="صف برای این تاریخ فعال است و امکان بازنشانی وجود ندارد",
        )
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
    normalized_phone = normalize_phone(phone)

    max_index = session.exec(
        select(func.max(QueueEntry.ticket_index)).where(QueueEntry.service_date == service_date)
    ).one()
    current_index = max_index or 0
    next_index = current_index + 1
    if next_index > 999:
        raise HTTPException(status_code=400, detail="Queue number limit reached for the day")

    entry = QueueEntry(
        service_date=service_date,
        ticket_index=next_index,
        ticket_number=f"{next_index:03d}",
        name=name,
        phone=normalized_phone,
        birthday=payload.birthday,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return QueueEntryRead.model_validate(entry)


@app.patch("/queue/entries/{entry_id}", response_model=QueueEntryRead)
def update_entry(
    entry_id: int, payload: QueueEntryUpdate, session: Session = Depends(get_db_session)
) -> QueueEntryRead:
    entry = session.get(QueueEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="نوبت یافت نشد")
    if entry.service_date != date.today():
        raise HTTPException(status_code=403, detail="ویرایش فقط برای نوبت‌های امروز مجاز است")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="هیچ فیلدی برای ویرایش ارسال نشده است")

    if "name" in updates:
        name = (updates["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="نام نمی‌تواند خالی باشد")
        entry.name = name

    if "phone" in updates:
        phone_value = (updates["phone"] or "").strip()
        if not phone_value:
            raise HTTPException(status_code=400, detail="شماره تماس نمی‌تواند خالی باشد")
        entry.phone = normalize_phone(phone_value)

    if "birthday" in updates:
        entry.birthday = updates["birthday"]

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
    active_entry = next((entry for entry in entries if entry.status == "active"), None)
    next_entry = next((entry for entry in entries if entry.status == "waiting"), None)
    return {
        "service_date": service_day.isoformat(),
        "count": len(entries),
        "active": (
            {
                "ticket": active_entry.ticket_number,
                "name": active_entry.name,
                "phone": active_entry.phone,
                "birthday": active_entry.birthday.isoformat() if active_entry.birthday else None,
            }
            if active_entry
            else None
        ),
        "next": (
            {
                "ticket": next_entry.ticket_number,
                "name": next_entry.name,
                "phone": next_entry.phone,
                "birthday": next_entry.birthday.isoformat() if next_entry.birthday else None,
            }
            if next_entry
            else None
        ),
        "queue": [
            {
                "ticket": entry.ticket_number,
                "name": entry.name,
                "phone": entry.phone,
                "status": entry.status,
                "birthday": entry.birthday.isoformat() if entry.birthday else None,
            }
            for entry in entries
        ],
    }


@app.post("/queue/next", response_model=QueueFlowResponse)
def call_next_number(
    service_date: Optional[str] = Body(default=None, embed=True),
    session: Session = Depends(get_db_session),
) -> QueueFlowResponse:
    service_day = resolve_service_day(service_date)
    active_entry = get_active_entry(session, service_day)
    served_entry: Optional[QueueEntry] = None
    last_index = 0
    if active_entry:
        active_entry.status = "served"
        served_entry = active_entry
        last_index = active_entry.ticket_index
    else:
        last_served = get_last_served_entry(session, service_day)
        if last_served is not None:
            last_index = last_served.ticket_index

    next_entry = get_next_waiting_entry(session, service_day, after_index=last_index)
    if next_entry:
        next_entry.status = "active"

    session.commit()

    if next_entry:
        session.refresh(next_entry)
    if served_entry:
        session.refresh(served_entry)

    return QueueFlowResponse(
        active=QueueEntryRead.model_validate(next_entry) if next_entry else None,
        served=QueueEntryRead.model_validate(served_entry) if served_entry else None,
        detail="نفر بعدی فراخوانی شد" if next_entry else "فردی در صف باقی نمانده است",
    )


@app.post("/queue/previous", response_model=QueueFlowResponse)
def call_previous_number(
    service_date: Optional[str] = Body(default=None, embed=True),
    session: Session = Depends(get_db_session),
) -> QueueFlowResponse:
    service_day = resolve_service_day(service_date)
    previous_entry = get_last_served_entry(session, service_day)
    if previous_entry is None:
        raise HTTPException(status_code=404, detail="نفر قبلی برای بازگرداندن وجود ندارد")

    active_entry = get_active_entry(session, service_day)
    if active_entry:
        active_entry.status = "waiting"
    previous_entry.status = "active"
    session.commit()

    session.refresh(previous_entry)
    if active_entry:
        session.refresh(active_entry)

    return QueueFlowResponse(
        active=QueueEntryRead.model_validate(previous_entry),
        served=None,
        detail="نفر قبلی دوباره فراخوانی شد",
    )


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
