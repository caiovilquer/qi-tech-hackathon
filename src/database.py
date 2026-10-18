"""Database engines and the per-request context that carries the Base API session."""
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from constants import DATABASE_URL


engine = create_engine(
    DATABASE_URL, pool_size=5, pool_recycle=600, pool_pre_ping=True,
    connect_args={"options": "-c search_path=public"},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False)


def pix_engine_for(url):
    """Bounded Pix pool: 100 ms checkout, 1 s connect, 5 connections plus 10 overflow."""
    return create_engine(
        url, pool_size=5, pool_recycle=600, pool_pre_ping=True, pool_timeout=.1,
        connect_args={"options": "-c search_path=public", "connect_timeout": 1},
    )


pix_engine = pix_engine_for(DATABASE_URL)
PixSessionLocal = sessionmaker(bind=pix_engine, autoflush=False)


class Context:
    """Per-request holder of the Base API session; the session is created on first use."""

    def __init__(self) -> None:
        self.db_session: Optional[Session] = None

    def get_or_create_session(self) -> Session:
        if self.db_session is None:
            self.db_session = SessionLocal()
        return self.db_session


_context: ContextVar[Optional[Context]] = ContextVar("context", default=None)


def open_context() -> Context:
    context = Context()
    _context.set(context)
    return context


def clear_context() -> None:
    _context.set(None)


def get_context() -> Context:
    """Request context for Base API style controllers; the Pix flow uses PixStorage transactions instead."""
    context = _context.get()
    if context is None:
        raise RuntimeError("No request context: the session manager middleware did not run")
    return context
