"""Zero-dependency asyncio HTTP CONNECT and HTTP forward proxy with exact hostname allowlist.

Enforces:
- Exact hostname matching against an allowlist (case-insensitive).
- Rejection of raw IP literals (e.g. 192.168.1.1, 10.0.0.1, etc.).
- Port restrictions (CONNECT must target port 443).
- Structured JSONL audit logging of every request attempt.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import ipaddress
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("egress_proxy")


def is_ip_literal(host: str) -> bool:
    """Check if host is an IPv4 or IPv6 address literal."""
    cleaned = host.strip("[]")
    try:
        ipaddress.ip_address(cleaned)
        return True
    except ValueError:
        return False


class EgressProxyServer:
    def __init__(
        self,
        allowlist: set[str],
        log_path: Path | None = None,
        bind_host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.allowlist = {h.lower().strip() for h in allowlist if h.strip()}
        self.log_path = log_path
        self.bind_host = bind_host
        self.port = port
        self.server: asyncio.Server | None = None
        self._log_lock = asyncio.Lock()

    async def log_request(
        self,
        client: str,
        method: str,
        host: str,
        port: int,
        allowed: bool,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.datetime.now(datetime.UTC).isoformat(),
            "client": client,
            "method": method,
            "host": host,
            "port": port,
            "allowed": allowed,
        }
        line = json.dumps(entry) + "\n"
        if self.log_path:
            async with self._log_lock:
                with open(self.log_path, "a") as f:
                    f.write(line)
        logger.info("Request: %s %s:%d allowed=%s", method, host, port, allowed)

    def is_allowed(self, host: str, port: int) -> bool:
        if port != 443:
            return False
        if is_ip_literal(host):
            return False
        return host.lower().strip() in self.allowlist

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        client_addr = f"{peer[0]}:{peer[1]}" if peer else "unknown"

        try:
            line = await reader.readline()
            if not line:
                writer.close()
                await writer.wait_closed()
                return

            req_line = line.decode("latin1").strip()
            parts = req_line.split()
            if len(parts) < 3:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            method, target, _ = parts[0].upper(), parts[1], parts[2]

            headers: dict[str, str] = {}
            while True:
                hdr_line = await reader.readline()
                if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                    break
                decoded_hdr = hdr_line.decode("latin1").strip()
                if ":" in decoded_hdr:
                    k, v = decoded_hdr.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if method == "CONNECT":
                if ":" in target:
                    host, port_str = target.rsplit(":", 1)
                    try:
                        port = int(port_str)
                    except ValueError:
                        port = 0
                else:
                    host = target
                    port = 443

                allowed = self.is_allowed(host, port)
                await self.log_request(client_addr, method, host, port, allowed)

                if not allowed:
                    writer.write(
                        b"HTTP/1.1 403 Forbidden\r\n"
                        b"Content-Type: text/plain\r\n"
                        b"Connection: close\r\n\r\n"
                        b"Forbidden by Egress Allowlist\r\n"
                    )
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    return

                try:
                    remote_reader, remote_writer = await asyncio.open_connection(host, port)
                except Exception as e:
                    logger.warning("Failed to connect to %s:%d: %s", host, port, e)
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    return

                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
                    try:
                        while not src.at_eof():
                            data = await src.read(65536)
                            if not data:
                                break
                            dst.write(data)
                            await dst.drain()
                    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                        pass
                    finally:
                        try:
                            dst.close()
                            await dst.wait_closed()
                        except Exception:
                            pass

                await asyncio.gather(
                    pipe(reader, remote_writer),
                    pipe(remote_reader, writer),
                    return_exceptions=True,
                )
            else:
                host_hdr = headers.get("host", target)
                if ":" in host_hdr:
                    host, port_str = host_hdr.rsplit(":", 1)
                    port = int(port_str) if port_str.isdigit() else 80
                else:
                    host = host_hdr
                    port = 80

                allowed = self.is_allowed(host, port)
                await self.log_request(client_addr, method, host, port, allowed)
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Connection: close\r\n\r\n"
                    b"Plain HTTP or unauthorized host rejected\r\n"
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

        except Exception as e:
            logger.exception("Error handling client %s: %s", client_addr, e)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self) -> asyncio.Server:
        self.server = await asyncio.start_server(self.handle_client, self.bind_host, self.port)
        return self.server

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SubscriptionBench Egress Allowlist Proxy")
    parser.add_argument("--port", type=int, default=8080, help="Listen port")
    parser.add_argument("--host", default="0.0.0.0", help="Listen host")
    parser.add_argument("--allowlist", help="Comma-separated allowed hostnames")
    parser.add_argument("--allowlist-file", help="Path to newline-separated allowed hostnames")
    parser.add_argument("--log", help="Path to proxy.log output")
    return parser.parse_args()


def load_allowlist(args: argparse.Namespace) -> set[str]:
    hosts: set[str] = set()
    if args.allowlist:
        hosts.update(args.allowlist.split(","))
    if args.allowlist_file:
        path = Path(args.allowlist_file)
        if path.exists():
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    hosts.add(stripped)
    return hosts


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = parse_args()
    allowlist = load_allowlist(args)
    log_path = Path(args.log) if args.log else None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)

    proxy = EgressProxyServer(
        allowlist=allowlist, log_path=log_path, bind_host=args.host, port=args.port
    )
    server = await proxy.start()
    logger.info(
        "Egress proxy running on %s:%d, allowlist: %s", args.host, args.port, sorted(allowlist)
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
