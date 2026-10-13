"""Engine/session construction shared by the API and workers."""

from datetime import datetime

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import Settings


def create_db_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=10,
        connect_args={"connect_timeout": 5},
    )


def create_sessions(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def database_now(session: Session) -> datetime:
    """Database clock; all scheduling and lease decisions use it."""
    result = session.scalar(select(func.clock_timestamp()))
    assert isinstance(result, datetime)
    return result
