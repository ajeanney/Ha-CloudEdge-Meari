"""Offline tests for the HTTP form of the local MPEG-TS stream.

HA hands ``stream_source`` to go2rtc. go2rtc reports no ``url`` for a raw
``tcp://`` producer, and HA's go2rtc client requires one, so every WebRTC
offer fails while that producer runs. The camera therefore advertises
``http://host:port/stream.ts``; raw TCP readers (Frigate) keep working on the
same port.
"""

from __future__ import annotations

import asyncio
import importlib
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
        cam._coordinator = SimpleNamespace(stream_port=41234, stream_host="192.168.1.10")
        source = asyncio.run(cam.stream_source())
        self.assertEqual(source, "http://192.168.1.10:41234/stream.ts")

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

    def test_http_get_gets_a_header_then_the_stream(self) -> None:
        server = self._server()
        viewer = self._connect(server)
        viewer.sendall(b"GET /stream.ts HTTP/1.1\r\nHost: x\r\n\r\n")
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

    def test_other_path_gets_404_and_is_not_counted(self) -> None:
        server = self._server()
        viewer = self._connect(server)
        viewer.sendall(b"GET /nope HTTP/1.1\r\n\r\n")
        self.assertTrue(_read_until(viewer, b"\r\n\r\n").startswith(b"HTTP/1.1 404"))
        time.sleep(0.2)
        self.assertEqual(server.client_count, 0)

    def test_head_gets_the_header_and_is_not_counted(self) -> None:
        server = self._server()
        viewer = self._connect(server)
        viewer.sendall(b"HEAD /stream.ts HTTP/1.1\r\n\r\n")
        self.assertTrue(_read_until(viewer, b"\r\n\r\n").startswith(b"HTTP/1.1 200 OK"))
        time.sleep(0.2)
        self.assertEqual(server.client_count, 0)


if __name__ == "__main__":
    unittest.main()
