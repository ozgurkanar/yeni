from __future__ import annotations

import os
import re
import select
import socket
import socketserver
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .util import StopRequested


class ProxyError(RuntimeError):
    pass


def normalize(host: str) -> str:
    return (
        (host or "")
        .strip()
        .lower()
        .rstrip(".")
    )


def host_matches(
    host: str,
    rules: tuple[str, ...],
) -> bool:
    host = normalize(host)
    return any(
        host == normalize(rule)
        or (
            rule.startswith("*.")
            and host.endswith(
                normalize(rule)[1:]
            )
        )
        for rule in rules
    )


def read_head(
    sock: socket.socket,
    max_bytes: int = 131072,
):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ProxyError(
                "HTTP header çok büyük"
            )

    position = data.find(b"\r\n\r\n")
    if position < 0:
        raise ProxyError(
            "Eksik HTTP header"
        )
    return (
        bytes(data[: position + 4]),
        bytes(data[position + 4 :]),
    )


def parse_destination(head: bytes):
    text = head.decode(
        "iso-8859-1",
        "replace",
    )
    lines = text.split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) != 3:
        raise ProxyError(
            "Geçersiz proxy isteği"
        )

    method, target, version = parts
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower().strip()] = (
                value.strip()
            )

    if method.upper() == "CONNECT":
        if ":" in target:
            host, port = target.rsplit(":", 1)
        else:
            host, port = target, "443"
        return (
            method.upper(),
            normalize(host),
            int(port),
            target,
            version,
            headers,
        )

    parsed = urlsplit(target)
    host = (
        parsed.hostname
        or headers.get("host", "").split(":")[0]
    )
    port = (
        parsed.port
        or (
            443
            if parsed.scheme == "https"
            else 80
        )
    )
    return (
        method.upper(),
        normalize(host),
        port,
        target,
        version,
        headers,
    )


def rewrite_origin(
    head: bytes,
    target: str,
) -> bytes:
    parsed = urlsplit(target)
    if not parsed.scheme:
        return head

    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    lines = (
        head.decode(
            "iso-8859-1",
            "replace",
        )
        .split("\r\n")
    )
    first = lines[0].split(" ", 2)
    lines[0] = (
        f"{first[0]} {path} {first[2]}"
    )
    lines = [
        line
        for line in lines
        if not line.lower().startswith(
            "proxy-connection:"
        )
    ]
    return "\r\n".join(lines).encode(
        "iso-8859-1",
        "replace",
    )


class SelectiveGate:
    def __init__(
        self,
        port: int,
        upstream_port: int,
        rules: tuple[str, ...],
        log,
    ):
        self.port = port
        self.upstream_port = upstream_port
        self.rules = rules
        self.log = log
        self.server = None
        self.thread = None
        self.lock = threading.RLock()

    def start(self) -> None:
        with self.lock:
            if self.server:
                return

            gate = self

            class Server(
                socketserver.ThreadingTCPServer
            ):
                allow_reuse_address = True
                daemon_threads = True

            class Handler(
                socketserver.BaseRequestHandler
            ):
                def handle(self):
                    gate.handle(self.request)

            self.server = Server(
                ("127.0.0.1", self.port),
                Handler,
            )
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                daemon=True,
                name="SelectiveProxyGate",
            )
            self.thread.start()

        self.log(
            f"Seçici geçit hazır: "
            f"127.0.0.1:{self.port}"
        )

    def stop(self) -> None:
        with self.lock:
            server = self.server
            thread = self.thread
            self.server = None
            self.thread = None

        if server:
            server.shutdown()
            server.server_close()
        if (
            thread
            and thread.is_alive()
        ):
            thread.join(timeout=3)

        if server:
            self.log(
                "Seçici geçit kapatıldı."
            )

    @staticmethod
    def tunnel(
        left: socket.socket,
        right: socket.socket,
    ) -> None:
        left.settimeout(None)
        right.settimeout(None)

        while True:
            ready, _, bad = select.select(
                [left, right],
                [],
                [left, right],
                60,
            )
            if bad:
                return
            for source in ready:
                target = (
                    right
                    if source is left
                    else left
                )
                data = source.recv(65536)
                if not data:
                    return
                target.sendall(data)

    def handle(
        self,
        client: socket.socket,
    ) -> None:
        remote = None
        try:
            client.settimeout(15)
            head, buffered = read_head(client)
            (
                method,
                host,
                port,
                target,
                version,
                _headers,
            ) = parse_destination(head)

            if host_matches(
                host,
                self.rules,
            ):
                remote = socket.create_connection(
                    (
                        "127.0.0.1",
                        self.upstream_port,
                    ),
                    15,
                )
                remote.sendall(head)
            else:
                remote = socket.create_connection(
                    (host, port),
                    15,
                )
                if method == "CONNECT":
                    client.sendall(
                        (
                            f"{version} "
                            "200 Connection Established"
                            "\r\n\r\n"
                        ).encode()
                    )
                else:
                    remote.sendall(
                        rewrite_origin(
                            head,
                            target,
                        )
                    )

            if buffered:
                remote.sendall(buffered)
            self.tunnel(client, remote)
        except Exception:
            try:
                client.sendall(
                    b"HTTP/1.1 502 Bad Gateway"
                    b"\r\nConnection: close"
                    b"\r\n\r\n"
                )
            except OSError:
                pass
        finally:
            try:
                client.close()
            except OSError:
                pass
            if remote:
                try:
                    remote.close()
                except OSError:
                    pass


class CharlesManager:
    CANDIDATES = (
        r"C:\Program Files\Charles\Charles.exe",
        r"C:\Program Files\Charles\Charles64.exe",
        r"C:\Program Files (x86)\Charles\Charles.exe",
    )

    def __init__(
        self,
        exe: str,
        port: int,
        log,
    ):
        self.exe = exe
        self.port = port
        self.log = log
        self.process: subprocess.Popen | None = None
        self.started_by_runtime = False

    def _probe(self) -> bool:
        try:
            with socket.create_connection(
                ("127.0.0.1", self.port),
                1.5,
            ) as connection:
                connection.sendall(
                    b"GET http://127.0.0.1:1/ "
                    b"HTTP/1.1\r\n"
                    b"Host: 127.0.0.1:1\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return connection.recv(
                    16
                ).startswith(b"HTTP/")
        except OSError:
            return False

    def ensure(
        self,
        timeout_s: float = 45,
        stop_event: threading.Event | None = None,
    ) -> None:
        if self._probe():
            self.started_by_runtime = False
            self.log(
                f"Charles zaten açık: "
                f"127.0.0.1:{self.port}"
            )
            return

        executable = self.exe
        if not executable:
            executable = next(
                (
                    candidate
                    for candidate in self.CANDIDATES
                    if Path(candidate).is_file()
                ),
                "",
            )
        if (
            not executable
            or not Path(executable).is_file()
        ):
            raise ProxyError(
                "Charles.exe bulunamadı."
            )

        self.process = subprocess.Popen(
            [executable],
            cwd=str(Path(executable).parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
        self.started_by_runtime = True

        deadline = (
            time.monotonic()
            + timeout_s
        )
        while time.monotonic() < deadline:
            if (
                stop_event is not None
                and stop_event.is_set()
            ):
                raise StopRequested(
                    "Charles açılırken durdurma istendi."
                )
            if self._probe():
                self.log(
                    f"Charles başlatıldı: "
                    f"127.0.0.1:{self.port}"
                )
                return
            time.sleep(0.5)

        raise ProxyError(
            f"Charles portu açılmadı: "
            f"{self.port}"
        )

    def _listener_pids(self) -> set[int]:
        if os.name != "nt":
            return set()

        try:
            result = subprocess.run(
                [
                    "netstat",
                    "-ano",
                    "-p",
                    "tcp",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=12,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                ),
            )
        except Exception:
            return set()

        pids: set[int] = set()
        pattern = re.compile(
            rf"^\s*TCP\s+\S+:{self.port}\s+"
            rf"\S+\s+LISTENING\s+(\d+)\s*$",
            re.IGNORECASE,
        )
        for line in result.stdout.splitlines():
            match = pattern.match(line)
            if match:
                pids.add(int(match.group(1)))
        return pids

    @staticmethod
    def _taskkill(pid: int) -> None:
        if os.name != "nt":
            return
        subprocess.run(
            [
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
            ),
        )

    def stop_if_owned(
        self,
        timeout_s: float = 20,
    ) -> None:
        if not self.started_by_runtime:
            if self._probe():
                self.log(
                    "Charles çalışma öncesinde zaten "
                    "açıktı; program tarafından kapatılmadı."
                )
            return

        self.log(
            "Runtime tarafından açılan Charles kapatılıyor."
        )

        if (
            self.process is not None
            and self.process.poll() is None
        ):
            try:
                self.process.terminate()
                self.process.wait(timeout=8)
            except Exception:
                try:
                    self._taskkill(
                        self.process.pid
                    )
                except Exception:
                    pass

        deadline = (
            time.monotonic()
            + timeout_s
        )
        while (
            time.monotonic() < deadline
            and self._probe()
        ):
            for pid in self._listener_pids():
                self._taskkill(pid)
            time.sleep(0.5)

        if self._probe():
            raise ProxyError(
                "Charles kapanmadı; "
                f"port hâlâ açık: {self.port}"
            )

        self.process = None
        self.started_by_runtime = False
        self.log(
            "Charles kapatıldı."
        )
