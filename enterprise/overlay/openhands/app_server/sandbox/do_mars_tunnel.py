"""HTTP and WebSocket clients over MARS' raw TCP port-forward transport.

A loopback listener adapts one local TCP connection to one DO WebSocket. HTTP
framing, chunking and inner WebSocket frames are handled by aiohttp, rather
than interpreting partial TCP packets as complete HTTP messages.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import AsyncIterator

import aiohttp

from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.do_mars_diagnostics import (
    error_class,
    log_diagnostic,
)

DEFAULT_TUNNEL_TIMEOUT_SECONDS = 30.0


@dataclass
class TunnelHttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@asynccontextmanager
async def tcp_forward(websocket_url: str, token: str) -> AsyncIterator[str]:
    """Expose a loopback endpoint for exactly one TCP connection.

    The listener stops accepting after the first client, so each forward is
    one local TCP connection to one provider WebSocket.
    """
    tasks: set[asyncio.Task] = set()
    accepted: list[asyncio.Task] = []
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=20)
    ) as upstream:

        async def connection(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ):
            task = asyncio.current_task()
            assert task is not None
            if accepted:
                # One loopback connection per forward: a second local client
                # must never ride this caller's authenticated provider dial.
                writer.close()
                return
            accepted.append(task)
            server.close()
            tasks.add(task)
            phase = 'handshake'
            try:
                async with upstream.ws_connect(
                    websocket_url,
                    headers={'Authorization': f'Bearer {token}'},
                    max_msg_size=16 * 1024 * 1024,
                    autoclose=True,
                    autoping=True,
                ) as ws:
                    phase = 'stream'

                    async def send():
                        while chunk := await reader.read(65536):
                            await ws.send_bytes(chunk)

                    async def receive():
                        async for message in ws:
                            if message.type == aiohttp.WSMsgType.BINARY:
                                writer.write(message.data)
                                await writer.drain()
                            elif message.type in (
                                aiohttp.WSMsgType.TEXT,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                # Type only: a provider text frame may carry
                                # an error message, never logged verbatim.
                                log_diagnostic(
                                    'do_mars.tunnel_ws_closed',
                                    phase='provider_frame',
                                    frame_type=message.type.name,
                                )
                                break

                    pumps = [
                        asyncio.create_task(send()),
                        asyncio.create_task(receive()),
                    ]
                    try:
                        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for pump in pumps:
                            pump.cancel()
                        await asyncio.gather(*pumps, return_exceptions=True)
                    if ws.close_code not in (None, 1000):
                        log_diagnostic(
                            'do_mars.tunnel_ws_closed',
                            phase='stream',
                            close_code=ws.close_code,
                        )
            except aiohttp.WSServerHandshakeError as exc:
                log_diagnostic(
                    'do_mars.tunnel_ws_failed',
                    phase='handshake',
                    handshake_status=exc.status,
                    error_class=error_class(exc),
                )
            except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                log_diagnostic(
                    'do_mars.tunnel_ws_failed',
                    phase=phase,
                    error_class=error_class(exc),
                )
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(connection, '127.0.0.1', 0)
        assert server.sockets
        port = server.sockets[0].getsockname()[1]
        try:
            yield f'http://127.0.0.1:{port}'
        finally:
            server.close()
            # Cancel first: on Python 3.12+ wait_closed() also waits for
            # active connections, which would otherwise hang on a stuck pump.
            active = list(tasks)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            await server.wait_closed()


async def tunnel_http_request(
    websocket_url: str,
    *,
    do_bearer_token: str,
    http_host: str,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes = b'',
    timeout_seconds: float = DEFAULT_TUNNEL_TIMEOUT_SECONDS,
) -> TunnelHttpResponse:
    """Bounded HTTP request; only idempotent readiness GETs are retried.

    A POST whose reply was lost must be reconciled by its caller, never replayed
    here. Redirects are disabled so session credentials stay in this guest.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    attempts = 0
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            log_diagnostic(
                'do_mars.tunnel_request_failed',
                method=method,
                reason='confirmation_timeout',
                attempts=attempts,
            )
            raise SandboxError('MARS guest request timed out', status_code=502)
        attempts += 1
        try:
            async with tcp_forward(websocket_url, do_bearer_token) as origin:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=remaining),
                    auto_decompress=False,
                ) as client:
                    async with client.request(
                        method,
                        origin + path,
                        headers={**headers, 'Host': http_host},
                        data=body or None,
                        allow_redirects=False,
                    ) as response:
                        content = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            content.extend(chunk)
                            if len(content) > 4 * 1024 * 1024:
                                raise SandboxError('MARS guest response exceeds limit')
                        return TunnelHttpResponse(
                            response.status, dict(response.headers), bytes(content)
                        )
        except (aiohttp.ClientError, TimeoutError) as exc:
            if method != 'GET' or asyncio.get_running_loop().time() >= deadline:
                log_diagnostic(
                    'do_mars.tunnel_request_failed',
                    method=method,
                    reason='transport_error',
                    error_class=error_class(exc),
                    attempts=attempts,
                )
                raise SandboxError(
                    'MARS guest transport failed', status_code=502
                ) from exc
            await asyncio.sleep(min(1.0, max(0.0, remaining)))
