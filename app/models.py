from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, field_validator, model_validator


def next_time(cron, zone, now=None):
    trigger = CronTrigger.from_crontab(cron, timezone=ZoneInfo(zone))
    value = trigger.get_next_fire_time(None, datetime.fromtimestamp(
        (now if now is not None else datetime.now(timezone.utc).timestamp()) + 1,
        timezone.utc,
    ))
    if value is None:
        raise ValueError("此 Cron 没有可执行的未来时间")
    return value.timestamp()


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AccountStart(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    api_id: int = Field(gt=0, lt=2**31)
    api_hash: str = Field(pattern=r"^[a-fA-F0-9]{32}$")
    phone: str = Field(pattern=r"^\+[1-9][0-9]{6,14}$")


class AccountFinish(BaseModel):
    ticket: str = Field(min_length=16, max_length=100)
    code: str = Field(default="", max_length=20)
    password: str = Field(default="", max_length=256)


class Toggle(BaseModel):
    enabled: bool


class Pause(BaseModel):
    paused: bool


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class Job(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4096)
    cron: str = Field(default="0 9 * * *", max_length=100)
    timezone: str = Field(default="Asia/Shanghai", max_length=80)
    button: str = Field(default="", max_length=100)
    success_text: str = Field(default="", max_length=200)
    reply_timeout: int = Field(default=30, ge=5, le=120)
    enabled: bool = True

    @field_validator("name", "target", "message")
    @classmethod
    def not_blank(cls, v):
        if not v.strip():
            raise ValueError("不能全部为空格")
        return v.strip()

    @model_validator(mode="after")
    def schedule_valid(self):
        try:
            next_time(self.cron, self.timezone)
        except Exception as e:
            raise ValueError("Cron 或时区无效；Cron 使用五段格式，星期 0=周一、6=周日") from e
        return self
