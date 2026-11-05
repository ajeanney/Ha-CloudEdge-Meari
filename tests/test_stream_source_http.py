"""Offline tests for the HTTP form of the local MPEG-TS stream.

HA hands ``stream_source`` to go2rtc. go2rtc reports no ``url`` for a raw
``tcp://`` producer, and HA's go2rtc client requires one, so every WebRTC
offer fails while that producer runs. The camera therefore advertises
``http://host:port/stream.ts?token=...``. Without the token the server refuses
the client; raw TCP readers are only served from loopback.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import resource
import socket
import time
import unittest
from types import SimpleNamespace

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
CAMERA = MODULES["camera"].CloudEdgeMeariCamera
STREAM_SERVER = importlib.import_module(
    "custom_components.cloudplus.coordinator.stream_server"
)
PACKET = b"G" + b"\x00" * 187


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _read_until(sock: socket.socket, marker: bytes, timeout: float = 3.0) -> bytes:
    sock.settimeout(timeout)
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


class CameraStreamSourceTests(unittest.TestCase):
    def test_stream_source_is_http(self) -> None:
        cam = CAMERA.__new__(CAMERA)
        cam._coordinator = SimpleNamespace(
            stream_port=41234, stream_host="192.168.1.10", stream_token="s3cret"
        )
        source = asyncio.run(cam.stream_source())
        self.assertEqual(source, "http://192.168.1.10:41234/stream.ts?token=s3cret")

    def test_no_stream_source_without_a_port(self) -> None:
        cam = CAMERA.__new__(CAMERA)
        cam._coordinator = SimpleNamespace(stream_port=0, stream_host="192.168.1.10")
        self.assertIsNone(asyncio.run(cam.stream_source()))


class StreamServerHttpTests(unittest.TestCase):
    def _server(self):
        server = STREAM_SERVER.StreamServer()
        server.start()
        self.addCleanup(server.stop)
        return server

    def _connect(self, server) -> socket.socket:
        viewer = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        self.addCleanup(viewer.close)
        return viewer

    @staticmethod
    def _request(server, method: str = "GET", target: str | None = None) -> bytes:
        target = target or f"/stream.ts?token={server.token}"
        return f"{method} {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode()

    def _refused(self, server, request: bytes, status: bytes) -> None:
        viewer = self._connect(server)
        viewer.sendall(request)
        self.assertTrue(_read_until(viewer, b"\r\n\r\n").startswith(status))
        time.sleep(0.2)
        self.assertEqual(server.client_count, 0)

    def test_http_get_gets_a_header_then_the_stream(self) -> None:
        server = self._server()
        viewer = self._connect(server)
        viewer.sendall(self._request(server))
        self.assertTrue(_wait(lambda: server.client_count == 1))
        server.broadcast(PACKET)
        head, _, body = _read_until(viewer, b"G").partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b"Content-Type: video/mp2t", head)
        self.assertTrue(body.startswith(b"G"))

    def test_raw_tcp_reader_gets_the_stream_without_a_header(self) -> None:
        server = self._server()
        viewer = self._connect(server)
        self.assertTrue(_wait(lambda: server.client_count == 1))
        server.broadcast(PACKET)
        self.assertTrue(_read_until(viewer, b"G").startswith(b"G"))

    def test_raw_tcp_reader_off_loopback_is_dropped(self) -> None:
        server_end, viewer = socket.socketpair()
        self.addCleanup(server_end.close)
        self.addCleanup(viewer.close)
        self.assertFalse(STREAM_SERVER._http_handshake(server_end, "s3cret", False))
        self.assertTrue(STREAM_SERVER._http_handshake(server_end, "s3cret", True))

    def test_raw_tcp_reader_from_a_lan_address_is_dropped(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 80))  # no packet sent; picks the LAN IP
                lan_ip = probe.getsockname()[0]
        except OSError:
            self.skipTest("no non-loopback IPv4 address")
        if lan_ip.startswith("127."):
            self.skipTest("no non-loopback IPv4 address")
        server = self._server()
        viewer = socket.create_connection((lan_ip, server.port), timeout=2)
        self.addCleanup(viewer.close)
        self.assertEqual(viewer.recv(1), b"")
        self.assertEqual(server.client_count, 0)

    def test_other_path_gets_404_and_is_not_counted(self) -> None:
        server = self._server()
        for target in ("/nope", f"/?token={server.token}"):
            self._refused(server, self._request(server, target=target), b"HTTP/1.1 404")

    def test_missing_or_wrong_token_gets_403_and_is_not_counted(self) -> None:
        server = self._server()
        for target in ("/stream.ts", "/stream.ts?token=", "/stream.ts?token=nope"):
            self._refused(server, self._request(server, target=target), b"HTTP/1.1 403")

    def test_each_server_has_its_own_token(self) -> None:
        self.assertNotEqual(self._server().token, self._server().token)

    def test_head_gets_the_header_and_is_not_counted(self) -> None:
        server = self._server()
        viewer = self._connect(server)
        viewer.sendall(self._request(server, "HEAD"))
        self.assertTrue(_read_until(viewer, b"\r\n\r\n").startswith(b"HTTP/1.1 200 OK"))
        time.sleep(0.2)
        self.assertEqual(server.client_count, 0)

    def test_clients_on_fds_above_1024_are_served(self) -> None:
        # select() rejects fds >= 1024, which a busy HA process reaches.
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = 2048 if hard == resource.RLIM_INFINITY else min(hard, 2048)
        if wanted < 1200:
            self.skipTest("RLIMIT_NOFILE too low")
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, wanted), hard))
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        for _ in range(1100):
            self.addCleanup(os.close, os.open(os.devnull, os.O_RDONLY))
        server = self._server()
        self._connect(server).sendall(self._request(server))
        self._connect(server)
        self.assertTrue(_wait(lambda: server.client_count == 2))


if __name__ == "__main__":
    unittest.main()
