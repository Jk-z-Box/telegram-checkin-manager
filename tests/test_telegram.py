import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from telethon import functions, types
from telethon.tl.types import KeyboardInlineButton, InlineButtonTypeCallback, InlineButtonTypeUrl

from app.telegram import CheckinError, TelegramService


class Client:
    def __init__(self, replies, private=True):
        self.replies = replies
        self.handlers = []
        self.private = private
        self.requests = []
        self.reply_delay = .001
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.is_user_authorized = AsyncMock(return_value=True)
        self.get_input_entity = AsyncMock(return_value=types.InputPeerUser(user_id=44, access_hash=123)
                                         if private else types.InputPeerChat(chat_id=44))
        from telethon.tl.types import User, Chat
        self.get_entity = AsyncMock(return_value=User(id=44,first_name="Bot") if private else Chat(
            id=44,title="Group",photo=None,participants_count=1,date=None,version=1))

    def add_event_handler(self, handler, event):
        if handler not in self.handlers:
            self.handlers.append(handler)

    def remove_event_handler(self, handler):
        self.handlers.remove(handler)

    async def __call__(self, request):
        assert isinstance(request, functions.messages.SendMessageRequest)
        assert request.entities is None
        self.requests.append(request)
        async def emit():
            for reply in self.replies:
                await asyncio.sleep(self.reply_delay)
                event = SimpleNamespace(chat_id=44 if self.private else -44,out=False,message=reply)
                for handler in self.handlers[:]:
                    await handler(event)
        asyncio.create_task(emit())
        return types.UpdateShortSentMessage(id=100, pts=1, pts_count=1,
                                           date=datetime.now(timezone.utc), out=True)


def reply(text="",buttons=None,reply_to=100):
    return SimpleNamespace(id=101,reply_to_msg_id=reply_to,raw_text=text,buttons=buttons)


def execute(replies,button="",success="",private=True):
    service = TelegramService(SimpleNamespace(decrypt=lambda value: {}))
    client = Client(replies,private)
    service.client = lambda secret: client
    job = {"target":"@bot","message":"/checkin","button":button,"success_text":success,"reply_timeout":.04}
    return service, client, job


def test_send_only_does_not_claim_confirmation():
    service,client,job = execute([])
    result = asyncio.run(service.send({"secret":""},job))
    assert result[0] == "sent"
    assert client.disconnect.await_count == 1
    client.get_entity.assert_not_awaited()


def test_group_ignores_unrelated_success():
    service,client,job = execute([reply("签到成功",reply_to=99)],success="签到成功",private=False)
    assert asyncio.run(service.send({"secret":""},job))[0] == "reply_timeout"


def test_matching_reply_confirms():
    service,client,job = execute([reply("签到成功，积分 +1")],success="签到成功")
    assert asyncio.run(service.send({"secret":""},job))[0] == "confirmed"


def test_callback_then_confirmation():
    button = SimpleNamespace(text="签到",button=KeyboardInlineButton(text="签到",type=InlineButtonTypeCallback(data=b"check")),click=AsyncMock())
    service,client,job = execute([reply(buttons=[[button]]),reply("签到成功")],button="签到",success="签到成功")
    assert asyncio.run(service.send({"secret":""},job))[0] == "confirmed"
    assert button.click.await_count == 1


def test_does_not_open_url_button():
    button = SimpleNamespace(text="签到",button=KeyboardInlineButton(text="签到",type=InlineButtonTypeUrl(url="https://example.com")),click=AsyncMock())
    service,client,job = execute([reply(buttons=[[button]])],button="签到")
    status, detail = asyncio.run(service.send({"secret":""},job))
    assert status == "unconfirmed" and "已拒绝点击" in detail
    assert button.click.await_count == 0
    assert client.disconnect.await_count == 1
