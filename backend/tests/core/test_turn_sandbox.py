"""Mount planning, refusal and optional real-Linux namespace qualification."""
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.core import turn_sandbox as box
from app.core.config import settings


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'TURN_SANDBOX_ENABLED', True)
    monkeypatch.setattr(settings, 'TURN_SANDBOX_READONLY_PATHS', '')
    monkeypatch.setattr(settings, 'TURN_SANDBOX_WRITABLE_PATHS', '')
    monkeypatch.setattr(settings, 'SQLITE_PATH', str(tmp_path / 'data' / 'app.db'))
    return tmp_path


@pytest.fixture
def fake_linux(monkeypatch, enabled):
    monkeypatch.setattr(box.sys, 'platform', 'linux')
    monkeypatch.setattr(box.profile, 'active', lambda: False)
    monkeypatch.setattr(box.shutil, 'which', lambda name: '/usr/bin/bwrap')
    return enabled


def test_off_is_exact_argv_and_does_not_probe(monkeypatch):
    monkeypatch.setattr(settings, 'TURN_SANDBOX_ENABLED', False)
    args = ['codex', 'app-server']
    assert box.command(args, cwd='/does-not-exist', env={}) is args
    assert box.self_check()['status'] == 'off'


@pytest.mark.parametrize('desktop,platform', [(True, 'linux'), (False, 'darwin'), (False, 'win32')])
def test_unsupported_enabled_refuses(monkeypatch, enabled, desktop, platform):
    monkeypatch.setattr(box.sys, 'platform', platform)
    monkeypatch.setattr(box.profile, 'active', lambda: desktop)
    with pytest.raises(box.SandboxUnavailable, match='Linux server'):
        box.command(['codex'], cwd=enabled, env={})


def test_missing_bwrap_refuses(monkeypatch, fake_linux):
    monkeypatch.setattr(box.shutil, 'which', lambda name: None)
    with pytest.raises(box.SandboxUnavailable, match='bubblewrap'):
        box.self_check()


def test_plan_has_minimal_root_private_proc_tmp_and_scoped_mailboxes(fake_linux):
    workspace = fake_linux / 'workspace'
    mailbox = fake_linux / 'mailbox'
    for path in (workspace, mailbox):
        path.mkdir()
    args = box.command(['codex', 'app-server'], cwd=workspace, env={}, writable=[mailbox])
    assert '--unshare-pid' in args and '--die-with-parent' in args and '--new-session' in args
    assert args[args.index('--proc') + 1] == '/proc'
    assert args[args.index('--tmpfs') + 1] == '/tmp'
    assert '--unshare-net' not in args
    assert ['--bind', str(workspace), str(workspace)] == args[args.index('--bind'):args.index('--bind') + 3]
    assert str(mailbox) in args
    assert not any(args[n + 1] == '/' for n, arg in enumerate(args[:-1]) if arg in ('--bind', '--ro-bind'))
    assert args[-3:] == ['--', 'codex', 'app-server']


def test_mounting_data_parent_or_symlink_to_home_refused(monkeypatch, fake_linux):
    workspace = fake_linux / 'workspace'
    workspace.mkdir()
    link = fake_linux / 'home-link'
    link.symlink_to(Path.home(), target_is_directory=True)
    for granted in (fake_linux, link):
        with pytest.raises(box.SandboxUnavailable, match='application data'):
            box.command(['codex'], cwd=workspace, env={}, writable=[granted])


def test_shared_legacy_workspace_refuses_other_agent_exposure(monkeypatch, fake_linux):
    from app.services import local_claude_runner
    monkeypatch.setattr(local_claude_runner, 'workspace_dir', lambda: fake_linux / 'local' / 'workspace')
    workspace = fake_linux / 'local' / 'workspace'
    (workspace / 'agents' / '1').mkdir(parents=True)
    with pytest.raises(box.SandboxUnavailable, match='agent workspace'):
        box.command(['codex'], cwd=workspace, env={})
    args = box.command(['codex'], cwd=workspace / 'agents' / '1', env={})
    assert str(workspace) not in args


def test_only_named_engine_login_is_exposed(fake_linux):
    home = fake_linux / 'home'
    for name in ('.codex', '.claude'):
        (home / name).mkdir(parents=True)
    workspace = fake_linux / 'workspace'
    workspace.mkdir()
    args = box.command(['codex'], cwd=workspace, env={'HOME': str(home)})
    assert str(home / '.codex') in args
    assert str(home / '.claude') not in args
    assert '--dir' in args


@pytest.mark.parametrize('returncode,stdout', [(1, ''), (0, ''), (0, 'pid:[host]')])
def test_self_check_rejects_failed_or_host_namespace(monkeypatch, fake_linux, returncode, stdout):
    monkeypatch.setattr(box.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout))
    monkeypatch.setattr(box.os, 'readlink', lambda path: 'pid:[host]')
    with pytest.raises(box.SandboxUnavailable, match='self-check failed'):
        box.self_check()


def test_blocking_launch_does_not_fall_back(monkeypatch, enabled):
    from app.services import local_claude_runner as runner
    monkeypatch.setattr(box, 'self_check', lambda: (_ for _ in ()).throw(box.SandboxUnavailable('no box')))
    monkeypatch.setattr(runner, '_child_env', lambda: {})
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: pytest.fail('unboxed spawn'))
    with pytest.raises(box.SandboxUnavailable):
        runner.run_blocking(['engine'], stdin_text='hello', cwd=enabled, timeout_seconds=2,
                            max_output_bytes=100, sandbox=True)


@pytest.mark.asyncio
async def test_rpc_launch_does_not_fall_back(monkeypatch, enabled):
    from app.services.stdio_rpc import StdioRpc
    monkeypatch.setattr(box, 'self_check', lambda: (_ for _ in ()).throw(box.SandboxUnavailable('no box')))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', lambda *a, **k: pytest.fail('unboxed spawn'))
    with pytest.raises(box.SandboxUnavailable):
        await StdioRpc.spawn(['engine'], cwd=str(enabled), max_bytes=100, sandbox=True)


@pytest.mark.skipif(not sys.platform.startswith('linux') or not shutil.which('bwrap'), reason='requires real Linux bubblewrap')
def test_real_linux_private_files_process_namespace_and_writable_workspace(enabled):
    report = box.self_check()
    assert report['status'] == 'available'
    workspace = enabled / 'workspace'
    workspace.mkdir()
    canary = enabled / 'private-canary'
    canary.write_text('secret')
    args = box.command(['/bin/sh', '-c', 'test ! -e "$1" && echo ok > result && readlink /proc/self/ns/pid',
                        'check', str(canary)], cwd=workspace, env={})
    done = subprocess.run(args, capture_output=True, text=True, timeout=10, env={'PATH': '/usr/bin:/bin'})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() != os.readlink('/proc/self/ns/pid')
    assert (workspace / 'result').read_text().strip() == 'ok'


def test_claude_reports_sandbox_refusal_without_generic_runner_error():
    from app.services.claude_adapter import ClaudeAdapter
    parser = SimpleNamespace(model=None, usage=None, init_seen=False, unflushed_text='')
    attempt = SimpleNamespace(normalizer=parser, error=box.SandboxUnavailable('Sandbox required but unavailable'))
    outcome = ClaudeAdapter()._outcome(SimpleNamespace(aborted=False), attempt, tools=False)
    assert outcome.error_code == 'sandbox_unavailable'
    assert outcome.error == 'Sandbox required but unavailable'
