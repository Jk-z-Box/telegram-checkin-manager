#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO="https://github.com/Jk-z-Box/telegram-checkin-manager.git"
ROOT=/opt/tgcheckin
DATA=/var/lib/tgcheckin
SOURCE=""
if [[ "${1:-}" == "--source" ]]; then SOURCE="${2:?Missing source directory}"; fi
if [[ "$(uname -s)" != Linux ]]; then echo "此脚本仅适用于 Linux。"; exit 1; fi
if [[ $EUID -ne 0 ]]; then echo "请使用 sudo bash install.sh 或以 root 运行。"; exit 1; fi
if ! command -v systemctl >/dev/null; then echo "需要 systemd；容器请按 README 手动运行。"; exit 1; fi
if [[ ! -d /run/systemd/system ]]; then echo "systemd 必须正在运行。"; exit 1; fi

install -d -m 755 "$ROOT"
exec 9>"$ROOT/install.lock"
flock -n 9 || { echo "另一个安装或更新正在运行。"; exit 1; }
echo "→ 安装系统依赖"
if command -v apt-get >/dev/null; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 python3-venv python3-pip git curl ca-certificates tzdata
elif command -v dnf >/dev/null; then
  dnf install -y python3 python3-pip git curl ca-certificates tzdata
else
  for command_name in python3 git curl; do
    command -v "$command_name" >/dev/null || { echo "请安装 Python 3.11+、venv、git、curl、tzdata。"; exit 1; }
  done
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "需要 Python 3.11+；推荐 Debian 12/13 或 Ubuntu 24.04+"'
id tgcheckin >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin tgcheckin
install -d -m 700 -o tgcheckin -g tgcheckin "$DATA"
install -d -m 755 "$ROOT/releases"
STAGING=$(mktemp -d "$ROOT/releases/staging.XXXXXXXX")
PREVIOUS=""
if [[ -L "$ROOT/current" && -d "$ROOT/current" ]]; then
  PREVIOUS=$(readlink -f "$ROOT/current")
elif [[ -e "$ROOT/current" ]]; then
  echo "$ROOT/current 不是有效的版本符号链接，请先检查现有安装。"
  exit 1
fi
SWITCHED=0
NEW_SERVICE=0
cleanup() {
  local code=$?
  trap - EXIT
  if [[ $code -ne 0 ]]; then
    echo "安装/更新失败。帐号数据保留在 $DATA。"
    if [[ $SWITCHED == 1 && -n "$PREVIOUS" ]]; then
      ln -sfn "$PREVIOUS" "$ROOT/rollback-link"
      mv -Tf "$ROOT/rollback-link" "$ROOT/current"
      install -m 755 "$PREVIOUS/app/scripts/tgcheckin" /usr/local/bin/tgcheckin
      if [[ -f "$STAGING/service.previous" ]]; then
        install -m 644 "$STAGING/service.previous" /etc/systemd/system/tgcheckin.service
        systemctl daemon-reload
      fi
      systemctl restart tgcheckin || true
      echo "已恢复到此前版本。"
    elif [[ $NEW_SERVICE == 1 ]]; then
      systemctl stop tgcheckin || true
    fi
    if [[ $SWITCHED == 0 ]]; then rm -rf -- "$STAGING"; fi
  fi
  exit "$code"
}
trap cleanup EXIT
echo "→ 下载项目"
if [[ -n "$SOURCE" ]]; then
  git clone --no-hardlinks "$SOURCE" "$STAGING/app"
else
  git clone --depth 1 --branch main "$REPO" "$STAGING/app"
fi
REVISION=$(git -C "$STAGING/app" rev-parse --short HEAD)
RELEASE="$ROOT/releases/$(date -u +%Y%m%dT%H%M%S)-$REVISION-$$"
mv "$STAGING" "$RELEASE"
STAGING="$RELEASE"
python3 -m venv "$RELEASE/venv"
"$RELEASE/venv/bin/pip" install --disable-pip-version-check -q -r "$RELEASE/app/requirements.txt"
chmod -R a+rX "$RELEASE"
chmod -R go-w "$RELEASE"
export TGCHECKIN_DATA="$DATA"
cd "$RELEASE/app"
if [[ ! -f "$DATA/config.json" ]]; then
  echo "→ 配置监听方式、端口、Web 管理员及开机自启（密码不会回显）"
  "$RELEASE/venv/bin/python" -m app.cli init </dev/tty
elif [[ -n "$PREVIOUS" ]]; then
  install -d -m 700 -o tgcheckin -g tgcheckin /var/backups/tgcheckin
  runuser -u tgcheckin -- "$PREVIOUS/venv/bin/python" -m app.backup /var/backups/tgcheckin
  echo "→ 保留现有配置；更新完成后可运行 tgc config 重新设置"
fi
chown -R tgcheckin:tgcheckin "$DATA"
chmod 700 "$DATA"
find "$DATA" -type f -exec chmod 600 {} +
if [[ -f /etc/systemd/system/tgcheckin.service ]]; then
  cp /etc/systemd/system/tgcheckin.service "$STAGING/service.previous"
fi

cat > /etc/systemd/system/tgcheckin.service <<'UNIT'
[Unit]
Description=Telegram Check-in Manager
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=tgcheckin
Group=tgcheckin
WorkingDirectory=/opt/tgcheckin/current/app
Environment=TGCHECKIN_DATA=/var/lib/tgcheckin
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/tgcheckin/current/venv/bin/python -m app.cli serve
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/tgcheckin
RestrictSUIDSGID=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
UNIT
install -m 755 "$RELEASE/app/scripts/tgcheckin" /usr/local/bin/tgcheckin
SHORTCUT_INSTALLED=0
if [[ ! -e /usr/local/bin/tgc && ! -L /usr/local/bin/tgc ]] ||
   [[ "$(readlink /usr/local/bin/tgc 2>/dev/null || true)" == /usr/local/bin/tgcheckin ]]; then
  ln -sfn /usr/local/bin/tgcheckin /usr/local/bin/tgc
  SHORTCUT_INSTALLED=1
fi
ln -sfn "$RELEASE" "$ROOT/next-link"
mv -Tf "$ROOT/next-link" "$ROOT/current"
SWITCHED=1
[[ -n "$PREVIOUS" ]] || NEW_SERVICE=1
systemctl daemon-reload
AUTOSTART=$("$RELEASE/venv/bin/python" -c '
import json,os
with open(os.environ["TGCHECKIN_DATA"]+"/config.json") as f:
    print("enable" if json.load(f).get("autostart", True) else "disable")
')
systemctl "$AUTOSTART" tgcheckin
systemctl restart tgcheckin
ADDRESS=$("$RELEASE/venv/bin/python" -c '
import json,os
c=json.load(open(os.environ["TGCHECKIN_DATA"]+"/config.json"))
h=c["host"]
h="127.0.0.1" if h=="0.0.0.0" else "::1" if h=="::" else h
print(("["+h+"]" if ":" in h else h)+":"+str(c["port"]))
')
echo "→ 等待服务启动"
for attempt in $(seq 1 20); do
  if curl --noproxy '*' -fsS "http://$ADDRESS/healthz" >/dev/null; then
    echo "安装成功（版本 $REVISION）"
    runuser -u tgcheckin -- "$RELEASE/venv/bin/python" -m app.cli access
    if [[ $SHORTCUT_INSTALLED == 0 ]]; then
      echo "已有同名 /usr/local/bin/tgc，未覆盖；请使用 tgcheckin 管理。"
    fi
    exit 0
  fi
  sleep 1
done
journalctl -u tgcheckin -n 30 --no-pager
exit 1
