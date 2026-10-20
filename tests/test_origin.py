import pytest
from fastapi.testclient import TestClient

from app.configuration import validate_public_url
from app.main import create_app
from app.store import initialize, save_config


@pytest.mark.parametrize("value,expected", [
    ("off", ""),
    ("", ""),
    ("https://CHECKIN.example.com:443/", "https://checkin.example.com"),
    ("http://localhost:80", "http://localhost"),
    ("https://checkin.example.com:8443", "https://checkin.example.com:8443"),
    ("http://[::1]:8080/", "http://[::1]:8080"),
])
def test_public_url_normalization(value, expected):
    assert validate_public_url(value) == expected


@pytest.mark.parametrize("value", [
    "checkin.example.com", "https://*", "https://example.com/path",
    "https://example.com?query", "https://example.com#fragment",
    "https://user:password@example.com", "https://example.com:0",
    "https://example.com:65536", "https://example.com:wrong",
    "https://example.com\\@attacker.invalid", "https://example.com\nHost:bad",
    "https://%65xample.com", "https://[::1", "https://-invalid.example",
])
def test_invalid_public_url_is_rejected(value):
    with pytest.raises(ValueError):
        validate_public_url(value)


def test_cloudflare_http_origin_login_and_csrf(tmp_path):
    password = "test-only-domain-password"
    domain = "https://checkin.example.com"
    initialize(tmp_path, "admin", password, public_url=domain)
    app = create_app(tmp_path, scheduler=False)
    # Simulates CF HTTPS browser requests arriving at a plain HTTP origin with
    # a rewritten Host/port. No trust in CF headers or IP addresses is needed.
    with TestClient(app, base_url="http://origin.internal:9090") as client:
        client.headers["Origin"] = domain
        auth = client.post("/api/login", json={"username": "admin", "password": password})
        assert auth.status_code == 200
        assert client.post("/api/pause", json={"paused": True}).status_code == 403
        client.headers["X-CSRF-Token"] = auth.json()["csrf"]
        assert client.post("/api/pause", json={"paused": True}).status_code == 200
        for origin in ("https://attacker.invalid", "http://checkin.example.com",
                       "https://checkin.example.com:8443", "null", "http://origin.internal:9090"):
            response = client.post("/api/pause", json={"paused": False}, headers={
                "Origin": origin, "X-Forwarded-Host": "checkin.example.com",
                "X-Forwarded-Proto": "https",
            })
            assert response.status_code == 403
        assert client.get("/api/overview").json()["paused"]
        assert client.post("/api/logout").status_code == 200


def test_old_config_falls_back_to_same_origin_and_domain_setting_is_explicit(tmp_path):
    initialize(tmp_path, "admin", "test-only-domain-password")
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app, base_url="http://origin.internal:9090") as client:
        config = app.state.store.config()
        config.pop("public_url")
        save_config(tmp_path, config)
        # Empty JSON stops at validation, without attempting authentication.
        assert client.post("/api/login", json={}, headers={"Origin": "http://origin.internal:9090"}).status_code == 422
        domain = "https://checkin.example.com"
        assert client.post("/api/login", json={}, headers={"Origin": domain}).status_code == 403
        config["public_url"] = domain
        save_config(tmp_path, config)
        assert client.post("/api/login", json={}, headers={"Origin": domain}).status_code == 422
        assert client.post("/api/login", json={}, headers={
            "Origin": "https://attacker.invalid", "Host": "attacker.invalid",
            "X-Forwarded-Proto": "https",
        }).status_code == 403
