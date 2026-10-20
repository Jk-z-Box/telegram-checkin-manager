import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from app.telegram import TelegramService, execution_timeout, sent_message_id
from test_telegram import Client, execute, reply


class ScriptedClient(Client):
    def __init__(self, outcomes):
        super().__init__([])
        self.outcomes = list(outcomes)
        self.attempts = []

    async def __call__(self, request):
        self.attempts.append(request)
        outcome = self.outcomes.pop(0)
        if outcome == "hang":
            await asyncio.Event().wait()
        if isinstance(outcome, Exception):
            raise outcome
        if not isinstance(outcome, str):
            return outcome
        return await super().__call__(request)


def scenario(outcomes, **settings):
    service = TelegramService(SimpleNamespace(decrypt=lambda value: {}))
    client = ScriptedClient(outcomes)
    service.client = lambda secret: client
    job = dict(target="@example_bot", message="/checkin *literal*",
               button="", success_text="", reply_timeout=.04,
               retry_enabled=True, send_timeout=.015, max_retries=2, **settings)
    return service, client, job


@pytest.fixture(autouse=True)
def quick_retry(monkeypatch):
    monkeypatch.setattr("app.telegram.RETRY_DELAY", .001)


@pytest.mark.parametrize("error", [
    errors.RpcCallFailError(request=None),
    errors.ServerError(request=None, message="test-only"),
    errors.TimedOutError(request=None, message="test-only"),
    ConnectionError("test-only"),
    OSError("test-only"),
])
def test_transient_failure_retries_same_message_identity(error):
    service, client, job = scenario([error, "ok"])
    status, detail = asyncio.run(service.send({"secret": ""}, job))
    assert status == "sent" and "重试 1 次" in detail
    assert len(client.attempts) == 2
    assert client.attempts[0] is client.attempts[1]
    assert client.attempts[0].message == job["message"]
    assert client.connect.await_count == 2
    assert client.handlers == [] and not service.active
    client.disconnect.assert_awaited_once()


def test_send_timeout_retries_and_stops_at_exact_limit():
    service, client, job = scenario(["hang"] * 3)
    status, detail = asyncio.run(service.send({"secret": ""}, job))
    assert status == "unknown"
    assert "发送尝试 3 次（重试 2 次）" in detail and "已达到最多重试次数" in detail
    assert len(client.attempts) == 3
    assert len({r.random_id for r in client.attempts}) == 1
    client.disconnect.assert_awaited_once()


def test_disabled_policy_does_not_retry():
    service, client, job = scenario([errors.RpcCallFailError(request=None)])
    job["retry_enabled"] = False
    status, detail = asyncio.run(service.send({"secret": ""}, job))
    assert status == "unknown" and "未开启失败后重试" in detail
    assert len(client.attempts) == 1


@pytest.mark.parametrize("error", [
    errors.ChatWriteForbiddenError(request=None),
    errors.AuthKeyUnregisteredError(request=None),
    errors.SessionRevokedError(request=None),
])
def test_permanent_errors_stop_without_retry(error):
    service, client, job = scenario([error])
    status, detail = asyncio.run(service.send({"secret": ""}, job))
    assert status == "failed" and "消息未发送" in detail
    assert len(client.attempts) == 1


def test_floodwait_is_propagated_for_persistent_cooldown_without_retry():
    service, client, job = scenario([errors.FloodWaitError(request=None, capture=60)])
    with pytest.raises(errors.FloodWaitError):
        asyncio.run(service.send({"secret": ""}, job))
    assert len(client.attempts) == 1
    client.disconnect.assert_awaited_once()


def test_duplicate_identity_stops_without_claiming_unsent():
    service, client, job = scenario([errors.RandomIdDuplicateError(request=None)])
    status, detail = asyncio.run(service.send({"secret": ""}, job))
    assert status == "unknown" and "核实" in detail
    assert len(client.attempts) == 1


def test_timeout_then_send_success_then_reply_timeout_never_sends_again():
    service, client, job = scenario(["hang", "ok"])
    job["success_text"] = "签到成功"
    status, detail = asyncio.run(service.send({"secret": ""}, job))
    assert status == "unconfirmed" and "消息已发送" in detail
    assert len(client.attempts) == 2


def test_reply_has_separate_timeout_even_when_send_timeout_is_shorter():
    service, client, job = execute([reply("签到成功")], success="签到成功")
    client.reply_delay = .025
    job.update(retry_enabled=True, send_timeout=.01, max_retries=2, reply_timeout=.15)
    assert asyncio.run(service.send({"secret": ""}, job))[0] == "confirmed"
    assert len(client.requests) == 1


@pytest.mark.parametrize("outcome", ["hang", "error"])
def test_button_failure_or_timeout_does_not_resend_command(outcome):
    async def click():
        if outcome == "hang":
            await asyncio.Event().wait()
        raise ConnectionError("test-only")
    button = SimpleNamespace(text="签到",
                             button=types.KeyboardInlineButton(text="签到", type=types.InlineButtonTypeCallback(data=b"ok")),
                             click=AsyncMock(side_effect=click))
    service, client, job = execute([reply(buttons=[[button]])], button="签到")
    job.update(retry_enabled=True, send_timeout=.01, max_retries=2)
    status, detail = asyncio.run(service.send({"secret": ""}, job))
    assert status == "unconfirmed" and "消息已发送" in detail
    assert len(client.requests) == 1 and button.click.await_count == 1


def test_connection_failure_is_retried_before_any_message_submission():
    service, client, job = scenario(["ok"])
    client.connect.side_effect = [ConnectionError("test-only"), None]
    assert asyncio.run(service.send({"secret": ""}, job))[0] == "sent"
    assert client.connect.await_count == 2 and len(client.attempts) == 1


def test_connection_timeout_exhaustion_is_definitely_unsent():
    service, client, job = scenario([])
    client.connect.side_effect = TimeoutError()
    status, detail = asyncio.run(service.send({"secret": ""}, job))
    assert status == "failed" and "消息未发送" in detail
    assert client.connect.await_count == 3 and client.attempts == []


def test_cancel_during_send_does_not_retry():
    async def run():
        service, client, job = scenario(["hang"])
        job["send_timeout"] = 60
        task = asyncio.create_task(service.send({"secret": ""}, job))
        while not client.attempts:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(client.attempts) == 1
        assert not service.active and client.handlers == []
        client.disconnect.assert_awaited_once()
    asyncio.run(run())


def test_response_mapping_accepts_only_matching_random_id():
    now = datetime.now(timezone.utc)
    updates = types.Updates(updates=[types.UpdateMessageID(id=456, random_id=123)], users=[], chats=[], date=now, seq=1)
    assert sent_message_id(updates, 123) == 456
    assert sent_message_id(updates, 999) is None


def test_missing_response_mapping_stops_without_retry():
    service, client, job = scenario([types.UpdatesTooLong()])
    status, detail = asyncio.run(service.send({"secret": ""}, job))
    assert status == "unknown" and "消息 ID" in detail and len(client.attempts) == 1


def test_distinct_executions_receive_distinct_random_ids():
    async def run():
        service, client, job = scenario(["ok", "ok"])
        await service.send({"secret": ""}, job)
        await service.send({"secret": ""}, job)
        assert client.attempts[0].random_id != client.attempts[1].random_id
    asyncio.run(run())


def test_real_telethon_transport_reuses_random_id_on_rpc_failure():
    """Use the pinned library's actual RPC handling, replacing only its transport."""
    async def run():
        service = TelegramService(SimpleNamespace(decrypt=lambda value: {}))
        client = service.client({"api_id": 12345, "api_hash": "a" * 32})
        requests = []
        def send(request, ordered=False):
            requests.append(request)
            future = asyncio.get_running_loop().create_future()
            if len(requests) == 1:
                future.set_exception(errors.RpcCallFailError(request=request))
            else:
                future.set_result(types.UpdateShortSentMessage(id=100, pts=1, pts_count=1,
                                                              date=datetime.now(timezone.utc), out=True))
            return future
        client._sender = SimpleNamespace(send=send)
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_input_entity = AsyncMock(return_value=types.InputPeerUser(user_id=44, access_hash=123))
        client.get_entity = AsyncMock(return_value=types.User(id=44, first_name="Mock"))
        service.client = lambda secret: client
        job = dict(target="@example_bot", message="/checkin", button="", success_text="",
                   retry_enabled=True, send_timeout=5, max_retries=1, reply_timeout=5)
        status, detail = await service.send({"secret": ""}, job)
        assert status == "sent" and "重试 1 次" in detail
        assert len(requests) == 2 and requests[0].random_id == requests[1].random_id
        assert all(isinstance(r, functions.messages.SendMessageRequest) for r in requests)
    asyncio.run(run())


def test_runner_budget_includes_every_send_attempt_and_independent_reply_window():
    job = dict(retry_enabled=True, send_timeout=300, max_retries=10, reply_timeout=120)
    assert execution_timeout(job) >= 300 * 11 + 120
    job["retry_enabled"] = False
    assert execution_timeout(job) == 90 + 120 + 15
