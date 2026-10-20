import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types, utils
from telethon.tl.custom.dialog import Dialog

from app.telegram import TelegramService


NOW = datetime.now(timezone.utc)


def entity_for(kind, access_hash=111):
    # Deliberately identical titles: a channel named "bot" is still a channel.
    name = "Mock 签到机器人"
    if kind in ("bot", "user"):
        return types.User(id=7000000123, first_name=name, bot=kind == "bot",
                          username="mock_checkin_bot", access_hash=access_hash)
    if kind == "group":
        return types.Chat(id=3456789012, title=name, photo=types.ChatPhotoEmpty(),
                          participants_count=1, date=NOW, version=1)
    return types.Channel(id=3456789012, title=name, photo=types.ChatPhotoEmpty(),
                         date=NOW, megagroup=kind == "supergroup",
                         broadcast=kind == "channel", username="mock_checkin_channel",
                         access_hash=access_hash)


class DialogClient:
    """Actual Telethon entities and dialogs, with all network calls replaced."""
    def __init__(self, entity, failure=None):
        self.entity, self.failure = entity, failure
        self.requests, self.handlers = [], []
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.is_user_authorized = AsyncMock(return_value=True)

    async def iter_dialogs(self, limit=None):
        raw = types.Dialog(peer=utils.get_peer(self.entity), top_message=0,
                           read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0,
                           unread_mentions_count=0, unread_reactions_count=0,
                           unread_poll_votes_count=0, notify_settings=types.PeerNotifySettings())
        yield Dialog(self, raw, {utils.get_peer_id(self.entity): self.entity}, None)

    def add_event_handler(self, handler, event):
        if handler not in self.handlers:
            self.handlers.append(handler)

    def remove_event_handler(self, handler):
        self.handlers.remove(handler)

    async def __call__(self, request):
        assert isinstance(request, functions.messages.SendMessageRequest)
        self.requests.append(request)
        if self.failure:
            raise self.failure
        return types.Updates(
            updates=[types.UpdateMessageID(id=101, random_id=request.random_id)],
            users=[], chats=[], date=NOW, seq=1)


def job(target):
    return dict(target=target, message="/checkin", button="", success_text="",
                reply_timeout=5, retry_enabled=True, send_timeout=5, max_retries=2)


@pytest.mark.parametrize("kind,expected_id,peer_type", [
    ("bot", "7000000123", types.InputPeerUser),
    ("user", "7000000123", types.InputPeerUser),
    ("group", "-3456789012", types.InputPeerChat),
    ("supergroup", "-1003456789012", types.InputPeerChannel),
    ("channel", "-1003456789012", types.InputPeerChannel),
])
def test_picker_to_send_preserves_typed_id_and_account_access_hash(kind, expected_id, peer_type):
    async def run():
        service = TelegramService(SimpleNamespace(decrypt=lambda value: {}))
        client = DialogClient(entity_for(kind))
        service.client = lambda secret: client
        account = {"id": 1, "secret": "mock"}
        selected = (await service.dialogs(account))[0]
        assert selected["id"] == expected_id and selected["kind"] == kind
        assert set(selected) == {"id", "name", "kind", "username"}
        assert selected["username"] == getattr(client.entity, "username", None)
        status, _ = await service.send(account, job(selected["id"]))
        assert status == "sent" and len(client.requests) == 1
        peer = client.requests[0].peer
        assert isinstance(peer, peer_type) and str(utils.get_peer_id(peer)) == expected_id
        if kind != "group":
            assert peer.access_hash == 111
        assert client.handlers == [] and not service.clients
    asyncio.run(run())


def test_same_target_resolves_separate_access_hash_for_each_account():
    async def run():
        service = TelegramService(SimpleNamespace(decrypt=lambda value: {"hash": value}))
        clients = []
        def create(secret):
            client = DialogClient(entity_for("supergroup", access_hash=secret["hash"]))
            clients.append(client)
            return client
        service.client = create
        for ident, access_hash in ((1, 111), (2, 222)):
            status, _ = await service.send({"id": ident, "secret": access_hash},
                                           job("-1003456789012"))
            assert status == "sent"
        assert [c.requests[0].peer.access_hash for c in clients] == [111, 222]
    asyncio.run(run())


def test_changing_bot_id_to_negative_never_sends_to_a_different_peer():
    async def run():
        service = TelegramService(SimpleNamespace(decrypt=lambda value: {}))
        client = DialogClient(entity_for("bot"))
        service.client = lambda secret: client
        status, detail = await service.send({"id": 1, "secret": "mock"}, job("-1007000000123"))
        assert status == "failed" and "未找到此对话 ID" in detail
        assert client.requests == []
    asyncio.run(run())


@pytest.mark.parametrize("error_type,expected", [
    (errors.PeerIdInvalidError, "目标对话无效"),
    (errors.ChannelInvalidError, "保留完整 ID"),
    (errors.ChannelPrivateError, "当前帐号无法访问"),
    (errors.UserBannedInChannelError, "发言"),
    (errors.ChatWriteForbiddenError, "没有向此对话发送消息的权限"),
])
def test_known_target_errors_are_actionable_and_never_retried(error_type, expected):
    async def run():
        service = TelegramService(SimpleNamespace(decrypt=lambda value: {}))
        client = DialogClient(entity_for("supergroup"), failure=error_type(request=None))
        service.client = lambda secret: client
        status, detail = await service.send({"id": 1, "secret": "mock"}, job("-1003456789012"))
        assert status == "failed" and expected in detail and "消息未发送" in detail
        assert len(client.requests) == 1
    asyncio.run(run())
