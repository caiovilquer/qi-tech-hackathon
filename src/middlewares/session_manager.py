from fastapi import FastAPI, Request

from database import clear_context, open_context


def register_session_manager_middleware(application: FastAPI) -> None:
    """Owns the lifecycle of the Base API session: opened lazily, rolled back on error, always closed.

    It never commits; controllers decide when their work is complete. Routes that build no
    controller (home, health check) never open a session and do not depend on the database.
    """

    @application.middleware("http")
    async def manage_session(request: Request, call_next):
        context = open_context()
        try:
            response = await call_next(request)
        except Exception:
            if context.db_session is not None:
                context.db_session.rollback()
            raise
        finally:
            if context.db_session is not None:
                context.db_session.close()
            clear_context()
        return response
