"""Real bounded child processes; no provider account or credentials."""
import asyncio
import os
import sys

import pytest

from app.services import codex_login

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")


@pytest.fixture
async def login(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_login, "child_env", lambda: {**os.environ, "CODEX_HOME": str(tmp_path)})
    script = tmp_path / "fake-codex"
    script.write_text(f'''#!{sys.executable}
import os, pathlib, sys, time
home = pathlib.Path(os.environ["CODEX_HOME"])
if sys.argv[1:] == ["login", "status"]:
    if (home / "account").exists():
        print("Logged in using ChatGPT", file=sys.stderr)
        sys.exit(0)
    print("Not logged in", file=sys.stderr)
    sys.exit(1)
assert sys.argv[1:] == ["login", "--device-auth"]
(home / "pid").write_text(str(os.getpid()))
mode = (home / "mode").read_text() if (home / "mode").exists() else "wait"
if mode == "overflow":
    print("SECRET_PROVIDER_TEXT" * 2000, flush=True)
else:
    target = "https://attacker.example/codex/device" if mode == "forged" else "https://auth.openai.com/codex/device"
    for fragment in ["\\x1b[3", "2m" + target, "\\x1b[0m\\nEnter code AB12-", "CDE34\\n"]:
        sys.stderr.write(fragment)
        sys.stderr.flush()
        time.sleep(.01)
while not (home / "approve").exists():
    time.sleep(0.01)
(home / "account").write_text("private-token-never-returned")
''')
    script.chmod(0o700)
    manager = codex_login.CodexLogin()
    yield manager, script, tmp_path
    await manager.close()


async def until(manager, flow, predicate):
    async with asyncio.timeout(5):
        while True:
            result = manager.view("browser:1", flow["id"])
            if predicate(result):
                return result
            await asyncio.sleep(.01)


async def test_private_code_and_confirmed_success(login):
    manager, script, home = login
    flow = await manager.start("browser:1", script)
    waiting = await until(manager, flow, lambda s: "user_code" in s)
    assert waiting["user_code"] == "AB12-CDE34"
    assert waiting["authorization_url"] == codex_login.DEVICE_URL
    with pytest.raises(KeyError):
        manager.view("browser:2", flow["id"])
    with pytest.raises(KeyError):
        await manager.cancel("browser:2", flow["id"])
    with pytest.raises(codex_login.LoginError):
        await manager.start("browser:2", script)
    (home / "approve").touch()
    result = await until(manager, flow, lambda s: s["phase"] != "running")
    assert result == {"id": flow["id"], "phase": "succeeded"}
    assert "private-token" not in str(result)


async def test_cancel_kills_child_and_releases_home_lock(login):
    manager, script, home = login
    flow = await manager.start("browser:1", script)
    await until(manager, flow, lambda s: "user_code" in s)
    other = codex_login.CodexLogin()
    with pytest.raises(codex_login.LoginError):
        await other.start("browser:2", script)
    pid = int((home / "pid").read_text())
    await manager.cancel("browser:1", flow["id"])
    assert manager.view("browser:1", flow["id"])["phase"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    await other.start("browser:2", script)
    await other.close()


async def test_cancel_before_worker_starts_does_not_leak_lock(login):
    manager, script, home = login
    await manager.start("browser:1", script)
    await manager.close()
    await manager.start("browser:1", script)
    await manager.close()


async def test_expiry_kills_child_and_scrubs_code(login, monkeypatch):
    manager, script, home = login
    processes = []
    actual_spawn = asyncio.create_subprocess_exec
    async def capture_spawn(*args, **kwargs):
        process = await actual_spawn(*args, **kwargs)
        if "--device-auth" in args:
            processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    manager.lifetime = .2
    flow = await manager.start("browser:1", script)
    result = await until(manager, flow, lambda s: s["phase"] != "running")
    assert result["phase"] == "expired"
    assert "user_code" not in result
    assert len(processes) == 1
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


async def test_forged_url_is_never_exposed(login):
    manager, script, home = login
    manager.prompt_timeout = .2
    (home / "mode").write_text("forged")
    flow = await manager.start("browser:1", script)
    result = await until(manager, flow, lambda s: s["phase"] != "running")
    assert result["phase"] == "expired"
    assert "user_code" not in result and "authorization_url" not in result


async def test_overflow_failure_never_echoes_output(login):
    manager, script, home = login
    (home / "mode").write_text("overflow")
    flow = await manager.start("browser:1", script)
    result = await until(manager, flow, lambda s: s["phase"] != "running")
    assert result["phase"] == "failed"
    assert "SECRET_PROVIDER_TEXT" not in str(result)


async def test_existing_login_is_preserved(login):
    manager, script, home = login
    (home / "account").write_text("previous-account")
    flow = await manager.start("browser:1", script)
    result = await until(manager, flow, lambda s: s["phase"] != "running")
    assert result["phase"] == "succeeded"
    assert not (home / "pid").exists()
    assert (home / "account").read_text() == "previous-account"


async def test_cancel_during_process_creation_reaps_child(login, monkeypatch):
    manager, script, home = login
    started = asyncio.Event()
    release_spawn = asyncio.Event()
    processes = []
    actual_spawn = asyncio.create_subprocess_exec
    async def delayed_spawn(*args, **kwargs):
        process = await actual_spawn(*args, **kwargs)
        if "--device-auth" in args:
            processes.append(process)
            started.set()
            await release_spawn.wait()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    flow = await manager.start("browser:1", script)
    await asyncio.wait_for(started.wait(), 3)
    closing = asyncio.create_task(manager.close())
    # Yield to close() so cancellation arrives while OS creation is pending.
    await asyncio.sleep(0)
    assert manager.task.cancelling()
    release_spawn.set()
    await asyncio.gather(closing, manager.close())
    assert manager.view("browser:1", flow["id"])["phase"] == "cancelled"
    assert len(processes) == 1
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)
