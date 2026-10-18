from fastapi import FastAPI, Request

from constants import BYPASS_ENDPOINTS, INTERNAL_TOKEN
from errors.base_error import ForbiddenNotInternal
from errors.handlers import qi_exception_to_response
from controllers.pix_api import resolve_tenant
from providers.budget import Deadline
from utils.request_context import get_request_id


def register_internal_token_middleware(application: FastAPI) -> None:
    """Deixa passar só quem manda o header INTERNAL-TOKEN com o valor certo.

    O valor certo vem da variável de ambiente INTERNAL_TOKEN, nunca do
    código. As rotas de BYPASS_ENDPOINTS (raiz e health check) são públicas.

    Repare que aqui a gente DEVOLVE a resposta de erro em vez de levantar
    a exceção: dentro de um middleware, ninguém está escutando pra traduzir
    o erro — quem traduz são os exception handlers, que rodam mais pra
    dentro. Levantar aqui viraria um 500.
    """

    @application.middleware("http")
    async def check_internal_token(request: Request, call_next):
        is_public = request.url.path in BYPASS_ENDPOINTS

        if request.method == "OPTIONS" or is_public:
            return await call_next(request)

        if request.url.path == '/pix/transaction' or request.url.path.startswith('/pix/transaction/'):
            request.state.pix_deadline = Deadline.after(3)
            credential = request.headers.get('INTERNAL-TOKEN')
            tenant = resolve_tenant(application.state.pix_tenants, credential)
            if tenant is None:
                # Anonymous receipts contain no credential or body. Authentication
                # semantics stay 403 even if storage itself is unavailable.
                service = application.state.pix_controller
                try:
                    receipt = await service.receive(get_request_id(), None, request.state.pix_deadline)
                    await service.finish(receipt,None,403,'QIT000002',request.state.pix_deadline)
                except Exception:
                    from utils.logger import get_logger
                    get_logger(__name__).error('Anonymous Pix receipt could not be persisted')
                return qi_exception_to_response(ForbiddenNotInternal())
            request.state.pix_tenant = tenant
            request.state.request_id = get_request_id()
            return await call_next(request)

        if request.headers.get("INTERNAL-TOKEN") != INTERNAL_TOKEN:
            return qi_exception_to_response(ForbiddenNotInternal())

        return await call_next(request)
