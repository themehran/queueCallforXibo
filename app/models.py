from datetime import date, datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class QueueDay(SQLModel, table=True):
    service_date: date = Field(primary_key=True, index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class QueueEntry(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    service_date: date = Field(index=True, nullable=False)
    ticket_index: int = Field(index=True, nullable=False)
    ticket_number: str = Field(max_length=3, nullable=False)
    name: str = Field(max_length=255, nullable=False)
    phone: str = Field(max_length=32, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("service_date", "ticket_index", name="uq_queue_entry_per_day"),
    )
