"""Client-controlled text never breaks a log line, in the request logger or in the error handlers."""
import asyncio
import logging

import pytest
from fastapi import FastAPI
from starlette.exceptions import HTTPException

from errors import register_error_handlers


def call(app, path):
    messages=[]
    async def receive():
        return {'type':'http.request','body':b'','more_body':False}
    async def send(message):
        messages.append(message)
    scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET','scheme':'http',
           'path':path,'raw_path':path.encode('utf-8','surrogateescape'),'query_string':b'','headers':[],
           'server':('test',80),'client':('127.0.0.1',1)}
    try:
        asyncio.run(app(scope,receive,send))
    except RuntimeError:
        pass  # Starlette re-raises unexpected errors after the 500 response was sent.
    return next(m['status'] for m in messages if m['type']=='http.response.start')


@pytest.mark.parametrize('separator',[' ','\u0085','\r\n'])
def test_error_handlers_escape_path_and_detail(caplog,separator):
    app=FastAPI()
    @app.get('/boom/{value}')
    async def boom(value):
        raise RuntimeError('unexpected')
    @app.get('/teapot/{value}')
    async def teapot(value):
        raise HTTPException(status_code=418,detail='detail'+separator+'FAKE LINE')
    register_error_handlers(app)
    with caplog.at_level(logging.ERROR):
        assert call(app,'/boom/a'+separator+'FAKE LINE')==500
        assert call(app,'/teapot/a'+separator+'FAKE LINE')==500
    entries=[r.getMessage() for r in caplog.records if r.name.endswith('errors.handlers')]
    assert len(entries)==2
    assert all(separator not in s and '\n' not in s and '\r' not in s for s in entries)
    assert all('FAKE LINE' in s for s in entries)
