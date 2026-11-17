"""Run-key expiry on the internal service API key endpoint."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from server.routes.service import (
    CreateUserApiKeyRequest,
    get_or_create_api_key_for_user,
)


def test_expiry_defaults_to_none():
    assert CreateUserApiKeyRequest(name='automation').expires_at is None


def test_expiry_is_normalised_to_utc():
    local = timezone(timedelta(hours=-6))
    deadline = datetime.now(local) + timedelta(hours=1)
    parsed = CreateUserApiKeyRequest(name='run', expires_at=deadline).expires_at
    assert parsed == deadline
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    'deadline',
    [
        datetime.now(timezone.utc) - timedelta(seconds=1),
        datetime.now(timezone.utc) + timedelta(days=1, minutes=1),
        (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None),
    ],
    ids=['past', 'over-24h', 'naive'],
)
def test_invalid_expiry_rejected(deadline):
    with pytest.raises(ValidationError):
        CreateUserApiKeyRequest(name='run', expires_at=deadline)


@pytest.mark.asyncio
@pytest.mark.parametrize('with_expiry', [False, True])
async def test_route_preserves_legacy_store_call_shape(with_expiry):
    user_id = str(uuid.uuid4())
    org_id = uuid.uuid4()
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    request = CreateUserApiKeyRequest(
        name='run', expires_at=deadline if with_expiry else None
    )
    store = MagicMock()
    store.get_or_create_system_api_key = AsyncMock(return_value='sk-oh-x')
    user = MagicMock(is_disabled=False, deletion_pending=False)
    with (
        patch(
            'server.routes.service.validate_service_api_key',
            AsyncMock(return_value='automations'),
        ),
        patch(
            'server.routes.service.UserStore.get_user_by_id',
            AsyncMock(return_value=user),
        ),
        patch(
            'server.routes.service.OrgMemberStore.get_org_member',
            AsyncMock(return_value=MagicMock()),
        ),
        patch('server.routes.service.ApiKeyStore.get_instance', return_value=store),
    ):
        response = await get_or_create_api_key_for_user(
            user_id, org_id, request, x_service_api_key='k'
        )
    assert response.key == 'sk-oh-x'
    expected = {'user_id': user_id, 'org_id': org_id, 'name': 'run'}
    if with_expiry:
        expected['expires_at'] = deadline
    store.get_or_create_system_api_key.assert_awaited_once_with(**expected)
