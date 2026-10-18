import json
import logging
import sys

from constants import APP_ENV, SERVICE_NAME
from utils.request_context import get_request_id

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s [%(request_id)s] - %(message)s"
NOISY_LIBRARIES = ("urllib3",)


class RequestIdFilter(logging.Filter):
    """Stamps every record with the current request id; "-" outside a request."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


def setup_logging() -> None:
    level = logging.DEBUG if APP_ENV.upper() in ("LOCAL", "TEST") else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = [handler]
    for library in NOISY_LIBRARIES:
        logging.getLogger(library).setLevel(logging.CRITICAL)


def escape_for_log(value) -> str:
    """Client-controlled text as one JSON-escaped ASCII line, so it can never forge a log entry."""
    return json.dumps(str(value), ensure_ascii=True)[1:-1]


def request_path(request) -> str:
    """The path exactly as the client sent it, percent-encoding kept, so a logged ID matches the request.

    The decoded path can lose or merge characters (``abc%0Adef`` read as ``abcdef``); the raw bytes cannot.
    """
    raw_path = request.scope.get("raw_path")
    return raw_path.decode("latin-1") if isinstance(raw_path, bytes) else request.url.path


def log_fields(**fields) -> str:
    """Formats `key=value` pairs for a log line. Strings are escaped; None values are omitted."""
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value) or "-"
        elif isinstance(value, float):
            value = f"{value:.1f}"
        parts.append(f"{key}={escape_for_log(value)}")
    return " ".join(parts)


def get_logger(module_name: str) -> logging.Logger:
    return logging.getLogger(f"{SERVICE_NAME}.{module_name}")
