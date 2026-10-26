"""Actual authentication/CSRF and engine routes; process behavior tested separately."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.config import settings
from app.routers.engines import get_runtime
from app.services import local_claude_runner
from app.services.claude_login import ClaudeLogin
from tests.auth.test_auth_api import (  # noqa: F401
    app, factory, auth_settings, client, login, pairing_code, set_owner,
)


@pytest.fixture
async def controller(app, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)
    monkeypatch.setattr(local_claude_runner, "resolve_claude_executable", lambda: Path("/fake/claude"))
    manager = ClaudeLogin()
    async def start(owner, executable):
        manager.flow = {"id": "a" * 32, "owner": owner, "phase": "running", "authorization_url": "https://claude.ai/oauth/authorize?state=test"}
        return manager.view(owner, "a" * 32)
    monkeypatch.setattr(manager, "start", start)
    rt = SimpleNamespace(claude_login=manager, adapters={"claude": object()}, _closing=False, engines=Mock())
    async def complete(owner, flow_id, code):
        manager.view(owner, flow_id)
        if code != "valid-code123":
            from app.services.codex_login import LoginError
            raise LoginError("Invalid code")
    monkeypatch.setattr(manager, "complete", complete)
    app.dependency_overrides[get_runtime] = lambda: rt
    yield manager
    await manager.close()


async def test_session_binding_csrf_and_no_store(app, factory, controller):
    await set_owner(factory)
    path = "/api/v1/engines/claude/login"
    async with client(app) as one, client(app) as two:
        first = await login(one)
        second = await login(two)
        assert (await one.post(path)).status_code == 403
        result = await one.post(path, headers={"X-CSRF-Token": first["csrf_token"]})
        assert result.status_code == 202, result.text
        assert result.headers["cache-control"] == "no-store"
        flow_path = f"{path}/{result.json()['id']}"
        read = await one.get(flow_path)
        assert read.json()["authorization_url"] == "https://claude.ai/oauth/authorize?state=test"
        assert read.headers["cache-control"] == "no-store"
        assert "owner" not in read.json()
        assert (await two.get(flow_path)).status_code == 404
        assert (await two.delete(flow_path, headers={"X-CSRF-Token": second["csrf_token"]})).status_code == 404
        assert (await one.post(flow_path + "/code", json={"code": "valid-code123"})).status_code == 403
        assert (await two.post(flow_path + "/code", json={"code": "valid-code123"}, headers={"X-CSRF-Token": second["csrf_token"]})).status_code == 404
        invalid = await one.post(flow_path + "/code", json={"code": "private-invalid-code"}, headers={"X-CSRF-Token": first["csrf_token"]})
        assert invalid.status_code == 422
        assert "private-invalid-code" not in invalid.text
        assert (await one.post(flow_path + "/code", json={"code": "valid-code123"}, headers={"X-CSRF-Token": first["csrf_token"]})).status_code == 204
        assert (await one.delete(flow_path)).status_code == 403
        assert (await one.delete(flow_path, headers={"X-CSRF-Token": first["csrf_token"]})).status_code == 204


async def test_chat_device_cannot_start_or_read_login(app, factory, controller):
    await set_owner(factory)
    path = "/api/v1/engines/claude/login"
    async with client(app) as admin, client(app) as phone:
        session = await login(admin)
        code = await pairing_code(admin, session["csrf_token"], scope="chat")
        paired = await phone.post("/api/v1/auth/pair", json={"code": code})
        assert paired.status_code == 200, paired.text
        token = paired.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert (await phone.post(path, headers=headers)).status_code == 403
        assert (await phone.get(path + "/" + "a" * 32, headers=headers)).status_code == 403
