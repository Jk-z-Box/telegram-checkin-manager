import asyncio
import logging
import re
import secrets
import time

from telethon import TelegramClient, events, errors, functions, types, utils
from telethon.sessions import StringSession
from telethon.tl.types import InlineButtonTypeCallback, ButtonTypeDefault


class CheckinError(Exception):
    pass


RETRY_DELAY = 2


def send_policy(job):
    """A retry count excludes the initial attempt; disabled tasks keep 90 seconds."""
    if job.get("retry_enabled", False):
        return job.get("send_timeout", 30), job.get("max_retries", 2)
    return 90, 0


def execution_timeout(job):
    timeout, retries = send_policy(job)
    return timeout * (retries + 1) + RETRY_DELAY * retries + job["reply_timeout"] + 15


def transient_send_error(exc):
    # Telethon classifies this as a ServerError, but retrying cannot recover its ID.
    if isinstance(exc, errors.RandomIdDuplicateError):
        return False
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, errors.ServerError,
                            errors.RpcCallFailError, errors.TimedOutError))


def sent_message_id(result, random_id):
    if isinstance(result, types.UpdateShortSentMessage):
        return result.id
    updates = [result.update] if isinstance(result, types.UpdateShort) else getattr(result, "updates", [])
    for update in updates:
        if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
            return update.id
    return None


class LoginFailure(CheckinError):
    """A credential-free diagnostic suitable for the Web UI and service logs."""

    def __init__(self, stage, error):
        self.stage = stage
        self.error_type = type(error).__name__
        self.reference = secrets.token_hex(4)
        label = {
            "initialize": "初始化登录客户端",
            "connect": "连接 Telegram",
            "send_code": "发送验证码",
            "verify_code": "校验验证码",
            "verify_password": "校验两步验证密码",
            "read_account": "读取帐号信息",
            "save_account": "保存帐号",
        }[stage]
        super().__init__(
            f"{label}失败（{self.error_type}）：{login_error_message(error)}"
            f"；诊断编号 {self.reference}，可用 tgc logs 查看"
        )
        # Never log the raw exception/traceback/request: they may contain credentials.
        logging.getLogger(__name__).warning(
            "Telegram login failed stage=%s error_type=%s reference=%s",
            stage, self.error_type, self.reference,
        )


class TelegramService:
    def __init__(self, store):
        self.store = store
        self.pending = {}
        self.locks = {}
        self.active = set()

    def client(self, secret, *, login=False):
        return TelegramClient(
            StringSession(secret.get("session", "")), secret["api_id"], secret["api_hash"],
            # Telethon consumes an attempt when switching Telegram DCs. Login must
            # be able to continue after PHONE/NETWORK/USER_MIGRATE. Task retries
            # are managed explicitly with a fixed random_id and per-task limits.
            flood_sleep_threshold=0, request_retries=3 if login else 0,
            raise_last_call_error=True, connection_retries=2,
            timeout=15, device_model="Checkin Manager", system_version="Linux",
            app_version="1.3.0",
        )

    async def prune(self):
        for ticket, item in list(self.pending.items()):
            if item["expires"] < time.time() and not item["busy"]:
                self.pending.pop(ticket, None)
                await item["client"].disconnect()

    async def start(self, data):
        await self.prune()
        if len(self.pending) >= 5:
            raise CheckinError("最多同时进行 5 个登录，请完成或等待 5 分钟后重试")
        secret = data.model_dump()
        client = None
        stage = "initialize"
        try:
            client = self.client(secret, login=True)
            self.active.add(client)
            async with asyncio.timeout(55):
                stage = "connect"
                await client.connect()
                stage = "send_code"
                sent = await client.send_code_request(data.phone)
            ticket = secrets.token_urlsafe(24)
            self.pending[ticket] = {
                "client": client, "secret": secret, "hash": sent.phone_code_hash,
                "expires": time.time() + 300, "busy": False, "need_password": False,
            }
            return {"ticket": ticket, "expires_in": 300}
        except Exception as exc:
            if client:
                await self.disconnect_login(client)
            raise LoginFailure(stage, exc) from None
        except BaseException:
            if client:
                await self.disconnect_login(client)
            raise
        finally:
            self.active.discard(client)

    async def finish(self, data):
        await self.prune()
        item = self.pending.get(data.ticket)
        if not item:
            raise CheckinError("登录已过期，请重新获取验证码")
        if item["busy"]:
            raise CheckinError("登录请求正在处理")
        item["busy"] = True
        client, secret = item["client"], item["secret"]
        stage = "verify_password" if item["need_password"] else "verify_code"
        try:
            if item["need_password"]:
                await client.sign_in(password=data.password)
            else:
                try:
                    await client.sign_in(phone=secret["phone"], code=data.code,
                                         phone_code_hash=item["hash"])
                except errors.SessionPasswordNeededError:
                    item["need_password"] = True
                    return {"need_password": True}
            stage = "read_account"
            me = await client.get_me()
            if me is None:
                raise CheckinError("未取得已登录的帐号信息，请重新获取验证码")
            stage = "save_account"
            secret["session"] = client.session.save()
            self.store.execute(
                """INSERT INTO accounts(name,uid,secret,created) VALUES(?,?,?,?)
                ON CONFLICT(uid) DO UPDATE SET name=excluded.name,secret=excluded.secret,
                enabled=1,cooldown=0""",
                (secret["name"], me.id, self.store.encrypt(secret), time.time()),
            )
            self.pending.pop(data.ticket, None)
            await self.disconnect_login(client)
            return {"ok": True}
        except Exception as exc:
            raise LoginFailure(stage, exc) from None
        finally:
            item["busy"] = False

    async def disconnect_login(self, client):
        try:
            await asyncio.wait_for(client.disconnect(), timeout=3)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Telegram login cleanup failed error_type=%s", type(exc).__name__)

    def lock(self, account_id):
        return self.locks.setdefault(account_id, asyncio.Lock())

    async def dialogs(self, account):
        async with self.lock(account["id"]):
            client = self.client(self.store.decrypt(account["secret"]))
            self.active.add(client)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise CheckinError("帐号会话失效，请重新登录")
                return [{"id": str(d.id), "name": d.name or str(d.id)}
                        async for d in client.iter_dialogs(limit=150)]
            finally:
                self.active.discard(client)
                await client.disconnect()

    async def resolve(self, client, target):
        if target.lstrip("-").isdigit():
            peer_id = int(target)
            # StringSession does not persist entity access hashes. Resolve numeric
            # peers through the current dialog list, including private groups.
            async for dialog in client.iter_dialogs():
                if dialog.id == peer_id:
                    return dialog.input_entity
            raise CheckinError("未找到此对话 ID；请确认帐号已加入，并从对话列表选择")
        return await client.get_input_entity(target)

    async def send(self, account, job):
        client = self.client(self.store.decrypt(account["secret"]))
        self.active.add(client)
        queue = asyncio.Queue(maxsize=100)
        peer_id = None
        is_private = False
        request = None
        dispatched = False
        uncertain = False
        timeout, retries = send_policy(job)

        async def receive(event):
            # For groups, replies are further restricted to our command below.
            if peer_id is not None and event.chat_id == peer_id and not event.out and not queue.full():
                queue.put_nowait(event.message)

        handler = receive
        client.add_event_handler(handler, events.NewMessage())
        client.add_event_handler(handler, events.MessageEdited())
        try:
            for attempt in range(retries + 1):
                prefix = f"发送尝试 {attempt + 1} 次（重试 {attempt} 次）；"
                try:
                    # Only preparation and message submission share this timeout.
                    # The reply deadline starts after a confirmed send response.
                    async with asyncio.timeout(timeout):
                        await client.connect()
                        if request is None:
                            if not await client.is_user_authorized():
                                raise CheckinError("帐号会话失效，请重新登录")
                            target = await self.resolve(client, job["target"])
                            entity = await client.get_entity(target)
                            peer_id = utils.get_peer_id(entity)
                            is_private = hasattr(entity, "first_name")
                            # Build once: every retry uses the same random_id, so
                            # Telegram can deduplicate a send whose response was lost.
                            # No formatting entities: preserve the original plain text.
                            request = functions.messages.SendMessageRequest(peer=target, message=job["message"])
                        dispatched = True
                        result = await client(request)
                    sent_id = sent_message_id(result, request.random_id)
                    if sent_id is None:
                        return "unknown", prefix + "Telegram 未返回可核对的消息 ID；请核实是否已发送，已停止重试"
                    break
                except errors.FloodWaitError as exc:
                    # Runner persists cooldown; do not retry Telegram rate limits.
                    exc.checkin_status = "unknown" if uncertain else "failed"
                    exc.checkin_detail = prefix + ("此前发送结果未知；请在 Telegram 核实；" if uncertain else "")
                    raise
                except Exception as exc:
                    transient = transient_send_error(exc)
                    rejected = isinstance(exc, errors.RPCError) and exc.code in (400, 401, 403, 406, 420)
                    uncertain = uncertain or (dispatched and not rejected) or isinstance(exc, errors.RandomIdDuplicateError)
                    if not transient or attempt == retries:
                        reason = (f"发送阶段超过 {timeout} 秒" if isinstance(exc, TimeoutError)
                                  else friendly_error(exc))
                        end = ("已达到最多重试次数" if transient and retries else
                               "未开启失败后重试" if transient else "此错误不自动重试")
                        status = "unknown" if uncertain else "failed"
                        detail = "；请在 Telegram 核实是否已发送" if uncertain else "；消息未发送"
                        return status, prefix + reason + "；" + end + detail
                    await asyncio.sleep(RETRY_DELAY)

            if not job["button"] and not job["success_text"]:
                return "sent", prefix + "签到消息已发送；未配置成功回复校验"
            try:
                async with asyncio.timeout(job["reply_timeout"]):
                    status, detail = await self.wait_reply(job, queue, sent_id, is_private)
                return status, prefix + detail
            except TimeoutError:
                return "unconfirmed", prefix + "消息已发送，但等待回复超时；不会触发发送重试"
            except errors.FloodWaitError as exc:
                exc.checkin_status = "unconfirmed"
                exc.checkin_detail = prefix + "消息已发送，但处理回复时触发限流；不会触发发送重试；"
                raise
            except Exception as exc:
                return "unconfirmed", prefix + "消息已发送，但处理回复失败：" + friendly_error(exc) + "；不会触发发送重试"
        finally:
            client.remove_event_handler(handler)
            self.active.discard(client)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=3)
            except Exception as exc:
                logging.getLogger(__name__).warning("Telegram send cleanup failed error_type=%s", type(exc).__name__)

    async def wait_reply(self, job, queue, sent_id, is_private):
        deadline = time.monotonic() + job["reply_timeout"]
        clicked = not bool(job["button"])
        button_id = None
        while time.monotonic() < deadline:
            try:
                reply = await asyncio.wait_for(queue.get(), deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            if reply.id <= sent_id:
                continue
            if not is_private and reply.reply_to_msg_id not in (sent_id, button_id):
                continue
            if not clicked and reply.buttons:
                for row in reply.buttons:
                    for button in row:
                        if button.text == job["button"]:
                            # Never follow URLs, share contact/location, or open auth flows.
                            if not isinstance(button.button.type, (InlineButtonTypeCallback, ButtonTypeDefault)):
                                raise CheckinError("匹配按钮不是普通文本或回调按钮，已拒绝点击")
                            await button.click()
                            clicked, button_id = True, reply.id
                            break
                    if clicked:
                        break
                if clicked and not job["success_text"]:
                    return "sent", "消息已发送并点击按钮；未配置成功回复校验"
                if clicked:
                    # Only a subsequent event can confirm the click outcome.
                    continue
            if clicked and job["success_text"] and job["success_text"] in (reply.raw_text or ""):
                return "confirmed", "收到包含指定成功文本的回复"
        return "unconfirmed", ("消息已发送，但等待回复超时；请在 Telegram 核实。"
                                "不会自动重发以避免重复签到")

    async def close(self):
        clients = set(self.active) | {item["client"] for item in self.pending.values()}
        await asyncio.gather(*(c.disconnect() for c in clients), return_exceptions=True)
        self.pending.clear()


def login_error_message(exc):
    """Classify known failures without returning exception text or request fields."""
    if isinstance(exc, CheckinError):
        return str(exc)
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram 要求等待 {exc.seconds} 秒，请等待后再获取验证码"
    mapping = {
        "ApiIdInvalidError": "API ID 或 API Hash 无效，请核对 my.telegram.org/apps 中同一应用的凭据",
        "ApiIdPublishedFloodError": "这组 API 凭据已被 Telegram 限制，请检查你自己的应用凭据",
        "PhoneNumberInvalidError": "Telegram 不接受这个手机号，请核对号码和国家代码",
        "PhoneNumberBannedError": "此手机号被 Telegram 限制，请通过官方客户端确认帐号状态",
        "PhoneNumberFloodError": "此手机号请求验证码过于频繁，请稍后重试",
        "PhonePasswordFloodError": "此帐号登录尝试过于频繁，请稍后重试",
        "SendCodeUnavailableError": "Telegram 当前没有可用的验证码发送方式，请稍后重试",
        "PhoneCodeInvalidError": "验证码错误，请输入本次网页请求收到的验证码",
        "PhoneCodeExpiredError": "验证码已过期，请重新获取",
        "PhoneCodeEmptyError": "请输入验证码",
        "PasswordHashInvalidError": "两步验证密码错误",
        "AuthRestartError": "Telegram 要求重新开始登录，请稍后重新获取验证码",
        "PhoneMigrateError": "Telegram 数据中心切换未完成，请稍后重新获取验证码",
        "NetworkMigrateError": "Telegram 数据中心切换未完成，请稍后重新获取验证码",
        "UserMigrateError": "Telegram 数据中心切换未完成，请稍后重新获取验证码",
    }
    if type(exc).__name__ in mapping:
        return mapping[type(exc).__name__]
    if isinstance(exc, (asyncio.TimeoutError, errors.TimedOutError)):
        return "请求超时，请检查部署服务器到 Telegram 的网络连接"
    if isinstance(exc, (ConnectionError, OSError)):
        return "连接失败，请检查部署服务器到 Telegram 的网络连接"
    if isinstance(exc, (errors.ServerError, errors.RpcCallFailError)):
        return "Telegram 服务暂时未完成请求，请稍后重试"
    if isinstance(exc, ValueError):
        if re.fullmatch(r"Request was unsuccessful \d+ time\(s\)", str(exc)):
            return "Telegram 请求未完成，请确认已更新程序并稍后重试"
        return "登录参数或会话状态无效，请核对应用凭据并重新开始登录"
    return "登录请求未完成，请根据错误类型和诊断编号检查服务"


def friendly_error(exc, *, login=False):
    if isinstance(exc, CheckinError):
        return str(exc)
    if login:
        return f"登录请求未完成（{type(exc).__name__}）：{login_error_message(exc)}"
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram 要求等待 {exc.seconds} 秒；已暂停该帐号至冷却结束"
    if isinstance(exc, errors.RandomIdDuplicateError):
        return "Telegram 检测到相同消息标识，已停止重试；请核实原消息"
    if isinstance(exc, (errors.ServerError, errors.RpcCallFailError)):
        return f"Telegram 暂时未完成请求（{type(exc).__name__}）"
    if isinstance(exc, (TimeoutError, errors.TimedOutError)):
        return "等待 Telegram 响应超时"
    if isinstance(exc, (ConnectionError, OSError)):
        return f"连接 Telegram 失败（{type(exc).__name__}），请检查服务器网络"
    mapping = {
        "PhoneCodeInvalidError": "验证码错误，请重新输入",
        "PhoneCodeExpiredError": "验证码已过期，请重新获取",
        "PasswordHashInvalidError": "两步验证密码错误",
        "ApiIdInvalidError": "Telegram API ID 或 API Hash 无效",
        "PhoneNumberInvalidError": "手机号格式无效",
        "AuthKeyUnregisteredError": "帐号会话失效，请重新登录",
        "SessionRevokedError": "帐号会话已撤销，请重新登录",
        "ChatWriteForbiddenError": "当前帐号没有向此对话发送消息的权限",
        "UserDeactivatedBanError": "Telegram 帐号已被限制",
        "SlowModeWaitError": "此对话触发 Telegram 慢速模式，请等待后再执行",
    }
    # Raw Telegram exceptions can contain phone numbers or API request details.
    return mapping.get(type(exc).__name__, "请求未完成（%s），请检查网络、帐号及目标对话" % type(exc).__name__)
