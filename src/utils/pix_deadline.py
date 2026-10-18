"""One monotonic budget across async I/O and PostgreSQL worker threads."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic

from sqlalchemy import event, text
from starlette.concurrency import run_in_threadpool

from providers.budget import BudgetExceeded

current_deadline = ContextVar('pix_deadline', default=None)


@dataclass(frozen=True)
class DatabaseClock:
    """PostgreSQL UTC anchor advanced only by monotonic elapsed time.

    Local wall-clock skew does not rewrite source times or initial coverage.
    PostgreSQL remains authoritative for leases and the final decision instant.
    """
    anchor: datetime
    started: float = field(default_factory=monotonic)

    def __call__(self):
        return self.anchor+timedelta(seconds=monotonic()-self.started)


async def in_database(deadline, operation, *args, **kwargs):
    def invoke():
        token = current_deadline.set(deadline)
        try:
            deadline.remaining()  # An expired queued worker must never start work.
            value = operation(*args, **kwargs)
            deadline.remaining()  # A late commit is an unknown result, never HTTP 201.
            return value
        finally:
            current_deadline.reset(token)
    try:
        seconds=deadline.remaining()
        return await asyncio.wait_for(run_in_threadpool(invoke), seconds)
    except TimeoutError as error:
        raise BudgetExceeded('Database request deadline exceeded; retry same ID/body') from error


# A statement may outlive the request budget by at most this much; commit is always exact.
LIMITS_REFRESH_SECONDS = .01


def bind_session_deadline(session,lock_timeout_ms=1000):
    """Recompute server timeouts from the remaining budget before statements, flush and commit.

    Server timeouts are reissued once LIMITS_REFRESH_SECONDS have passed since the
    last setting, and always before commit. Repeating set_config before each of a
    burst of fast statements was half of all round trips of an analysis.

    Cancellation of an HTTP waiter cannot kill a Python thread. Its budget is
    still checked here, preventing any later commit; if commit was already sent,
    the outcome is unknown and is resolved by the permanent ID/hash on retry.
    """
    deadline = current_deadline.get()
    if deadline is None:
        return
    issued_at = None

    def limits(force=False):
        nonlocal issued_at
        remaining = deadline.remaining()
        now = deadline.clock()
        if not force and issued_at is not None and now - issued_at < LIMITS_REFRESH_SECONDS:
            return
        ms = max(1, int(remaining*1000))
        session.connection().execute(text("SELECT set_config('statement_timeout', :s, true), "
            "set_config('lock_timeout', :l, true)"), {'s':f'{ms}ms', 'l':f'{min(ms,lock_timeout_ms)}ms'})
        issued_at = now

    limits(force=True)
    event.listen(session, 'do_orm_execute', lambda state: limits())
    event.listen(session, 'before_commit', lambda _: limits(force=True))
    event.listen(session, 'before_flush', lambda *_: limits())
