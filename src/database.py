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


# Every Pix storage call holds at most one connection. A full pool makes a request
# wait up to PIX_POOL_TIMEOUT_SECONDS inside its deadline instead of failing at
# once with 503. Sized per uvicorn worker: API_WORKERS (default 4) * 20 Pix
# connections stays below PostgreSQL's default max_connections of 100; the Base
# API pool above only opens connections for Base API routes, never for Pix.
PIX_POOL_SIZE = 10
PIX_POOL_OVERFLOW = 10
PIX_POOL_TIMEOUT_SECONDS = 1


def pix_engine_for(url):
    """Bounded Pix pool: 1 s checkout, 1 s connect, 10 connections plus 10 overflow."""
    return create_engine(
        url, pool_size=PIX_POOL_SIZE, max_overflow=PIX_POOL_OVERFLOW, pool_recycle=600, pool_pre_ping=True,
        pool_timeout=PIX_POOL_TIMEOUT_SECONDS,
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
