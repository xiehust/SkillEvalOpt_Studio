"""Tests for the studio's optional username/password session auth.

Auth is off unless STUDIO_AUTH_PASSWORD is set, so every other test file
runs against an unauthenticated app; these tests flip it on per-test with
monkeypatch.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillopt_studio import auth
from skillopt_studio.app import create_app
from skillopt_studio.config import StudioConfig


@pytest.fixture
def studio_config(tmp_path: Path) -> StudioConfig:
    return StudioConfig(studio_root=tmp_path / "studio", skill_sources={})


@pytest.fixture
def client(studio_config) -> TestClient:
    app = create_app(studio_config)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("STUDIO_AUTH_USERNAME", "operator")
    monkeypatch.setenv("STUDIO_AUTH_PASSWORD", "s3cret-pass")


class TestAuthDisabled:
    def test_api_open_without_credentials(self, client):
        assert client.get("/api/skills").status_code == 200
        status = client.get("/api/auth/status").json()
        assert status == {"auth_required": False, "authenticated": True}

    def test_login_noop(self, client):
        response = client.post("/api/auth/login", json={"username": "x", "password": "y"})
        assert response.status_code == 200
        assert response.json()["auth_required"] is False


class TestAuthEnabled:
    def test_api_blocked_without_session(self, auth_env, client):
        assert client.get("/api/skills").status_code == 401
        assert client.get("/api/jobs").status_code == 401
        assert client.get("/docs").status_code == 401
        assert client.get("/openapi.json").status_code == 401

    def test_open_paths_stay_open(self, auth_env, client):
        assert client.get("/api/health").status_code == 200
        status = client.get("/api/auth/status").json()
        assert status == {"auth_required": True, "authenticated": False}

    def test_wrong_credentials_401(self, auth_env, client):
        for creds in (
            {"username": "operator", "password": "wrong"},
            {"username": "wrong", "password": "s3cret-pass"},
        ):
            response = client.post("/api/auth/login", json=creds)
            assert response.status_code == 401
            assert client.get("/api/skills").status_code == 401  # no cookie granted

    def test_login_grants_session(self, auth_env, client):
        response = client.post(
            "/api/auth/login", json={"username": "operator", "password": "s3cret-pass"}
        )
        assert response.status_code == 200
        assert auth.COOKIE_NAME in response.cookies
        assert client.get("/api/skills").status_code == 200
        assert client.get("/api/auth/status").json()["authenticated"] is True

    def test_logout_revokes_session(self, auth_env, client):
        client.post("/api/auth/login", json={"username": "operator", "password": "s3cret-pass"})
        assert client.get("/api/skills").status_code == 200
        client.post("/api/auth/logout")
        assert client.get("/api/skills").status_code == 401

    def test_default_username_admin(self, monkeypatch, studio_config):
        monkeypatch.delenv("STUDIO_AUTH_USERNAME", raising=False)
        monkeypatch.setenv("STUDIO_AUTH_PASSWORD", "pw")
        with TestClient(create_app(studio_config)) as client:
            ok = client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
            assert ok.status_code == 200

    def test_tampered_or_expired_cookie_rejected(self, auth_env, client):
        client.cookies.set(auth.COOKIE_NAME, "9999999999.deadbeef")
        assert client.get("/api/skills").status_code == 401
        expired = auth._sign(int(time.time()) - 10)
        client.cookies.set(auth.COOKIE_NAME, expired)
        assert client.get("/api/skills").status_code == 401

    def test_password_rotation_invalidates_sessions(self, auth_env, client, monkeypatch):
        client.post("/api/auth/login", json={"username": "operator", "password": "s3cret-pass"})
        assert client.get("/api/skills").status_code == 200
        monkeypatch.setenv("STUDIO_AUTH_PASSWORD", "rotated")
        assert client.get("/api/skills").status_code == 401

    def test_spa_shell_stays_public(self, auth_env, client):
        # The SPA shell and assets carry no data; only /api is guarded.
        response = client.get("/skills")
        assert response.status_code in (200, 404)  # 404 when dist/ absent in CI
        assert response.status_code != 401
