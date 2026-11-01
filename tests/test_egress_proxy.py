import asyncio
import json
from pathlib import Path

from subbench.provision.egress_proxy import EgressProxyServer, is_ip_literal


def test_is_ip_literal():
    assert is_ip_literal("127.0.0.1")
    assert is_ip_literal("192.168.1.1")
    assert is_ip_literal("::1")
    assert is_ip_literal("[::1]")
    assert is_ip_literal("2001:db8::1")
    assert not is_ip_literal("example.com")
    assert not is_ip_literal("daily-cloudcode-pa.googleapis.com")
    assert not is_ip_literal("localhost")


def test_is_allowed():
    proxy = EgressProxyServer(
        allowlist={"daily-cloudcode-pa.googleapis.com", "accounts.google.com"}
    )
    assert proxy.is_allowed("daily-cloudcode-pa.googleapis.com", 443)
    assert proxy.is_allowed("DAILY-CLOUDCODE-PA.GOOGLEAPIS.COM", 443)
    assert not proxy.is_allowed("daily-cloudcode-pa.googleapis.com", 80)
    assert not proxy.is_allowed("127.0.0.1", 443)
    assert not proxy.is_allowed("github.com", 443)


def test_proxy_blocks_disallowed_hosts_and_ip_literals(tmp_path: Path):
    async def _test():
        log_file = tmp_path / "proxy.log"
        proxy = EgressProxyServer(
            allowlist={"allowed.example.com"},
            log_path=log_file,
            bind_host="127.0.0.1",
            port=0,
        )
        server = await proxy.start()
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]

        try:
            # Test 1: Disallowed domain
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n")
            await writer.drain()
            resp = await reader.read(1024)
            assert b"403 Forbidden" in resp
            writer.close()
            await writer.wait_closed()

            # Test 2: IP literal
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT 192.168.1.100:443 HTTP/1.1\r\nHost: 192.168.1.100:443\r\n\r\n")
            await writer.drain()
            resp = await reader.read(1024)
            assert b"403 Forbidden" in resp
            writer.close()
            await writer.wait_closed()

            # Test 3: Plain HTTP request rejected
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: allowed.example.com\r\n\r\n")
            await writer.drain()
            resp = await reader.read(1024)
            assert b"403 Forbidden" in resp
            writer.close()
            await writer.wait_closed()

            # Verify JSONL log entries
            lines = [json.loads(line) for line in log_file.read_text().splitlines()]
            assert len(lines) == 3
            assert lines[0]["host"] == "github.com"
            assert lines[0]["allowed"] is False
            assert lines[1]["host"] == "192.168.1.100"
            assert lines[1]["allowed"] is False
            assert lines[2]["allowed"] is False
        finally:
            await proxy.stop()

    asyncio.run(_test())


def test_proxy_tunnels_allowed_host(tmp_path: Path):
    async def _test():
        # Spin up a mock upstream HTTPS server
        received_upstream = []

        async def handle_upstream(r, w):
            data = await r.read(100)
            received_upstream.append(data)
            w.write(b"PONG-FROM-UPSTREAM")
            await w.drain()
            w.close()
            await w.wait_closed()

        upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
        assert upstream.sockets is not None
        upstream_port = upstream.sockets[0].getsockname()[1]

        class TestProxyServer(EgressProxyServer):
            def is_allowed(self, host: str, port: int) -> bool:
                return host == "127.0.0.1" and port == upstream_port

        log_file = tmp_path / "proxy.log"
        proxy = TestProxyServer(
            allowlist={"127.0.0.1"},
            log_path=log_file,
            bind_host="127.0.0.1",
            port=0,
        )
        server = await proxy.start()
        assert server.sockets is not None
        proxy_port = server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            connect_req = f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n\r\n".encode()
            writer.write(connect_req)
            await writer.drain()

            resp = await reader.readline()
            assert b"200 Connection Established" in resp
            await reader.readline()  # empty line

            # Send data through the tunnel
            writer.write(b"PING-THROUGH-TUNNEL")
            await writer.drain()

            # Read response from upstream through the tunnel
            tunnel_resp = await reader.read(100)
            assert tunnel_resp == b"PONG-FROM-UPSTREAM"
            assert received_upstream == [b"PING-THROUGH-TUNNEL"]

            writer.close()
            await writer.wait_closed()

            lines = [json.loads(line) for line in log_file.read_text().splitlines()]
            assert len(lines) == 1
            assert lines[0]["allowed"] is True
        finally:
            await proxy.stop()
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(_test())
