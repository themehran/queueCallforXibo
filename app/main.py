from datetime import date, datetime
import re
from typing import Optional

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import delete, func
from sqlmodel import Session, select

from .database import get_session, init_db
from .models import QueueDay, QueueEntry, QueueLoadSnapshot


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


PERSIAN_DIGIT_TRANSLATION = {
    ord("۰"): "0",
    ord("۱"): "1",
    ord("۲"): "2",
    ord("۳"): "3",
    ord("۴"): "4",
    ord("۵"): "5",
    ord("۶"): "6",
    ord("۷"): "7",
    ord("۸"): "8",
    ord("۹"): "9",
    ord("٠"): "0",
    ord("١"): "1",
    ord("٢"): "2",
    ord("٣"): "3",
    ord("٤"): "4",
    ord("٥"): "5",
    ord("٦"): "6",
    ord("٧"): "7",
    ord("٨"): "8",
    ord("٩"): "9",
}


def normalize_digits(value: str) -> str:
    return (value or "").translate(PERSIAN_DIGIT_TRANSLATION)


def normalize_phone(phone: str) -> str:
    normalized = normalize_digits(phone or "")
    cleaned = re.sub(r"[\s\-()]+", "", normalized)
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


SNAPSHOT_INTERVAL_MINUTES = 30


def truncate_to_window(dt: datetime, minutes: int = SNAPSHOT_INTERVAL_MINUTES) -> datetime:
    minute = (dt.minute // minutes) * minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def fetch_queue_entries(session: Session, service_day: date) -> list[QueueEntry]:
    return session.exec(
        select(QueueEntry)
        .where(QueueEntry.service_date == service_day)
        .order_by(QueueEntry.ticket_index.asc())
    ).all()


def summarize_queue(entries: list[QueueEntry]) -> dict[str, object]:
    active_entry: Optional[QueueEntry] = None
    next_entry: Optional[QueueEntry] = None
    waiting_count = 0
    served_count = 0

    for entry in entries:
        if entry.status == "served":
            served_count += 1
            continue
        if entry.status == "active" and active_entry is None:
            active_entry = entry
        if entry.status == "waiting":
            waiting_count += 1
            if next_entry is None:
                next_entry = entry

    pending_count = len(entries) - served_count
    return {
        "active": active_entry,
        "next": next_entry,
        "waiting_count": waiting_count,
        "served_count": served_count,
        "pending_count": pending_count,
    }


def record_queue_snapshot(
    session: Session,
    service_day: date,
    entries: Optional[list[QueueEntry]] = None,
    summary: Optional[dict[str, object]] = None,
) -> None:
    if entries is None:
        entries = fetch_queue_entries(session, service_day)
    if summary is None:
        summary = summarize_queue(entries)

    window_start = truncate_to_window(datetime.utcnow())
    snapshot = session.get(QueueLoadSnapshot, (service_day, window_start))
    if snapshot is None:
        snapshot = QueueLoadSnapshot(
            service_date=service_day,
            window_start=window_start,
            pending_count=summary["pending_count"],
            waiting_count=summary["waiting_count"],
            served_count=summary["served_count"],
        )
        session.add(snapshot)
    else:
        snapshot.pending_count = summary["pending_count"]
        snapshot.waiting_count = summary["waiting_count"]
        snapshot.served_count = summary["served_count"]
        snapshot.captured_at = datetime.utcnow()
    session.commit()


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
    record_queue_snapshot(session, service_date)
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
    phone = (payload.phone or "").strip()
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
    record_queue_snapshot(session, service_date)
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
        phone_value = normalize_digits((updates["phone"] or "").strip())
        if not phone_value:
            raise HTTPException(status_code=400, detail="شماره تماس نمی‌تواند خالی باشد")
        entry.phone = normalize_phone(phone_value)

    if "birthday" in updates:
        entry.birthday = updates["birthday"]

    session.add(entry)
    session.commit()
    session.refresh(entry)
    record_queue_snapshot(session, entry.service_date)
    return QueueEntryRead.model_validate(entry)


@app.get("/queue/entries", response_model=list[QueueEntryRead])
def list_entries(
    service_date: Optional[str] = Query(default=None),
    session: Session = Depends(get_db_session),
) -> list[QueueEntryRead]:
    service_day = resolve_service_day(service_date)
    entries = fetch_queue_entries(session, service_day)
    return [QueueEntryRead.model_validate(entry) for entry in entries]


@app.get("/queue/display")
def display_payload(
    service_date: Optional[str] = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    service_day = resolve_service_day(service_date)
    entries = fetch_queue_entries(session, service_day)
    summary = summarize_queue(entries)
    snapshots = session.exec(
        select(QueueLoadSnapshot)
        .where(QueueLoadSnapshot.service_date == service_day)
        .order_by(QueueLoadSnapshot.window_start.asc())
    ).all()
    return {
        "service_date": service_day.isoformat(),
        "count": len(entries),
        "pending_count": summary["pending_count"],
        "waiting_count": summary["waiting_count"],
        "served_count": summary["served_count"],
        "active": (
            {
                "ticket": summary["active"].ticket_number,
                "name": summary["active"].name,
                "phone": summary["active"].phone,
                "birthday": summary["active"].birthday.isoformat()
                if summary["active"].birthday
                else None,
            }
            if summary["active"]
            else None
        ),
        "next": (
            {
                "ticket": summary["next"].ticket_number,
                "name": summary["next"].name,
                "phone": summary["next"].phone,
                "birthday": summary["next"].birthday.isoformat()
                if summary["next"].birthday
                else None,
            }
            if summary["next"]
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
        "history": [
            {
                "window_start": snapshot.window_start.isoformat(),
                "pending_count": snapshot.pending_count,
                "waiting_count": snapshot.waiting_count,
                "served_count": snapshot.served_count,
                "captured_at": snapshot.captured_at.isoformat(),
            }
            for snapshot in snapshots
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

    record_queue_snapshot(session, service_day)

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

    record_queue_snapshot(session, service_day)

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


@app.get("/queue/xibo-dataset")
def xibo_dataset(
    service_date: Optional[str] = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """
    XIBO DataSet endpoint for displaying queue information on digital signage.
    Returns current serving and next serving information as a list suitable for XIBO DataSets.

    Each row contains:
    - row_type: "current" or "next"
    - ticket_number: Queue ticket number (e.g., "001")
    - name: Person's name
    - status_label: Display label in Persian
    """
    service_day = resolve_service_day(service_date)
    entries = fetch_queue_entries(session, service_day)
    summary = summarize_queue(entries)

    result = []

    # Current serving
    if summary["active"]:
        active = summary["active"]
        result.append({
            "row_type": "current",
            "ticket_number": active.ticket_number,
            "name": active.name,
            "phone": active.phone,
            "status_label": "در حال خدمت",
            "waiting_count": summary["waiting_count"],
            "served_count": summary["served_count"],
        })
    else:
        result.append({
            "row_type": "current",
            "ticket_number": "—",
            "name": "در انتظار فراخوانی",
            "phone": "",
            "status_label": "—",
            "waiting_count": summary["waiting_count"],
            "served_count": summary["served_count"],
        })

    # Next serving
    if summary["next"]:
        next_entry = summary["next"]
        result.append({
            "row_type": "next",
            "ticket_number": next_entry.ticket_number,
            "name": next_entry.name,
            "phone": next_entry.phone,
            "status_label": "نفر بعدی",
            "waiting_count": summary["waiting_count"],
            "served_count": summary["served_count"],
        })
    else:
        result.append({
            "row_type": "next",
            "ticket_number": "—",
            "name": "صف خالی است",
            "phone": "",
            "status_label": "—",
            "waiting_count": summary["waiting_count"],
            "served_count": summary["served_count"],
        })

    return {"data": result}


@app.get("/queue/xibo-simple")
def xibo_simple(
    service_date: Optional[str] = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict[str, object]:
    """
    Simplified XIBO endpoint returning current and next serving as single object.
    Perfect for simple XIBO ticker displays.

    Returns:
    - current_number: Current ticket number
    - current_name: Current person name
    - next_number: Next ticket number
    - next_name: Next person name
    - waiting_count: Number of people waiting
    - served_count: Number of people served
    """
    service_day = resolve_service_day(service_date)
    entries = fetch_queue_entries(session, service_day)
    summary = summarize_queue(entries)

    return {
        "data": {
            "current_number": summary["active"].ticket_number if summary["active"] else "—",
            "current_name": summary["active"].name if summary["active"] else "در انتظار فراخوانی",
            "next_number": summary["next"].ticket_number if summary["next"] else "—",
            "next_name": summary["next"].name if summary["next"] else "صف خالی است",
            "waiting_count": summary["waiting_count"],
            "served_count": summary["served_count"],
            "pending_count": summary["pending_count"],
            "total_count": len(entries),
        }
    }


@app.get("/queue/rss")
def queue_rss_feed(
    service_date: Optional[str] = Query(default=None),
    session: Session = Depends(get_db_session),
) -> Response:
    """
    RSS feed endpoint for Xibo RSS Ticker widget.

    Returns real-time queue information in RSS 2.0 format.
    Configure your Xibo RSS Ticker widget to poll this endpoint
    at regular intervals (e.g., every 10-30 seconds) for automatic updates.

    RSS items include:
    - Current serving ticket and name
    - Next serving ticket and name
    - Queue statistics (waiting count, served count)
    """
    service_day = resolve_service_day(service_date)
    entries = fetch_queue_entries(session, service_day)
    summary = summarize_queue(entries)

    # Build current serving info
    current_ticket = summary["active"].ticket_number if summary["active"] else "—"
    current_name = summary["active"].name if summary["active"] else "در انتظار فراخوانی"

    # Build next serving info
    next_ticket = summary["next"].ticket_number if summary["next"] else "—"
    next_name = summary["next"].name if summary["next"] else "صف خالی است"

    # Build RSS XML with proper escaping
    rss_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>وضعیت صف</title>
    <description>اطلاعات لحظه‌ای صف</description>
    <link>http://localhost/queue</link>
    <lastBuildDate>{datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')}</lastBuildDate>
    <item>
      <title>در حال خدمت: {current_ticket}</title>
      <description>{current_name}</description>
      <guid isPermaLink="false">current-{service_day.isoformat()}-{current_ticket}</guid>
    </item>
    <item>
      <title>نفر بعدی: {next_ticket}</title>
      <description>{next_name}</description>
      <guid isPermaLink="false">next-{service_day.isoformat()}-{next_ticket}</guid>
    </item>
    <item>
      <title>آمار صف</title>
      <description>در انتظار: {summary["waiting_count"]} | خدمت شده: {summary["served_count"]} | کل: {summary["pending_count"]}</description>
      <guid isPermaLink="false">stats-{service_day.isoformat()}-{datetime.utcnow().timestamp()}</guid>
    </item>
  </channel>
</rss>"""

    return Response(content=rss_content, media_type="application/rss+xml")


@app.get("/queue/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("admin.html", {"request": request})


@app.get("/queue/load-history")
def load_history(
    service_date: Optional[str] = Query(default=None),
    session: Session = Depends(get_db_session),
) -> list[dict[str, object]]:
    service_day = resolve_service_day(service_date)
    snapshots = session.exec(
        select(QueueLoadSnapshot)
        .where(QueueLoadSnapshot.service_date == service_day)
        .order_by(QueueLoadSnapshot.window_start.asc())
    ).all()
    return [
        {
            "service_date": snapshot.service_date.isoformat(),
            "window_start": snapshot.window_start.isoformat(),
            "pending_count": snapshot.pending_count,
            "waiting_count": snapshot.waiting_count,
            "served_count": snapshot.served_count,
            "captured_at": snapshot.captured_at.isoformat(),
        }
        for snapshot in snapshots
    ]
