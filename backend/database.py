from pathlib import Path

from sqlalchemy import event
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_PATH = (Path(__file__).resolve().parent / "yottaflex.db").resolve()
DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


def enable_sqlite_foreign_keys(target_engine) -> None:
    @event.listens_for(target_engine, "connect")
    def set_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


enable_sqlite_foreign_keys(engine)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def init_db() -> None:
    import models

    Base.metadata.create_all(bind=engine)