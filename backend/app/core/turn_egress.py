"""Per-turn public HTTP(S) gate and isolated loopback bridge, stdlib only.

The supervisor runs outside bubblewrap. The bridge runs inside its network
namespace. Only the gate's UNIX socket crosses that boundary. No destination
name is resolved by a client or looked up again after authorization.
"""
from __future__ import annotations

import contextlib
import ipaddress
import os
from pathlib import Path
import selectors
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

HEADER_LIMIT = 32768
SOCKET_PATH = '/run/turn-egress/gate.sock'
PROXY_PORT = 18764


class Refused(ValueError):
    pass


def local_addresses() -> set[str]:
    """Include public interface addresses, not just the RFC private ranges."""
    addresses = {'127.0.0.1', '::1'}
    try:
        addresses.update(answer[4][0] for answer in socket.getaddrinfo(socket.gethostname(), None))
    except OSError:
        pass
    if sys.platform.startswith('linux'):
        # /proc lists all local IPv4 addresses, including interfaces whose
        # primary address is public; if unavailable, refuse the gate request.
        previous = ''
        for line in Path('/proc/net/fib_trie').read_text().splitlines():
            if '/32 host LOCAL' in line:
                addresses.add(previous.split()[-1])
            previous = line
        for line in Path('/proc/net/if_inet6').read_text().splitlines():
            addresses.add(str(ipaddress.IPv6Address(int(line.split()[0], 16))))
    return addresses


def public_addresses(host: str, port: int) -> list[tuple]:
    """Reject the entire DNS answer if any candidate is non-public."""
    if port not in (80, 443) or not host or '%' in host:
        raise Refused('Only public HTTP(S) destinations are allowed')
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers:
        raise Refused('No destination address')
    own = local_addresses()
    for family, _, _, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        # IPv4-mapped IPv6, NAT64 and transition routes can target an address
        # different from the literal one checked here. Do not admit them.
        if (family not in (socket.AF_INET, socket.AF_INET6) or not ip.is_global
                or str(ip) in own or str(ip) == '168.63.129.16'
                or ip.is_multicast or getattr(ip, 'ipv4_mapped', None)
                or ip in ipaddress.ip_network('64:ff9b::/96')
                or ip in ipaddress.ip_network('64:ff9b:1::/48')
                or ip in ipaddress.ip_network('2002::/16')
                or ip in ipaddress.ip_network('2001::/32')):
            raise Refused('Destination is not a public address')
    return answers


def connect_public(host: str, port: int) -> socket.socket:
    answers = public_addresses(host, port)
    for family, kind, protocol, _, address in answers:
        upstream = socket.socket(family, kind, protocol)
        upstream.settimeout(15)
        try:
            upstream.connect(address)  # numeric sockaddr; never a second DNS lookup
            return upstream
        except OSError:
            upstream.close()
    raise OSError('Public destination could not be reached')


def relay(left: socket.socket, right: socket.socket) -> None:
    """Bound memory, connection count and idle lifetime; propagate half-close."""
    with selectors.DefaultSelector() as selector:
        selector.register(left, selectors.EVENT_READ, right)
        selector.register(right, selectors.EVENT_READ, left)
        left.settimeout(60)
        right.settimeout(60)
        while selector.get_map():
            ready = selector.select(60)
            if not ready:
                return
            for key, _ in ready:
                chunk = key.fileobj.recv(65536)
                if chunk:
                    key.data.sendall(chunk)
                else:
                    selector.unregister(key.fileobj)
                    with contextlib.suppress(OSError):
                        key.data.shutdown(socket.SHUT_WR)


def read_request(client: socket.socket) -> tuple[str, str, bytes, bytes]:
    data = bytearray()
    while b'\r\n\r\n' not in data:
        chunk = client.recv(min(4096, HEADER_LIMIT + 1 - len(data)))
        if not chunk or len(data) + len(chunk) > HEADER_LIMIT:
            raise Refused('Invalid proxy request')
        data.extend(chunk)
    header, tail = bytes(data).split(b'\r\n\r\n', 1)
    line, _, headers = header.partition(b'\r\n')
    try:
        method, target, version = line.decode('ascii').split(' ')
    except (UnicodeError, ValueError) as exc:
        raise Refused('Invalid request line') from exc
    if version not in ('HTTP/1.0', 'HTTP/1.1') or not method.isalpha():
        raise Refused('Invalid request line')
    return method, target, headers, tail


def proxy_request(client: socket.socket) -> None:
    client.settimeout(15)
    connected = False
    try:
        method, target, headers, tail = read_request(client)
        if method == 'CONNECT':
            parsed = urlsplit('//' + target)
            if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise Refused('Invalid CONNECT destination')
            host, port = parsed.hostname, parsed.port
            if not host or port is None:
                raise Refused('CONNECT requires a host and port')
            outgoing = b''
        else:
            parsed = urlsplit(target)
            if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise Refused('An absolute HTTP URL is required')
            host, port = parsed.hostname, parsed.port or 80
            # Replace authority headers and connection handling. No proxy
            # credentials are forwarded; HTTP redirects require a new check.
            kept = []
            for line in headers.split(b'\r\n'):
                name, sep, _ = line.partition(b':')
                if not sep or name.strip() != name or b'\x00' in line:
                    raise Refused('Invalid header')
                if name.lower() not in (b'host', b'connection', b'proxy-connection', b'proxy-authorization'):
                    kept.append(line)
            authority = f'[{host}]' if ':' in host else host
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            outgoing = (b'\r\n'.join([f'{method} {path} HTTP/1.1'.encode('ascii'),
                                     f'Host: {authority}:{port}'.encode('ascii'), b'Connection: close', *kept])
                        + b'\r\n\r\n')
        with connect_public(host, port) as upstream:
            connected = True
            if method == 'CONNECT':
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            elif outgoing:
                upstream.sendall(outgoing)
            if tail:
                upstream.sendall(tail)
            relay(client, upstream)
    except (Refused, ValueError, UnicodeError):
        if not connected:
            with contextlib.suppress(OSError):
                client.sendall(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
    except OSError:
        if not connected:
            with contextlib.suppress(OSError):
                client.sendall(b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')


class BoundedServer(socketserver.ThreadingMixIn):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class GateHandler(socketserver.BaseRequestHandler):
    def handle(self):
        proxy_request(self.request)


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.socket(socket.AF_UNIX) as upstream:
                upstream.settimeout(15)
                upstream.connect(SOCKET_PATH)
                relay(self.request, upstream)
        except OSError:
            return


class GateServer(BoundedServer, socketserver.UnixStreamServer):
    pass


class BridgeServer(BoundedServer, socketserver.TCPServer):
    allow_reuse_address = True


def run_child(args: list[str]) -> int:
    child = subprocess.Popen(args)
    old = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        old[sig] = signal.signal(sig, lambda number, frame: child.send_signal(number))
    try:
        result = child.wait()
        return result if result >= 0 else 128 - result
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        if child.poll() is None:
            child.kill()
            child.wait()


def supervise(args: list[str]) -> int:
    """Keep the host gate alive exactly as long as the boxed process."""
    # A short fixed /tmp prefix keeps AF_UNIX paths below Linux's 108 bytes.
    with tempfile.TemporaryDirectory(prefix='duet-egress-', dir='/tmp') as directory:
        gate_path = str(Path(directory) / 'gate.sock')
        with GateServer(gate_path, GateHandler) as server:
            os.chmod(gate_path, 0o600)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # The marker is inserted by the trusted sandbox planner.
                args = [gate_path if arg == '__TURN_EGRESS_SOCKET__' else arg for arg in args]
                return run_child(args)
            finally:
                server.shutdown()
                thread.join(timeout=2)


def bridge(args: list[str]) -> int:
    # A missing gate is a launch error, rather than a provider sign-in error.
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(5)
        probe.connect(SOCKET_PATH)
    with BridgeServer(('127.0.0.1', PROXY_PORT), BridgeHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return run_child(args)
        finally:
            server.shutdown()
            thread.join(timeout=2)


if __name__ == '__main__':
    try:
        mode, *arguments = sys.argv[1:]
        raise SystemExit({'supervise': supervise, 'bridge': bridge}[mode](arguments))
    except (KeyError, ValueError, OSError):
        print('The isolated network gate could not start; no unboxed retry is allowed.', file=sys.stderr)
        raise SystemExit(125)
