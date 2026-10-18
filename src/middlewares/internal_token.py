from fastapi import FastAPI, Request

from constants import BYPASS_ENDPOINTS, INTERNAL_TOKEN
from errors.base_error import ForbiddenNotInternal
from errors.handlers import qi_exception_to_response
from providers.budget import Deadline
from utils.logger import get_logger, log_fields, request_path
from utils.pix_tenants import resolve_tenant
from utils.request_context import get_request_id

PIX_ROUTE = "/pix/transaction"
PIX_REQUEST_BUDGET_SECONDS = 3

logger = get_logger(__name__)


def register_internal_token_middleware(application: FastAPI) -> None:
    """Rejects requests without a valid INTERNAL-TOKEN before any database session exists.

    Pix routes resolve their tenant from the credential and receive the request budget here.
    Errors are returned instead of raised: exception handlers run inside the middleware stack.
    """

    def deny(request: Request):
        logger.warning("auth.denied " + log_fields(method=request.method, path=request_path(request)))
        return qi_exception_to_response(ForbiddenNotInternal())

    @application.middleware("http")
    async def check_internal_token(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in BYPASS_ENDPOINTS:
            return await call_next(request)
        credential = request.headers.get("INTERNAL-TOKEN")
        if path == PIX_ROUTE or path.startswith(PIX_ROUTE + "/"):
            request.state.pix_deadline = Deadline.after(PIX_REQUEST_BUDGET_SECONDS)
            tenant = resolve_tenant(application.state.pix_tenants, credential)
            if tenant is None:
                return deny(request)
            request.state.pix_tenant = tenant
            request.state.request_id = get_request_id()
            return await call_next(request)
        if credential != INTERNAL_TOKEN:
            return deny(request)
        return await call_next(request)
