import time

from fastapi import FastAPI, Request

from constants import BYPASS_ENDPOINTS
from utils.logger import get_logger, log_fields

logger = get_logger(__name__)


def register_request_logger_middleware(application: FastAPI) -> None:
    """Logs the start and the end of every request with method, path, status and elapsed time.

    Request bodies are never logged. Public bypass endpoints (health check) are skipped.
    """

    @application.middleware("http")
    async def log_request(request: Request, call_next):
        if request.url.path in BYPASS_ENDPOINTS:
            return await call_next(request)
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        started_at = time.perf_counter()
        logger.info("request.started " + log_fields(method=request.method, path=target))
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info("request.finished " + log_fields(method=request.method, path=request.url.path,
                                                     status=response.status_code, elapsed_ms=elapsed_ms))
        return response
