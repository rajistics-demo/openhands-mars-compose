"""Short-lived, session-bound credentials; never a DigitalOcean team token."""

import os
import time

import jwt

from openhands.app_server.sandbox.do_mars_secrets import read_secret

ISSUER = 'openhands-enterprise-mars'
AUDIENCE = 'mars-agent-server-and-callbacks'


def issue_session_key(sandbox_id: str, provider_id: str) -> str:
    now = int(time.time())
    ttl = min(86400, max(300, int(os.getenv('DO_MARS_SESSION_TTL_SECONDS', '28800'))))
    return jwt.encode(
        {
            'iss': ISSUER,
            'aud': AUDIENCE,
            'sub': sandbox_id,
            'provider_id': provider_id,
            'iat': now,
            'exp': now + ttl,
        },
        read_secret('DO_MARS_SESSION_SIGNING_KEY'),
        algorithm='HS256',
    )


def valid_session_key(token: str, sandbox_id: str, provider_id: str) -> bool:
    try:
        claims = jwt.decode(
            token,
            read_secret('DO_MARS_SESSION_SIGNING_KEY'),
            algorithms=['HS256'],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={'require': ['exp', 'iat', 'sub', 'provider_id']},
        )
        return claims['sub'] == sandbox_id and claims['provider_id'] == provider_id
    except jwt.PyJWTError:
        return False
