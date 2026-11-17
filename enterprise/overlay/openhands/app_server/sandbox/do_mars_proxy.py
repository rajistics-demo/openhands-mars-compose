"""Authenticated Agent Server bridge on a separate runtime origin.

Only port 8000 is exposed. Preview ports need a separate capability design.
A stored session-key hash is checked before opening any provider connection;
the gateway never loads the encrypted plaintext key or forwards app cookies.
Run uvicorn with access logs disabled because legacy WS query keys are secrets.
"""

import asyncio
import hmac
import json
import os
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode

import aiohttp
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect

from openhands.app_server.sandbox.do_mars_client import DO_MARS_BACKEND, DoMarsClient
from openhands.app_server.sandbox.do_mars_credentials import valid_session_key
from openhands.app_server.sandbox.do_mars_diagnostics import (
    error_class,
    log_diagnostic,
)
from openhands.app_server.sandbox.do_mars_secrets import read_secret
from openhands.app_server.sandbox.do_mars_tunnel import tcp_forward
from openhands.app_server.sandbox.sandbox_store import (
    LifecycleState,
    StoredSandbox,
    hash_session_api_key,
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
APP_ORIGIN = os.environ.get('DO_MARS_APP_ORIGIN', '')
if APP_ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[APP_ORIGIN],
        allow_methods=['*'],
        allow_headers=['Content-Type', 'X-Session-API-Key'],
        allow_credentials=False,
    )
HOP_HEADERS = {
    'host',
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailer',
    'transfer-encoding',
    'upgrade',
    'content-length',
    'set-cookie',
}
STRIPPED_DOWNSTREAM_HEADERS = HOP_HEADERS | {
    'cookie',
    'authorization',
    'x-init-api-key',
}


@dataclass
class GatewayRow:
    provider_resource_id: str
    session_api_key_hash: str
    lifecycle_state: str


async def _load_row(sandbox_id: str) -> GatewayRow | None:
    """Read only the id mapping, key hash and settled lifecycle state.

    A plain MVCC read, never a row lock: the lifecycle worker probes idle time
    through this gateway while it holds the row ``FOR UPDATE``, so any lock
    here would make that probe conflict with itself. The gateway never writes
    the row.
    """
    from storage.database import a_session_maker

    async with a_session_maker() as db:
        row = (
            await db.execute(
                select(
                    StoredSandbox.provider_resource_id,
                    StoredSandbox.session_api_key_hash,
                    StoredSandbox.lifecycle_state,
                ).where(
                    StoredSandbox.id == sandbox_id,
                    StoredSandbox.backend == DO_MARS_BACKEND,
                )
            )
        ).first()
    if not row or not row[0] or not row[1]:
        return None
    return GatewayRow(row[0], row[1], row[2])


async def authorize(sandbox_id: str, port: int, key: str | None) -> GatewayRow:
    """Authenticate the caller, then allow a dial only to a RUNNING row.

    The key is checked before any state is revealed. A settled paused row is
    never dialed: MARS port-forwarding can wake a paused VM, and only an
    explicit Enterprise resume may do that. The check is not atomic with a
    concurrent pause: a dial authorized just before the pause commits can
    still reach the provider.
    """
    if port != 8000:
        raise HTTPException(404)
    row = await _load_row(sandbox_id)
    if row is None:
        raise HTTPException(404)
    if not key or not hmac.compare_digest(
        hash_session_api_key(key), row.session_api_key_hash
    ):
        raise HTTPException(401, 'Invalid or missing session key')
    if not valid_session_key(key, sandbox_id, row.provider_resource_id):
        raise HTTPException(401, 'Expired or incorrectly scoped session key')
    if row.lifecycle_state != LifecycleState.RUNNING:
        raise HTTPException(409, 'Sandbox is not running; resume it in Enterprise')
    return row


def _safe_path(path: str) -> None:
    decoded = path
    for _ in range(8):
        value = unquote(decoded)
        if value == decoded:
            break
        decoded = value
    # Also reject normalized traversal forms and encoded separators. Init is
    # exclusively server-side, authenticated with the bootstrap key.
    parts = [p for p in decoded.replace('\\', '/').split('/') if p]
    if (
        any(c in decoded for c in '%?#')
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in decoded)
        or any(p in ('.', '..') for p in parts)
        or [p.lower() for p in parts[:2]] == ['api', 'init']
    ):
        raise HTTPException(403)


def forwarded_headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    blocked = set(STRIPPED_DOWNSTREAM_HEADERS)
    for key, value in raw:
        if key.lower() == b'connection':
            blocked.update(
                part.strip().lower() for part in value.decode('latin1').split(',')
            )
    return {
        key.decode('latin1'): value.decode('latin1')
        for key, value in raw
        if key.decode('latin1').lower() not in blocked
        and not key.decode('latin1').lower().startswith('sec-websocket-')
    }


def _tunnel_url(row: GatewayRow) -> str:
    # URL validation is shared with the control client; no token needed here.
    return DoMarsClient.port_forward_url(row.provider_resource_id, 8000)


@app.get('/alive')
async def alive():
    return {'status': 'ok'}


@app.api_route(
    '/{sandbox_id}/{port}/{path:path}',
    methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'],
)
async def proxy_http(request: Request, sandbox_id: str, port: int, path: str):
    _safe_path(path)
    row = await authorize(sandbox_id, port, request.headers.get('x-session-api-key'))
    tunnel = tcp_forward(_tunnel_url(row), read_secret('DO_MARS_TEAM_TOKEN'))
    origin = await tunnel.__aenter__()
    client = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=20), auto_decompress=False
    )
    response: aiohttp.ClientResponse | None = None
    closed = False

    async def close():
        nonlocal closed
        if closed:
            return
        closed = True
        if response is not None:
            response.close()
        await client.close()
        await tunnel.__aexit__(None, None, None)

    try:
        query = request.scope['query_string'].decode('ascii')
        target = origin + '/' + path + ('?' + query if query else '')
        response = await client.request(
            request.method,
            target,
            data=request.stream(),
            headers=forwarded_headers(request.scope['headers']),
            allow_redirects=False,
        )
        if response.status >= 400:
            log_diagnostic(
                'do_mars.gateway_guest_response',
                operation='gateway.http',
                method=request.method,
                status_code=response.status,
            )

        async def chunks():
            try:
                async for chunk in response.content.iter_chunked(65536):
                    yield chunk
            finally:
                await close()

        return StreamingResponse(
            chunks(),
            status_code=response.status,
            headers={
                k: v
                for k, v in response.headers.items()
                if k.lower() not in HOP_HEADERS
            },
            background=BackgroundTask(close),
        )
    except (aiohttp.ClientError, TimeoutError) as exc:
        log_diagnostic(
            'do_mars.gateway_guest_transport_error',
            operation='gateway.http',
            method=request.method,
            error_class=error_class(exc),
        )
        await close()
        raise HTTPException(502, 'MARS guest transport unavailable') from None
    except BaseException:
        await close()
        raise


@app.websocket('/{sandbox_id}/{port}/{path:path}')
async def proxy_websocket(websocket: WebSocket, sandbox_id: str, port: int, path: str):
    accepted = False
    try:
        _safe_path(path)
        if port != 8000:
            raise HTTPException(404)
        origin_header = websocket.headers.get('origin')
        if origin_header and origin_header != APP_ORIGIN:
            raise HTTPException(403)
        header_key = websocket.headers.get('x-session-api-key')
        query_key = websocket.query_params.get('session_api_key')
        if header_key and query_key and not hmac.compare_digest(header_key, query_key):
            raise HTTPException(401)
        supplied_key = header_key or query_key
        if not supplied_key:
            # Current SDK authenticates in its first WS frame. Accept only
            # the outer socket, with no provider connection until validated.
            await websocket.accept()
            accepted = True
            frame = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            if len(frame) > 8192:
                raise HTTPException(401)
            auth = json.loads(frame)
            if not isinstance(auth, dict) or auth.get('type') != 'auth':
                raise HTTPException(401)
            supplied_key = auth.get('session_api_key')
            if not isinstance(supplied_key, str):
                raise HTTPException(401)
        row = await authorize(sandbox_id, port, supplied_key)
    except (HTTPException, ValueError, TimeoutError, WebSocketDisconnect):
        await websocket.close(code=1008)
        return
    try:
        async with tcp_forward(
            _tunnel_url(row), read_secret('DO_MARS_TEAM_TOKEN')
        ) as origin:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=20)
            ) as client:
                query = urlencode(
                    [
                        (k, v)
                        for k, v in parse_qsl(
                            websocket.scope['query_string'].decode('ascii'),
                            keep_blank_values=True,
                        )
                        if k != 'session_api_key'
                    ]
                )
                target = origin + '/' + path + ('?' + query if query else '')
                headers = forwarded_headers(websocket.scope['headers'])
                headers['X-Session-API-Key'] = supplied_key or ''
                protocols = [
                    x.strip()
                    for x in websocket.headers.get('sec-websocket-protocol', '').split(
                        ','
                    )
                    if x.strip()
                ]
                async with client.ws_connect(
                    target,
                    headers=headers,
                    protocols=protocols,
                    max_msg_size=16 * 1024 * 1024,
                ) as ws:
                    if not accepted:
                        await websocket.accept(subprotocol=ws.protocol)

                    async def send():
                        while True:
                            message = await websocket.receive()
                            if message['type'] == 'websocket.disconnect':
                                break
                            if message.get('text') is not None:
                                await ws.send_str(message['text'])
                            elif message.get('bytes') is not None:
                                await ws.send_bytes(message['bytes'])

                    async def receive():
                        async for message in ws:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                await websocket.send_text(message.data)
                            elif message.type == aiohttp.WSMsgType.BINARY:
                                await websocket.send_bytes(message.data)

                    tasks = [
                        asyncio.create_task(send()),
                        asyncio.create_task(receive()),
                    ]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
    except aiohttp.WSServerHandshakeError as exc:
        log_diagnostic(
            'do_mars.gateway_guest_ws_failed',
            operation='gateway.ws',
            phase='handshake',
            handshake_status=exc.status,
            error_class=error_class(exc),
        )
    except (aiohttp.ClientError, TimeoutError) as exc:
        log_diagnostic(
            'do_mars.gateway_guest_ws_failed',
            operation='gateway.ws',
            phase='stream',
            error_class=error_class(exc),
        )
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass
