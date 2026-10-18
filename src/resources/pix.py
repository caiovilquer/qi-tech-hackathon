"""Pix HTTP boundary: a bounded raw body in, the controller result out. FastAPI never decodes the financial JSON."""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from errors.pix import PixError
from utils.logger import get_logger, log_fields

logger = get_logger(__name__)

MAX_BODY_BYTES = 65536
BODY_READ_MARGIN_SECONDS = .1


class PixResource:
    async def on_post(self, request: Request):
        controller = request.app.state.pix_controller
        tenant = request.state.pix_tenant
        deadline = request.state.pix_deadline
        receipt = None
        handed_to_controller = False
        try:
            try:
                receipt = await controller.receive(request.state.request_id, tenant.name, deadline)
            except Exception:
                # The bounded body is still validated: a client error must not become a 503.
                logger.exception('pix.receipt.failed ' + log_fields(tenant=tenant.name))
            body = await self._read_body(request, deadline)
            handed_to_controller = True
            policy = request.app.state.pix_policies.for_tenant(tenant.name)
            status, response = await controller.post(body, tenant, policy, receipt, deadline)
            return JSONResponse(response, status_code=status)
        except PixError as error:
            if not handed_to_controller:
                await controller.finish(receipt, tenant.name, error.http_status, error.code, deadline)
            raise
        except Exception as error:
            logger.exception('pix.request.failed ' + log_fields(tenant=tenant.name, error=type(error).__name__))
            await controller.finish(receipt, tenant.name, 503, 'QIT002008', deadline)
            raise PixError() from error

    @staticmethod
    async def _read_body(request: Request, deadline) -> bytes:
        declared_length = request.headers.get('content-length')
        if declared_length is not None:
            if not declared_length.isascii() or not declared_length.isdigit():
                raise PixError(400, 'QIT000001')
            if int(declared_length) > MAX_BODY_BYTES:
                raise PixError(413, 'QIT002009')
        body = bytearray()
        async with asyncio.timeout(max(.001, deadline.remaining() - BODY_READ_MARGIN_SECONDS)):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_BODY_BYTES:
                    raise PixError(413, 'QIT002009')
                body.extend(chunk)
        return bytes(body)

    async def on_get(self, request: Request, id: str):
        controller = request.app.state.pix_controller
        analysis = await controller.get(request.state.pix_tenant.name, id, request.state.pix_deadline)
        return JSONResponse(analysis)
