"""Actual authentication/CSRF and engine routes; process behavior tested separately."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.config import settings
from app.routers.engines import get_runtime
from app.services import codex_adapter
from app.services.codex_login import CodexLogin
from tests.auth.test_auth_api import (  # noqa: F401
    app, factory, auth_settings, client, login, pairing_code, set_owner,
)


@pytest.fixture
async def controller(app, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)
    monkeypatch.setattr(codex_adapter, "resolve_codex_executable", lambda: Path("/fake/codex"))
    manager = CodexLogin()
    async def start(owner, executable):
        manager.flow = {"id": "a" * 32, "owner": owner, "phase": "running", "user_code": "AB12-CDE34"}
        return manager.view(owner, "a" * 32)
    monkeypatch.setattr(manager, "start", start)
    rt = SimpleNamespace(codex_login=manager, adapters={"codex": object()}, _closing=False, engines=Mock())
    app.dependency_overrides[get_runtime] = lambda: rt
    yield manager
    await manager.close()


async def test_session_binding_csrf_and_no_store(app, factory, controller):
    await set_owner(factory)
    path = "/api/v1/engines/codex/login"
    async with client(app) as one, client(app) as two:
        first = await login(one)
        second = await login(two)
        assert (await one.post(path)).status_code == 403
        result = await one.post(path, headers={"X-CSRF-Token": first["csrf_token"]})
        assert result.status_code == 202, result.text
        assert result.headers["cache-control"] == "no-store"
        flow_path = f"{path}/{result.json()['id']}"
        read = await one.get(flow_path)
        assert read.json()["user_code"] == "AB12-CDE34"
        assert read.headers["cache-control"] == "no-store"
        assert "owner" not in read.json()
        assert (await two.get(flow_path)).status_code == 404
        assert (await two.delete(flow_path, headers={"X-CSRF-Token": second["csrf_token"]})).status_code == 404
        assert (await one.delete(flow_path)).status_code == 403
        assert (await one.delete(flow_path, headers={"X-CSRF-Token": first["csrf_token"]})).status_code == 204


async def test_chat_device_cannot_start_or_read_login(app, factory, controller):
    await set_owner(factory)
    path = "/api/v1/engines/codex/login"
    async with client(app) as admin, client(app) as phone:
        session = await login(admin)
        code = await pairing_code(admin, session["csrf_token"], scope="chat")
        paired = await phone.post("/api/v1/auth/pair", json={"code": code})
        assert paired.status_code == 200, paired.text
        token = paired.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert (await phone.post(path, headers=headers)).status_code == 403
        assert (await phone.get(path + "/" + "a" * 32, headers=headers)).status_code == 403
