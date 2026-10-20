import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from app import cli
from app.configuration import access_summary
from app.store import PASSWORDS, Store, initialize

PASSWORD = "test-only-cli-password"
PROJECT = Path(__file__).resolve().parents[1]


def invoke(monkeypatch, path, *args, answers=(), passwords=()):
    monkeypatch.setenv("TGCHECKIN_DATA", str(path))
    monkeypatch.setattr(sys, "argv", ["app.cli", *args])
    inputs, secrets = iter(answers), iter(passwords)
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(secrets))
    cli.main()


def config(path):
    return json.loads((path / "config.json").read_text())


def add_session(path):
    db = Store(path)
    db.execute("INSERT INTO sessions VALUES('test-session','csrf',9999999999)")
    db.close()


def session_count(path):
    db = Store(path)
    count = db.one("SELECT COUNT(*) n FROM sessions")["n"]
    db.close()
    return count


def test_init_defaults_and_password_hash(monkeypatch, tmp_path, capsys):
    invoke(monkeypatch, tmp_path, "init", answers=[""] * 6, passwords=[PASSWORD, PASSWORD])
    saved = config(tmp_path)
    assert (saved["host"], saved["port"], saved["username"]) == ("127.0.0.1", 8080, "admin")
    assert saved["autostart"] and not saved["secure_cookie"]
    assert PASSWORDS.verify(saved["password_hash"], PASSWORD)
    assert PASSWORD not in (tmp_path / "config.json").read_text()
    assert PASSWORD not in capsys.readouterr().out


def test_init_public_retries_invalid_values(monkeypatch, tmp_path, capsys):
    invoke(monkeypatch, tmp_path, "init",
           answers=["invalid", "2", "0", "65536", "9090", "off", "invalid", "n", "n", "bad name", "owner"],
           passwords=["short", PASSWORD, "mismatch", PASSWORD, PASSWORD])
    saved = config(tmp_path)
    assert (saved["host"], saved["port"], saved["username"]) == ("0.0.0.0", 9090, "owner")
    assert not saved["autostart"] and not saved["secure_cookie"]
    output = capsys.readouterr().out
    assert "http://服务器IP:9090" in output
    assert PASSWORD not in output


def test_init_ipv6_https(monkeypatch, tmp_path, capsys):
    invoke(monkeypatch, tmp_path, "init",
           answers=["3", "::", "8443", "off", "y", "y", "owner"], passwords=[PASSWORD, PASSWORD])
    saved = config(tmp_path)
    assert saved["host"] == "::" and saved["port"] == 8443 and saved["secure_cookie"]
    output = capsys.readouterr().out
    assert "HTTPS 反向代理域名" in output
    assert "Web 地址：http://" not in output


def test_reconfigure_preserves_credentials_data_and_sessions(monkeypatch, tmp_path):
    initialize(tmp_path, "owner", PASSWORD, host="0.0.0.0", port=9090,
               secure_cookie=True, autostart=False)
    add_session(tmp_path)
    original = config(tmp_path)
    key = (tmp_path / "master.key").read_bytes()
    invoke(monkeypatch, tmp_path, "config", answers=[""] * 6, passwords=[""])
    assert config(tmp_path) == original
    assert (tmp_path / "master.key").read_bytes() == key
    assert session_count(tmp_path) == 1


@pytest.mark.parametrize("args,expected", [
    (["public", "9001"], {"host": "0.0.0.0", "port": 9001}),
    (["local", "9002"], {"host": "127.0.0.1", "port": 9002}),
    (["bind", "::1", "9003"], {"host": "::1", "port": 9003}),
    (["port", "80"], {"host": "127.0.0.1", "port": 80}),
    (["disable"], {"autostart": False}),
    (["enable"], {"autostart": True}),
    (["domain", "https://checkin.example.com/"], {"public_url": "https://checkin.example.com"}),
])
def test_configuration_shortcuts(monkeypatch, tmp_path, args, expected):
    initialize(tmp_path, "owner", PASSWORD)
    add_session(tmp_path)
    invoke(monkeypatch, tmp_path, *args)
    saved = config(tmp_path)
    assert all(saved[k] == v for k, v in expected.items())
    assert session_count(tmp_path) == 1


@pytest.mark.parametrize("args", [["port", "0"], ["public", "65536"], ["bind", "invalid"]])
def test_invalid_shortcut_does_not_write_config(monkeypatch, tmp_path, args):
    initialize(tmp_path, "owner", PASSWORD)
    before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(SystemExit):
        invoke(monkeypatch, tmp_path, *args)
    assert (tmp_path / "config.json").read_bytes() == before


def test_username_and_password_revoke_sessions(monkeypatch, tmp_path):
    initialize(tmp_path, "owner", PASSWORD)
    add_session(tmp_path)
    invoke(monkeypatch, tmp_path, "username", "new-admin")
    assert config(tmp_path)["username"] == "new-admin" and session_count(tmp_path) == 0
    add_session(tmp_path)
    invoke(monkeypatch, tmp_path, "passwd", passwords=["new-cli-password-123", "new-cli-password-123"])
    assert PASSWORDS.verify(config(tmp_path)["password_hash"], "new-cli-password-123")
    assert session_count(tmp_path) == 0


def test_setup_changes_credentials_and_network_together(monkeypatch, tmp_path):
    initialize(tmp_path, "owner", PASSWORD)
    add_session(tmp_path)
    invoke(monkeypatch, tmp_path, "setup",
           answers=["2", "9000", "off", "n", "n", "new-admin"],
           passwords=["another-test-password", "another-test-password"])
    saved = config(tmp_path)
    assert saved["username"] == "new-admin" and saved["host"] == "0.0.0.0"
    assert saved["port"] == 9000 and not saved["autostart"]
    assert PASSWORDS.verify(saved["password_hash"], "another-test-password")
    assert session_count(tmp_path) == 0


def test_access_summary_uses_custom_port_and_ipv6(capsys):
    access_summary({"host": "::1", "port": 9000, "username": "owner", "secure_cookie": False})
    output = capsys.readouterr().out
    assert "http://[::1]:9000" in output
    assert "18080:[::1]:9000" in output
    assert ":8080" not in output


def test_menu_accepts_numbers_and_commands_without_shell(monkeypatch, capsys):
    choices = iter(["17", "9090", "tgc public 9000", "restart", "rm -rf /", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(choices))
    monkeypatch.setattr("os.path.isfile", lambda path: True)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    cli.menu()
    assert [cmd for cmd, _ in calls] == [
        ["/usr/local/bin/tgcheckin", "port", "9090"],
        ["/usr/local/bin/tgcheckin", "public", "9000"],
        ["/usr/local/bin/tgcheckin", "restart"],
    ]
    assert all(not kw.get("shell") for _, kw in calls)
    assert "未知选项" in capsys.readouterr().out


@pytest.fixture
def wrapper(tmp_path):
    """Run the real shell dispatch with fake runuser/systemctl, never touching the host."""
    data = tmp_path / "data"
    initialize(data, "owner", PASSWORD)
    release = tmp_path / "release"
    (release / "venv/bin").mkdir(parents=True)
    python = release / "venv/bin/python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    (release / "app").symlink_to(PROJECT, target_is_directory=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = {
        "runuser": '#!/bin/bash\nshift 3\nexec "$@"\n',
        "systemctl": '#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_SERVICE_LOG"\n'
                     'if [[ "$1" == is-active ]]; then exit "$TEST_ACTIVE_EXIT"; fi\n',
    }
    for name, script in fake.items():
        target = bin_dir / name
        target.write_text(script)
        target.chmod(0o755)
    script = (PROJECT / "scripts/tgcheckin").read_text()
    script = script.replace(
        'if [[ $EUID -ne 0 ]]; then exec sudo /usr/local/bin/tgcheckin "$@"; fi',
        "# Test harness: privilege switching is mocked.")
    script = script.replace("ROOT=/opt/tgcheckin/current", f"ROOT={shlex.quote(str(release))}")
    script = script.replace("export TGCHECKIN_DATA=/var/lib/tgcheckin",
                            f"export TGCHECKIN_DATA={shlex.quote(str(data))}")
    entry = tmp_path / "tgc"
    entry.write_text(script)
    log = tmp_path / "service.log"
    env = {**os.environ, "PATH": str(bin_dir)+os.pathsep+os.environ["PATH"],
           "TEST_SERVICE_LOG": str(log), "TEST_ACTIVE_EXIT": "0"}
    return entry, data, log, env


@pytest.mark.parametrize("active", [True, False])
def test_wrapper_applies_config_restarts_only_active_service(wrapper, active):
    entry, data, log, env = wrapper
    env["TEST_ACTIVE_EXIT"] = "0" if active else "3"
    result = subprocess.run(["bash", str(entry), "public", "9091"], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert config(data)["host"] == "0.0.0.0" and config(data)["port"] == 9091
    calls = log.read_text().splitlines()
    assert calls == ["enable tgcheckin", "is-active --quiet tgcheckin"] + (["restart tgcheckin"] if active else [])


def test_wrapper_invalid_config_never_calls_systemctl(wrapper):
    entry, data, log, env = wrapper
    before = config(data)
    result = subprocess.run(["bash", str(entry), "port", "0"], env=env, text=True, capture_output=True)
    assert result.returncode != 0 and not log.exists() and config(data) == before
    assert "argument port" in result.stderr


def test_domain_wrapper_saves_url_restarts_and_can_clear(wrapper):
    entry, data, log, env = wrapper
    original = config(data)
    for value, expected in [("https://checkin.example.com/", "https://checkin.example.com"), ("off", "")]:
        result = subprocess.run(["bash", str(entry), "domain", value], env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        saved = config(data)
        assert saved["public_url"] == expected
        assert saved["host"] == original["host"] and saved["port"] == original["port"]
        assert saved["password_hash"] == original["password_hash"]
    assert log.read_text().splitlines().count("restart tgcheckin") == 2


def test_wizard_sets_public_url_and_retains_it(monkeypatch, tmp_path, capsys):
    invoke(monkeypatch, tmp_path, "init",
           answers=["2", "9000", "invalid", "https://CHECKIN.example.com:443/", "y", "y", "owner"],
           passwords=[PASSWORD, PASSWORD])
    saved = config(tmp_path)
    assert saved["public_url"] == "https://checkin.example.com" and saved["secure_cookie"]
    assert "Web 地址：https://checkin.example.com" in capsys.readouterr().out
    invoke(monkeypatch, tmp_path, "config", answers=[""] * 6, passwords=[""])
    assert config(tmp_path) == saved


def test_wrapper_boot_setting_does_not_restart(wrapper):
    entry, data, log, env = wrapper
    result = subprocess.run(["bash", str(entry), "disable"], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not config(data)["autostart"]
    assert log.read_text().splitlines() == ["disable tgcheckin"]


def test_help_needs_no_install_or_privileges():
    result = subprocess.run(["bash", str(PROJECT / "scripts/tgcheckin"), "help"], text=True, capture_output=True)
    assert result.returncode == 0
    assert "tgc public [端口]" in result.stdout
