"""Official Pix HTTP boundary. Never let FastAPI decode the financial JSON."""
import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse
from errors.pix import PixError
from utils.logger import get_logger

logger=get_logger(__name__)

LIMIT=65536


class PixResource:
    async def on_post(self, request: Request):
        service=request.app.state.pix_controller
        tenant=request.state.pix_tenant
        deadline=request.state.pix_deadline
        receipt=None
        delegated=False
        try:
            try:
                receipt=await service.receive(request.state.request_id,tenant.name,deadline)
            except Exception:
                # Still validate a bounded body: a known client error is not a
                # transient financial outcome. Valid input requires a receipt.
                logger.error('Pix receipt could not be persisted')
            length=request.headers.get('content-length')
            if length is not None:
                if not length.isascii() or not length.isdigit():
                    raise PixError(400,'QIT000001')
                if int(length)>LIMIT:
                    raise PixError(413,'QIT002009')
            raw=bytearray()
            async with asyncio.timeout(max(.001,deadline.remaining()-.1)):
                async for chunk in request.stream():
                    if len(raw)+len(chunk)>LIMIT:
                        raise PixError(413,'QIT002009')
                    raw.extend(chunk)
            delegated=True
            status,body=await service.post(bytes(raw),tenant,request.app.state.pix_policies.for_tenant(tenant.name),receipt,deadline)
            return JSONResponse(body,status_code=status)
        except PixError as error:
            if not delegated:
                await service.finish(receipt,tenant.name,error.http_status,error.code,deadline)
            raise
        except Exception as error:
            logger.error('Pix body/receipt processing failed')
            await service.finish(receipt,tenant.name,503,'QIT002008',deadline)
            raise PixError() from error

    async def on_get(self, request: Request, id: str):
        body=await request.app.state.pix_controller.get(request.state.pix_tenant.name,id,request.state.pix_deadline)
        return JSONResponse(body)
