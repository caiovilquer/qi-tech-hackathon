from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from errors.base_error import (
    QIException,
    InternalError,
    InvalidParameter,
    InvalidSchema,
    MethodNotAllowed,
    NotFoundResource,
)
from utils.logger import get_logger, log_fields, request_path


logger = get_logger(__name__)


def qi_exception_to_response(exception: QIException) -> JSONResponse:
    """The only place that turns a QIException into the HTTP error envelope."""
    body = {
        "title": exception.title,
        "description": exception.description,
        "translation": exception.translation,
        "code": exception.code,
    }
    return JSONResponse(status_code=exception.http_status, content=body)


def describe_validation_error(error: dict) -> str:
    location = [str(part) for part in error.get("loc", []) if part not in ("body", "query")]
    message = error.get("msg", "invalid value")
    return f"{message} in {'.'.join(location)}" if location else message


def register_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(QIException)
    def handle_qi_exception(request: Request, exception: QIException) -> JSONResponse:
        logger.info("error.response " + log_fields(method=request.method, path=request_path(request),
                                                   status=exception.http_status, code=exception.code))
        return qi_exception_to_response(exception)

    @application.exception_handler(StarletteHTTPException)
    def handle_http_exception(request: Request, exception: StarletteHTTPException) -> JSONResponse:
        if exception.status_code == 404:
            return qi_exception_to_response(NotFoundResource())
        if exception.status_code == 405:
            return qi_exception_to_response(MethodNotAllowed())
        logger.error("error.http " + log_fields(method=request.method, path=request_path(request),
                                                status=exception.status_code, detail=exception.detail))
        return qi_exception_to_response(InternalError())

    @application.exception_handler(RequestValidationError)
    def handle_validation_error(request: Request, exception: RequestValidationError) -> JSONResponse:
        errors = exception.errors()
        first_error = errors[0] if errors else {}
        description = describe_validation_error(first_error)
        if first_error.get("loc", [""])[0] in ("query", "path"):
            return qi_exception_to_response(InvalidParameter(description))
        return qi_exception_to_response(InvalidSchema(description))

    @application.exception_handler(Exception)
    def handle_unexpected_error(request: Request, exception: Exception) -> JSONResponse:
        logger.exception("error.unexpected " + log_fields(method=request.method, path=request_path(request),
                                                          error=type(exception).__name__))
        return qi_exception_to_response(InternalError())
