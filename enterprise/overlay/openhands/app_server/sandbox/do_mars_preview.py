"""Browser previews live on a dedicated HTTPS origin for each MARS sandbox."""

import hmac
import os
import re
import time
from urllib.parse import urlsplit

import jwt

from openhands.app_server.sandbox.do_mars_secrets import read_secret

PREVIEW_PORT = 8011
PREVIEW_COOKIE = '__Host-openhands-mars-preview'
PREVIEW_SESSION_PATH = '/.well-known/openhands-preview-session'
PREVIEW_TTL = 600
PREVIEW_AUDIENCE = 'openhands-mars-browser-preview'
LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')


def preview_domain(app_origin: str) -> str | None:
    if os.getenv('DO_MARS_ENABLE_BROWSER_PREVIEW') != 'true':
        return None
    url = urlsplit(app_origin)
    host = url.hostname or ''
    if (
        app_origin != f'https://{host}'
        or '.' not in host
        or not any(character.isalpha() for character in host)
        or not all(LABEL.fullmatch(label) for label in host.split('.'))
    ):
        raise ValueError('MARS browser preview requires a canonical HTTPS app origin')
    return f'preview.{host}'


def preview_origin(domain: str, sandbox_id: str) -> str:
    if not LABEL.fullmatch(sandbox_id):
        raise ValueError('Invalid MARS preview sandbox ID')
    return f'https://{sandbox_id}.{domain}'


def _cookie_key(session_key_hash: str) -> bytes:
    return hmac.digest(
        read_secret('DO_MARS_SESSION_SIGNING_KEY').encode(),
        f'{PREVIEW_AUDIENCE}:{session_key_hash}'.encode(),
        'sha256',
    )


def issue_preview_cookie(
    sandbox_id: str, provider_id: str, session_key_hash: str
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            'aud': PREVIEW_AUDIENCE,
            'sub': sandbox_id,
            'provider_id': provider_id,
            'port': PREVIEW_PORT,
            'iat': now,
            'exp': now + PREVIEW_TTL,
        },
        _cookie_key(session_key_hash),
        algorithm='HS256',
    )


def valid_preview_cookie(
    token: str, sandbox_id: str, provider_id: str, session_key_hash: str
) -> bool:
    try:
        claims = jwt.decode(
            token,
            _cookie_key(session_key_hash),
            algorithms=['HS256'],
            audience=PREVIEW_AUDIENCE,
            options={'require': ['iat', 'exp', 'aud', 'sub', 'provider_id', 'port']},
        )
    except jwt.PyJWTError:
        return False
    return (
        claims['sub'] == sandbox_id
        and claims['provider_id'] == provider_id
        and claims['port'] == PREVIEW_PORT
    )
