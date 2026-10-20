import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import types

from app.telegram import TelegramService
from test_telegram import Client


class SharedClient(Client):
    def __init__(self):
        super().__init__([])
        self.next_id = 100

    async def __call__(self, request):
        self.requests.append(request)
        self.next_id += 100
        return types.UpdateShortSentMessage(id=self.next_id, pts=1, pts_count=1,
                                           date=datetime.now(timezone.utc), out=True)

    async def emit(self, reference, *, message_id=1000, text="ok", buttons=None):
        message = SimpleNamespace(id=message_id, reply_to_msg_id=reference, raw_text=text, buttons=buttons)
        for handler in self.handlers[:]:
            await handler(SimpleNamespace(chat_id=44, out=False, message=message))


def prepare():
    service = TelegramService(SimpleNamespace(decrypt=lambda value: {}))
    client = SharedClient()
    factory_calls = []
    def create(secret):
        factory_calls.append(1)
        return client
    service.client = create
    job = dict(target="@example_bot", message="/checkin", button="", success_text="ok", reply_timeout=.12)
    return service, client, factory_calls, job


async def start(service, job, account=None):
    acknowledged = asyncio.Event()
    task = asyncio.create_task(service.send(account or {"id": 1, "secret": "mock"}, job,
                                            on_sent=lambda detail: acknowledged.set()))
    await asyncio.wait_for(acknowledged.wait(), 1)
    return task


def test_overlapping_same_account_reuses_connection_and_correlates_explicit_replies():
    async def run():
        service, client, calls, job = prepare()
        first = await start(service, job)
        second = await start(service, job)
        assert len(calls) == 1 and len(client.handlers) == 2
        await client.emit(300)
        assert (await second)[0] == "confirmed"
        assert not first.done() and client.disconnect.await_count == 0
        await client.emit(200)
        assert (await first)[0] == "confirmed"
        assert client.handlers == [] and not service.clients and not service.active
        client.disconnect.assert_awaited_once()
    asyncio.run(run())


def test_overlapping_unthreaded_reply_cannot_confirm_either_command():
    async def run():
        service, client, _, job = prepare()
        first = await start(service, job)
        second = await start(service, job)
        await client.emit(None)
        results = await asyncio.gather(first, second)
        assert all(status == "reply_timeout" and "无法可靠归属" in detail for status, detail in results)
        assert len(client.requests) == 2
    asyncio.run(run())


def test_private_reply_without_reference_still_works_for_one_task():
    async def run():
        service, client, _, job = prepare()
        task = await start(service, job)
        await client.emit(None)
        assert (await task)[0] == "confirmed"
    asyncio.run(run())


def test_private_reply_with_wrong_reference_is_ignored_even_without_overlap():
    async def run():
        service, client, _, job = prepare()
        task = await start(service, job)
        await client.emit(999)
        assert (await task)[0] == "reply_timeout"
    asyncio.run(run())


def test_no_reply_job_still_prevents_ambiguous_confirmation_of_prior_command():
    async def run():
        service, client, _, job = prepare()
        first = await start(service, job)
        second = await start(service, {**job, "success_text": ""})
        assert (await second)[0] == "sent"
        await client.emit(None)
        assert (await first)[0] == "reply_timeout"
        client.disconnect.assert_awaited_once()
    asyncio.run(run())


def test_finishing_one_waiter_does_not_disconnect_or_cancel_other_waiter():
    async def run():
        service, client, _, job = prepare()
        first = await start(service, job)
        second = await start(service, {**job, "reply_timeout": .25})
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert client.disconnect.await_count == 0 and len(client.handlers) == 1
        await client.emit(300)
        assert (await second)[0] == "confirmed"
        client.disconnect.assert_awaited_once()
    asyncio.run(run())


def test_overlapping_button_reply_is_only_clicked_by_its_owner():
    async def run():
        service, client, _, job = prepare()
        job = {**job, "button": "签到", "success_text": ""}
        first = await start(service, job)
        second = await start(service, job)
        click = AsyncMock()
        button = SimpleNamespace(text="签到",
                                 button=types.KeyboardInlineButton(text="签到", type=types.InlineButtonTypeCallback(data=b"ok")),
                                 click=click)
        await client.emit(300, buttons=[[button]])
        assert (await second)[0] == "sent"
        assert not first.done() and click.await_count == 1
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    asyncio.run(run())
