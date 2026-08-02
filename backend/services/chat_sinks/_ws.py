"""Minimal RFC 6455 WebSocket client over stdlib socket/ssl (no PyPI deps).

Covers exactly what the Kick Pusher sink needs: TLS handshake, masked text
frames, server ping auto-pong, close handling. Not a general client — no
extensions, no permessage-deflate, no fragmentation reassembly (Pusher never
fragments), single 1 MiB frame guard.

Module self-check runs a real local echo WebSocket server on 127.0.0.1 so
import proves the wire protocol without any external network.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from typing import Optional

MAX_FRAME_SIZE = 1 << 20  # 1 MiB — generous for Pusher chat events
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSClosed(Exception):
    """Connection closed (or close frame received)."""


class WSClient:
    """One WebSocket connection. Not thread-safe; single reader loop."""

    def __init__(self, url: str, *, origin: Optional[str] = None,
                 timeout: float = 15.0, max_frame: int = MAX_FRAME_SIZE):
        if url.startswith("wss://"):
            self._tls = True
            rest = url[len("wss://"):]
        elif url.startswith("ws://"):
            self._tls = False
            rest = url[len("ws://"):]
        else:
            raise ValueError(f"Unsupported WebSocket URL: {url[:40]}")
        host, _, path = rest.partition("/")
        if ":" in host and not host.startswith("["):
            self._host, _, port = host.partition(":")
            self._port = int(port)
        else:
            self._host, self._port = host, (443 if self._tls else 80)
        self._path = "/" + path
        self._origin = origin
        self._timeout = timeout
        self._max_frame = max_frame
        self._sock: Optional[socket.socket] = None
        self._recv_buf = b""

    # -- connection --------------------------------------------------------

    def connect(self) -> None:
        """Open the socket, TLS-wrap (wss) and complete the HTTP upgrade."""
        if self._sock is not None:
            return
        sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        try:
            if self._tls:
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=self._host)
            key = base64.b64encode(os.urandom(16)).decode()
            host_hdr = self._host if self._port in (80, 443) else f"{self._host}:{self._port}"
            req = (
                f"GET {self._path} HTTP/1.1\r\n"
                f"Host: {host_hdr}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"User-Agent: VOD.RIP/1.0\r\n"
                + (f"Origin: {self._origin}\r\n" if self._origin else "")
                + "\r\n"
            )
            sock.sendall(req.encode("ascii"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WSClosed("handshake: connection closed")
                buf += chunk
                if len(buf) > 64 * 1024:
                    raise ConnectionError("handshake: response headers too large")
            head, rest = buf.split(b"\r\n\r\n", 1)
            status = head.split(b"\r\n", 1)[0]
            if b" 101 " not in status:
                raise ConnectionError(f"handshake failed: {status.decode('ascii', 'replace')}")
            self._sock = sock
            self._recv_buf = rest
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._send_frame(0x8, b"")  # close handshake, best effort
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    # -- frames ------------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WSClosed("not connected")
        header = bytearray()
        header.append(0x80 | opcode)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv_text(self, timeout: Optional[float] = None) -> str:
        """Return the next text message; auto-pongs server pings."""
        if self._sock is None:
            raise WSClosed("not connected")
        self._sock.settimeout(timeout)
        while True:
            hdr = self._recv_exact(2)
            opcode = hdr[0] & 0x0F
            masked = bool(hdr[1] & 0x80)
            n = hdr[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            if n > self._max_frame:
                raise ConnectionError(f"frame too large: {n} bytes")
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                reason = payload[2:].decode("utf-8", "replace") if len(payload) >= 2 else ""
                raise WSClosed(reason)
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x1:  # text
                return payload.decode("utf-8", "replace")
            # 0x2 binary and continuation frames: ignore (not used by Pusher)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._recv_buf) < n:
            if self._sock is None:
                raise WSClosed("connection closed")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSClosed("connection closed")
            self._recv_buf += chunk
        out, self._recv_buf = self._recv_buf[:n], self._recv_buf[n:]
        return out


# ---------------------------------------------------------------------------
# Module self-check: real local echo WebSocket round-trip (no external network)
# ---------------------------------------------------------------------------

def _selfcheck_echo() -> None:
    import threading

    got: list[bytes] = []

    def server(port_holder: list[int]) -> None:
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        port_holder.append(lsock.getsockname()[1])
        conn, _ = lsock.accept()
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(4096)
            head = buf.split(b"\r\n\r\n", 1)[0]
            key_line = next(
                (ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key:")), b""
            )
            accept = base64.b64encode(
                hashlib.sha1(key_line.split(b":", 1)[1].strip() + _WS_GUID.encode()).digest()
            )
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            # server -> client: ping, then one text frame (unmasked)
            conn.sendall(bytes([0x89, 0x02]) + b"pp")
            payload = b'{"event":"pusher:ping","data":"{}"}'
            conn.sendall(bytes([0x81, len(payload)]) + payload)
            # read the client's masked pong + subscribe text frames
            while True:
                hdr = conn.recv(2)
                if not hdr:
                    return
                n = hdr[1] & 0x7F
                mask = conn.recv(4)
                body = conn.recv(n)
                if mask:
                    body = bytes(b ^ mask[i & 3] for i, b in enumerate(body))
                got.append(body)
                if body.startswith(b'{"event":"pusher:subscribe"'):
                    # echo a chat event back (unmasked text frame)
                    ev = b'{"event":"App\\\\Events\\\\ChatMessageEvent","data":"{\\"id\\":1}"}'
                    conn.sendall(bytes([0x81, len(ev)]) + ev)
        finally:
            conn.close()
            lsock.close()

    port_holder: list[int] = []
    t = threading.Thread(target=server, args=(port_holder,), daemon=True)
    t.start()
    while not port_holder:
        t.join(0.05)
    client = WSClient(f"ws://127.0.0.1:{port_holder[0]}/app/x?protocol=7", origin="https://kick.com")
    client.connect()
    client.send_text('{"event":"pusher:subscribe","data":{"channel":"chat.1"}}')
    msg = client.recv_text(timeout=5.0)  # ping arrives first; auto-ponged
    assert "pusher:ping" in msg, f"expected ping event, got: {msg[:80]!r}"
    msg2 = client.recv_text(timeout=5.0)
    assert "ChatMessageEvent" in msg2, f"expected chat event, got: {msg2[:80]!r}"
    client.close()
    t.join(5.0)
    assert any(body == b"pp" for body in got), "client must pong server pings"
    assert any(b"pusher:subscribe" in b for b in got), "client must send subscribe"


_selfcheck_echo()
