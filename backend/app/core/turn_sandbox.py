"""Opt-in Linux filesystem/PID box for engine turns (stage C).

A minimal root is populated from explicit mounts. This does NOT restrict
network traffic; do not treat it as protection from hostile unattended input.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping

from app.core.config import settings
from app.desktop import profile


class SandboxUnavailable(OSError):
    """A requested sandbox cannot be provided; never retry unboxed."""


def _path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=True)


def _contains(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents


def _check_grant(path: Path) -> None:
    # An operator may expose runtime/login folders, never the database or its
    # enclosing data directory. Also reject a whole home or filesystem root.
    from app.core import app_settings
    from app.services.workspaces import canonical_workspace
    agents_root = canonical_workspace(app_settings.get("LOCAL_WORKSPACE_ROOT")) / "agents"
    protected = [settings.SQLITE_FILE.resolve(), Path.home().resolve(), agents_root]
    for value in (settings.ENGINE_PROCESS_DIR, settings.DECISION_LOG_DIR, settings.ATTACHMENTS_DIR):
        if value.strip():
            protected.append(Path(value).expanduser().resolve())
    unsafe_tree = any(_contains(Path(root), path) for root in ('/proc', '/sys', '/dev', '/run'))
    if path in (Path('/'), Path('/tmp')) or unsafe_tree or any(_contains(path, item) for item in protected):
        raise SandboxUnavailable('A sandbox mount would expose the home or application data. Use a narrower folder.')


def mailbox_mounts(launches: Iterable) -> list[Path]:
    """Only backend-created launch descriptors may grant a mailbox to a turn."""
    if not settings.TURN_SANDBOX_ENABLED:
        return []
    result = []
    for launch in launches:
        if launch is None:
            continue
        args = list(launch.args)
        if '--mailbox' in args:
            index = args.index('--mailbox')
            if index + 1 >= len(args):
                raise SandboxUnavailable('Invalid sandbox mailbox descriptor.')
            result.append(_path(args[index + 1]))
    return result


def _base() -> list[str]:
    if not sys.platform.startswith('linux') or profile.active():
        raise SandboxUnavailable('The turn sandbox requires Linux server mode; no engine was started.')
    exe = shutil.which('bwrap')
    if exe is None:
        raise SandboxUnavailable('The turn sandbox requires bubblewrap; no engine was started.')
    result = [exe, '--die-with-parent', '--new-session', '--unshare-user', '--unshare-pid',
              '--unshare-ipc', '--unshare-uts', '--cap-drop', 'ALL', '--proc', '/proc',
              '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run',
              '--setenv', 'TMPDIR', '/tmp', '--setenv', 'TMP', '/tmp', '--setenv', 'TEMP', '/tmp']
    # No host root bind: other users, application data, /run sockets and /tmp
    # mailboxes start absent. Keep standard runtimes read-only.
    for name in ('/usr', '/bin', '/sbin', '/lib', '/lib64'):
        if Path(name).exists():
            _check_grant(Path(name).resolve())
            result.extend(['--ro-bind', name, name])
    for name in ('/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/ssl',
                 '/etc/ld.so.cache', '/etc/localtime'):
        if Path(name).exists():
            _check_grant(Path(name).resolve())
            result.extend(['--ro-bind', name, name])
    return result


def command(args: list[str], *, cwd: str | Path, env: Mapping[str, str],
            writable: Iterable[Path] = (), readonly: Iterable[Path] = ()) -> list[str]:
    """Wrap one turn. Off preserves the existing argv exactly.

    Mount source paths are canonicalized to prevent a symlink granting a
    broader directory. Custom runtimes outside /usr need an explicit grant.
    """
    if not settings.TURN_SANDBOX_ENABLED:
        return args
    result = _base()
    work = _path(cwd)
    if not work.is_dir():
        raise SandboxUnavailable('The sandbox workspace must be a directory.')
    # The legacy default workspace holds every agent. Per-agent workspaces are
    # fine; a shared root would reveal other agents even inside a mount box.
    from app.services.local_claude_runner import workspace_dir
    if work == workspace_dir():
        raise SandboxUnavailable('Choose an agent workspace before using the turn sandbox.')
    _check_grant(work)
    read_paths = [Path(__file__).resolve().parents[1], Path(sys.prefix), *readonly]
    read_paths += [Path(x.strip()) for x in settings.TURN_SANDBOX_READONLY_PATHS.split(',') if x.strip()]
    write_paths = [work, *writable]
    # Engine account data remains accessible; credential brokering is a later
    # stage. Never bind the entire HOME or an entire XDG config directory.
    program = Path(args[0]).name.lower()
    names = ('CODEX_HOME',) if 'codex' in program else ('CLAUDE_CONFIG_DIR',) if 'claude' in program else ()
    default = '.codex' if 'codex' in program else '.claude' if 'claude' in program else None
    login_paths = [Path(env[name]) for name in names if env.get(name)]
    if not login_paths and default and env.get('HOME'):
        login_paths = [Path(env['HOME']) / default]
    write_paths += [p for p in login_paths if p.exists()]
    write_paths += [Path(x.strip()) for x in settings.TURN_SANDBOX_WRITABLE_PATHS.split(',') if x.strip()]
    # Empty home is created before binding named login folders into it.
    if env.get('HOME'):
        result.extend(['--dir', str(Path(env['HOME']).expanduser())])
    for flag, paths in (('--ro-bind', read_paths), ('--bind', write_paths)):
        for path in dict.fromkeys(_path(p) for p in paths):
            _check_grant(path)
            result.extend([flag, str(path), str(path)])
    result.extend(['--chdir', str(work), '--', *args])
    return result


def self_check() -> dict:
    """Probe the actual kernel permissions, without starting an engine.

    Called before each enabled turn, so a changed host configuration never
    falls through to an unboxed launch. No provider key is passed to the probe.
    """
    if not settings.TURN_SANDBOX_ENABLED:
        return {'status': 'off', 'reason': 'Not enabled', 'network': 'unrestricted'}
    args = _base()
    # $$ is a shell in a fresh PID namespace; the backend's /proc entry must
    # not be the same process. The namespace identifier provides a direct check.
    script = 'test ! -e "$1" && readlink /proc/self/ns/pid'
    import tempfile
    data_root = settings.SQLITE_FILE.expanduser().resolve().parent
    data_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sandbox-check-', dir=data_root) as folder:
        canary = Path(folder) / 'private-canary'
        canary.write_text('private', encoding='utf-8')
        try:
            done = subprocess.run([*args, '--', '/bin/sh', '-c', script, 'check', str(canary)],
                                  env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxUnavailable('The Linux sandbox self-check failed; no engine was started.') from exc
    if done.returncode or done.stdout.strip() == os.readlink('/proc/self/ns/pid') or not done.stdout.startswith('pid:['):
        raise SandboxUnavailable('The Linux sandbox self-check failed; no engine was started.')
    return {'status': 'available', 'filesystem': 'explicit mounts', 'processes': 'private PID namespace',
            'network': 'unrestricted'}


if __name__ == '__main__':
    import json
    try:
        print(json.dumps(self_check()))
    except SandboxUnavailable as error:
        print(json.dumps({'status': 'unavailable', 'reason': str(error), 'network': 'unrestricted'}))
        raise SystemExit(1)
