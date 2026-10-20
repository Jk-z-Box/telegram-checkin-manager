import asyncio
import fcntl
import hashlib
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import AccountFinish, AccountStart, Job, Login, PasswordChange, Pause, Toggle, next_time
from .runner import Runner
from .store import PASSWORDS, Store, data_dir, save_config
from .telegram import TelegramService, friendly_error

STATIC = Path(__file__).parent / "static"


def create_app(path=None, service_factory=TelegramService, scheduler=True):
    path = path or data_dir()

    @asynccontextmanager
    async def lifespan(app):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = open(path / "service.lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("只能启动一个服务进程；请勿使用多个 Uvicorn workers")
        store = Store(path)
        service = service_factory(store)
        runner = Runner(store, service)
        app.state.store, app.state.telegram, app.state.runner = store, service, runner
        if scheduler:
            runner.start()
        try:
            yield
        finally:
            await runner.close()
            store.close()
            lock.close()

    app = FastAPI(title="Telegram Check-in Manager", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    attempts = defaultdict(deque)
    password_slots = asyncio.Semaphore(2)

    @app.middleware("http")
    async def security(request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            # A configured browser origin also works when a remote proxy changes
            # Host, scheme or port. Do not trust client-supplied forwarded headers.
            expected = (request.app.state.store.config().get("public_url") or
                        str(request.base_url).rstrip("/"))
            if origin and origin != expected:
                return JSONResponse({"detail": "请求来源不匹配；通过域名访问时，请在服务器用 "
                                     "tgc domain 设置浏览器访问地址"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > 32768:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "请求格式无效"}, status_code=400)
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                      "img-src 'self' data:; connect-src 'self'; "
                                      "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Cache-Control": "no-store",
        })
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # FastAPI's default validation response echoes input (including passwords).
        return JSONResponse(status_code=422, content={
            "detail": "; ".join(".".join(map(str, e["loc"][1:])) + ": " + e["msg"]
                                for e in exc.errors())})

    def store(request: Request):
        return request.app.state.store

    def auth(request: Request, db=Depends(store)):
        token = request.cookies.get("tgcheckin", "")
        session = db.one("SELECT * FROM sessions WHERE token=? AND expires>?",
                         (hashlib.sha256(token.encode()).hexdigest(), time.time())) if token else None
        if not session:
            raise HTTPException(401, "请先登录")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise HTTPException(403, "登录状态已变化，请刷新页面")
        return session

    def require_account(db, account_id):
        account = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not account:
            raise HTTPException(404, "帐号不存在")
        return account

    def require_job(db, job_id):
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            raise HTTPException(404, "任务不存在")
        return job

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/login")
    async def login(data: Login, request: Request, response: Response, db=Depends(store)):
        now = time.time()
        ip = request.client.host if request.client else "unknown"
        for key in list(attempts):
            while attempts[key] and attempts[key][0] < now - 900:
                attempts[key].popleft()
            if not attempts[key]:
                del attempts[key]
        if len(attempts[ip]) >= 8 or len(attempts["__global__"]) >= 80:
            raise HTTPException(429, "登录尝试过多，请 15 分钟后重试")
        attempts[ip].append(now)
        attempts["__global__"].append(now)
        config = db.config()
        try:
            async with password_slots:
                valid = await asyncio.to_thread(PASSWORDS.verify, config["password_hash"], data.password)
        except (VerifyMismatchError, VerificationError):
            valid = False
        if not valid or not secrets.compare_digest(data.username.encode(), config["username"].encode()):
            raise HTTPException(401, "用户名或密码错误")
        attempts.pop(ip, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute("INSERT INTO sessions VALUES(?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), csrf, now + 43200))
        response.set_cookie("tgcheckin", token, max_age=43200, httponly=True,
                            secure=config["secure_cookie"], samesite="strict")
        return {"csrf": csrf, "username": config["username"]}

    @app.get("/api/me")
    async def me(session=Depends(auth), db=Depends(store)):
        return {"username": db.config()["username"], "csrf": session["csrf"]}

    @app.post("/api/logout")
    async def logout(response: Response, session=Depends(auth), db=Depends(store)):
        db.execute("DELETE FROM sessions WHERE token=?", (session["token"],))
        response.delete_cookie("tgcheckin")
        return {"ok": True}

    @app.post("/api/password")
    async def password(data: PasswordChange, session=Depends(auth), db=Depends(store)):
        config = db.config()
        try:
            await asyncio.to_thread(PASSWORDS.verify, config["password_hash"], data.current_password)
        except (VerifyMismatchError, VerificationError):
            raise HTTPException(400, "原密码错误")
        config["password_hash"] = await asyncio.to_thread(PASSWORDS.hash, data.new_password)
        save_config(path, config)
        db.execute("DELETE FROM sessions")
        return {"ok": True}

    @app.get("/api/overview")
    async def overview(session=Depends(auth), db=Depends(store)):
        return {
            "paused": db.paused(),
            "accounts": db.one("SELECT COUNT(*) n FROM accounts")["n"],
            "jobs": db.one("SELECT COUNT(*) n FROM jobs WHERE enabled=1")["n"],
            "runs24h": db.one("SELECT COUNT(*) n FROM runs WHERE started>?",
                              (time.time()-86400,))["n"],
            "confirmed24h": db.one("SELECT COUNT(*) n FROM runs WHERE started>? AND status='confirmed'",
                                   (time.time()-86400,))["n"],
        }

    @app.post("/api/pause")
    async def pause(data: Pause, session=Depends(auth), db=Depends(store)):
        db.execute("UPDATE settings SET value=? WHERE key='paused'", ("1" if data.paused else "0",))
        return {"ok": True}

    @app.get("/api/accounts")
    async def accounts(session=Depends(auth), db=Depends(store)):
        result = []
        for account in db.all("SELECT * FROM accounts ORDER BY id"):
            secret = db.decrypt(account.pop("secret"))
            phone = secret["phone"]
            account["phone"] = phone[:3] + " •••• " + phone[-4:]
            result.append(account)
        return result

    @app.post("/api/accounts/start")
    async def account_start(data: AccountStart, request: Request, session=Depends(auth)):
        try:
            return await asyncio.wait_for(request.app.state.telegram.start(data), timeout=60)
        except Exception as exc:
            raise HTTPException(400, friendly_error(exc, login=True))

    @app.post("/api/accounts/finish")
    async def account_finish(data: AccountFinish, request: Request, session=Depends(auth)):
        try:
            return await asyncio.wait_for(request.app.state.telegram.finish(data), timeout=60)
        except Exception as exc:
            raise HTTPException(400, friendly_error(exc, login=True))

    @app.get("/api/accounts/{account_id}/dialogs")
    async def dialogs(account_id: int, request: Request, session=Depends(auth), db=Depends(store)):
        account = require_account(db, account_id)
        if account["cooldown"] > time.time():
            raise HTTPException(429, "帐号处于 Telegram 限流冷却期")
        try:
            return await asyncio.wait_for(request.app.state.telegram.dialogs(account), timeout=60)
        except Exception as exc:
            from telethon.errors import FloodWaitError
            if isinstance(exc, FloodWaitError):
                db.execute("UPDATE accounts SET cooldown=? WHERE id=?",
                           (time.time()+exc.seconds+1, account_id))
            raise HTTPException(400, friendly_error(exc))

    @app.patch("/api/accounts/{account_id}")
    async def account_toggle(account_id: int, data: Toggle, session=Depends(auth), db=Depends(store)):
        require_account(db, account_id)
        db.execute("UPDATE accounts SET enabled=? WHERE id=?", (data.enabled, account_id))
        return {"ok": True}

    @app.delete("/api/accounts/{account_id}")
    async def account_delete(account_id: int, request: Request, session=Depends(auth), db=Depends(store)):
        require_account(db, account_id)
        if request.app.state.telegram.lock(account_id).locked():
            raise HTTPException(409, "帐号正在执行请求，请完成后删除")
        db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        return {"ok": True}

    @app.get("/api/jobs")
    async def jobs(session=Depends(auth), db=Depends(store)):
        return db.all("""SELECT j.*,a.name account_name FROM jobs j
                         JOIN accounts a ON j.account_id=a.id ORDER BY j.id DESC""")

    @app.post("/api/jobs")
    async def job_create(data: Job, session=Depends(auth), db=Depends(store)):
        require_account(db, data.account_id)
        fields = data.model_dump()
        fields.update(next_run=next_time(data.cron, data.timezone), created=time.time())
        columns = ",".join(fields)
        cur = db.execute(f"INSERT INTO jobs({columns}) VALUES({','.join('?' for _ in fields)})",
                         tuple(fields.values()))
        return {"id": cur.lastrowid}

    @app.put("/api/jobs/{job_id}")
    async def job_update(job_id: int, data: Job, request: Request, session=Depends(auth), db=Depends(store)):
        require_job(db, job_id)
        require_account(db, data.account_id)
        if job_id in request.app.state.runner.tasks:
            raise HTTPException(409, "任务正在执行，请完成后编辑")
        fields = data.model_dump()
        fields["next_run"] = next_time(data.cron, data.timezone)
        db.execute(f"UPDATE jobs SET {','.join(key+'=?' for key in fields)} WHERE id=?",
                   (*fields.values(), job_id))
        return {"ok": True}

    @app.patch("/api/jobs/{job_id}")
    async def job_toggle(job_id: int, data: Toggle, session=Depends(auth), db=Depends(store)):
        job = require_job(db, job_id)
        db.execute("UPDATE jobs SET enabled=?,next_run=? WHERE id=?",
                   (data.enabled, next_time(job["cron"], job["timezone"]), job_id))
        return {"ok": True}

    @app.delete("/api/jobs/{job_id}")
    async def job_delete(job_id: int, request: Request, session=Depends(auth), db=Depends(store)):
        require_job(db, job_id)
        if job_id in request.app.state.runner.tasks:
            raise HTTPException(409, "任务正在执行，请完成后删除")
        db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/run")
    async def job_run(job_id: int, request: Request, session=Depends(auth), db=Depends(store)):
        job = require_job(db, job_id)
        account = require_account(db, job["account_id"])
        if db.paused() or not job["enabled"] or not account["enabled"]:
            raise HTTPException(409, "请先启用全局调度、帐号及任务")
        if account["cooldown"] > time.time():
            raise HTTPException(429, "帐号处于 Telegram 限流冷却期")
        recent = db.one("SELECT started FROM runs WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,))
        if recent and recent["started"] > time.time()-30:
            raise HTTPException(429, "同一任务两次执行至少间隔 30 秒")
        run_id = request.app.state.runner.launch(job, "manual")
        if run_id is None:
            raise HTTPException(409, "任务已在执行或帐号被禁用")
        return {"run_id": run_id}

    @app.get("/api/runs")
    async def runs(limit: int = 100, session=Depends(auth), db=Depends(store)):
        return db.all("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
