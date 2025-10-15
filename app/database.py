from contextlib import contextmanager
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine


DB_PATH = Path("data")
DB_PATH.mkdir(exist_ok=True)
DATABASE_URL = f"sqlite:///{DB_PATH / 'queue.db'}"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session() -> Session:
    with Session(engine) as session:
        yield session
