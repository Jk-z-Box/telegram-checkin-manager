import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from telethon import errors, functions, types

from app.main import create_app
from app.models import AccountFinish, AccountStart
from app.store import Store, initialize
from app.telegram import LoginFailure, TelegramService, friendly_error


def account():
    # Fictional number and credentials. No test in this file makes network calls.
    return AccountStart(name="Test account", phone="+12025550123", api_id=12345, api_hash="a"*32)


def sent_code():
    return types.auth.SentCode(type=types.auth.SentCodeTypeApp(length=5),
                               phone_code_hash="test-only-code-hash")


class ScriptedSender:
    """Exercise Telethon's actual request/retry/DC handling, replacing only transport."""
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def send(self, request, ordered=False):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        future = asyncio.get_running_loop().create_future()
        result = outcome(request) if callable(outcome) else outcome
        if isinstance(result, Exception):
            future.set_exception(result)
        else:
            future.set_result(result)
        return future


def wire_transport(client, outcomes):
    sender = ScriptedSender(outcomes)
    client._sender = sender
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client._switch_dc = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=False)
    return sender


def service_with_transport(outcomes, store=None):
    service = TelegramService(store)
    factory = service.client
    created = []

    def create(secret, *, login=False):
        client = factory(secret, login=login)
        sender = wire_transport(client, outcomes)
        created.append((client, sender))
        return client

    service.client = create
    return service, created


def test_send_code_continues_after_phone_dc_migration():
    async def scenario():
        service, clients = service_with_transport([
            lambda request: errors.PhoneMigrateError(request=request, capture=1), sent_code()])
        result = await service.start(account())
        client, sender = clients[0]
        assert len(sender.requests) == 2
        assert all(isinstance(r, functions.auth.SendCodeRequest) for r in sender.requests)
        client._switch_dc.assert_awaited_once_with(1)
        assert service.pending[result["ticket"]]["hash"] == "test-only-code-hash"
        assert not service.active
        await service.close()
        assert not service.pending and client.disconnect.await_count == 1
    asyncio.run(scenario())


def test_login_retries_are_bounded_and_keep_actual_error_type():
    async def scenario():
        service, clients = service_with_transport(
            [lambda request: errors.PhoneMigrateError(request=request, capture=1)] * 4)
        with pytest.raises(LoginFailure) as result:
            await service.start(account())
        client, sender = clients[0]
        assert len(sender.requests) == 4
        assert result.value.stage == "send_code" and result.value.error_type == "PhoneMigrateError"
        assert "ValueError" not in str(result.value)
        assert client.disconnect.await_count == 1
        assert not service.pending and not service.active
    asyncio.run(scenario())


def test_login_transient_error_can_recover():
    async def scenario():
        service, clients = service_with_transport([
            lambda request: errors.RpcCallFailError(request=request), sent_code()])
        result = await service.start(account())
        assert result["ticket"] in service.pending
        assert len(clients[0][1].requests) == 2
        await service.close()
    asyncio.run(scenario())


def test_message_rpc_still_does_not_retry_on_server_failure():
    async def scenario():
        service = TelegramService(None)
        client = service.client(account().model_dump())
        sender = wire_transport(client, [lambda request: errors.RpcCallFailError(request=request)])
        with pytest.raises(errors.RpcCallFailError):
            await client(functions.messages.SendMessageRequest(
                peer=types.InputPeerSelf(), message="/checkin", random_id=42))
        assert len(sender.requests) == 1
    asyncio.run(scenario())


def test_floodwait_is_returned_without_retry_or_claiming_account_pause():
    async def scenario():
        service, clients = service_with_transport([
            lambda request: errors.FloodWaitError(request=request, capture=60)])
        with pytest.raises(LoginFailure) as result:
            await service.start(account())
        assert len(clients[0][1].requests) == 1
        assert "60 秒" in str(result.value) and "已暂停" not in str(result.value)
        assert not service.pending
    asyncio.run(scenario())


def test_two_factor_flow_saves_session_not_codes_or_password(tmp_path):
    initialize(tmp_path, "admin", "test-only-admin-password")
    store = Store(tmp_path)

    async def scenario():
        service, clients = service_with_transport([sent_code()], store)
        result = await service.start(account())
        client, _ = clients[0]
        client.sign_in = AsyncMock(side_effect=[errors.SessionPasswordNeededError(request=None), None])
        client.get_me = AsyncMock(return_value=SimpleNamespace(id=42))
        client.session.save = lambda: "test-saved-session"
        first = await service.finish(AccountFinish(ticket=result["ticket"], code="12345"))
        assert first == {"need_password": True}
        assert not service.pending[result["ticket"]]["busy"]
        second = await service.finish(AccountFinish(ticket=result["ticket"], password="private-2fa-test"))
        assert second == {"ok": True} and not service.pending
        client.sign_in.assert_awaited_with(password="private-2fa-test")
        secret = store.decrypt(store.one("SELECT secret FROM accounts")["secret"])
        assert secret["session"] == "test-saved-session"
        assert "password" not in secret and "code" not in secret
    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_incorrect_code_can_retry_without_losing_pending_login():
    async def scenario():
        service, clients = service_with_transport([sent_code()])
        result = await service.start(account())
        client, _ = clients[0]
        client.sign_in = AsyncMock(side_effect=errors.PhoneCodeInvalidError(request=None))
        with pytest.raises(LoginFailure) as error:
            await service.finish(AccountFinish(ticket=result["ticket"], code="00000"))
        assert error.value.stage == "verify_code"
        assert result["ticket"] in service.pending and not service.pending[result["ticket"]]["busy"]
        await service.close()
    asyncio.run(scenario())


def test_login_error_response_and_logs_never_echo_sensitive_exception(tmp_path, caplog):
    initialize(tmp_path, "admin", "test-only-admin-password")
    marker = "private-phone-api-hash-session-marker"

    def factory(store):
        service = TelegramService(store)
        original = service.client

        def create(secret, *, login=False):
            client = original(secret, login=login)
            wire_transport(client, [])
            client.connect = AsyncMock(side_effect=ValueError(marker))
            return client

        service.client = create
        return service

    with TestClient(create_app(tmp_path, service_factory=factory, scheduler=False)) as browser:
        auth = browser.post("/api/login", json={"username": "admin", "password": "test-only-admin-password"})
        browser.headers["X-CSRF-Token"] = auth.json()["csrf"]
        result = browser.post("/api/accounts/start", json=account().model_dump())
        assert result.status_code == 400
        detail = result.json()["detail"]
        assert "连接 Telegram" in detail and "ValueError" in detail and "诊断编号" in detail
        assert "目标对话" not in detail
        assert "stage=connect error_type=ValueError" in caplog.text
        for value in (marker, account().api_hash, account().phone, "test-only-admin-password"):
            assert value not in detail and value not in caplog.text
        assert not browser.app.state.telegram.pending


def test_api_credentials_error_has_actionable_message():
    message = friendly_error(errors.ApiIdInvalidError(request=None), login=True)
    assert "API ID 或 API Hash" in message and "目标对话" not in message


def test_cancelled_login_disconnects_without_pending_session():
    async def scenario():
        service, clients = service_with_transport([])
        original = service.client
        connected = asyncio.Event()

        async def connect():
            connected.set()
            await asyncio.Event().wait()

        def create(secret, *, login=False):
            client = original(secret, login=login)
            client.connect = connect
            return client

        service.client = create
        task = asyncio.create_task(service.start(account()))
        await connected.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clients[0][0].disconnect.await_count == 1
        assert not service.active and not service.pending
    asyncio.run(scenario())
