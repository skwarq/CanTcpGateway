"""Cross-platform UDP discovery protocol for CanTcpGateway."""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
import struct
from dataclasses import dataclass

import psutil


MAGIC = b"CTG1"
VERSION = 1
DISCOVER = 1
RESPONSE = 2
_HEADER = struct.Struct("!4sBBH")
_DISCOVER = _HEADER.size + 16
_RESPONSE = _HEADER.size + 16 + 4 + 16


@dataclass(frozen=True)
class DiscoveryResponse:
    token: bytes
    address: str
    port: int


def make_discover(token: bytes | None = None) -> tuple[bytes, bytes]:
    token = token or secrets.token_bytes(16)
    if len(token) != 16:
        raise ValueError("discovery token must contain 16 bytes")
    return _HEADER.pack(MAGIC, VERSION, DISCOVER, 0) + token, token


def make_response(token: bytes, address: str, port: int) -> bytes:
    if len(token) != 16 or not 0 < port <= 65535:
        raise ValueError("invalid discovery response")
    parsed = ipaddress.ip_address(address)
    family = socket.AF_INET if parsed.version == 4 else socket.AF_INET6
    packed = parsed.packed
    return (
        _HEADER.pack(MAGIC, VERSION, RESPONSE, 0)
        + token
        + struct.pack("!HBB", port, family, len(packed))
        + packed.ljust(16, b"\0")
    )


def parse_discover(packet: bytes) -> bytes | None:
    if len(packet) != _DISCOVER:
        return None
    magic, version, message_type, _ = _HEADER.unpack_from(packet)
    if (magic, version, message_type) != (MAGIC, VERSION, DISCOVER):
        return None
    return packet[_HEADER.size :]


def parse_response(packet: bytes, token: bytes) -> DiscoveryResponse | None:
    if len(packet) != _RESPONSE:
        return None
    magic, version, message_type, _ = _HEADER.unpack_from(packet)
    if (magic, version, message_type) != (MAGIC, VERSION, RESPONSE):
        return None
    offset = _HEADER.size
    if packet[offset : offset + 16] != token:
        return None
    port, family, address_length = struct.unpack_from("!HBB", packet, offset + 16)
    raw = packet[offset + 20 : offset + 20 + address_length]
    if (family == socket.AF_INET and address_length != 4) or (family == socket.AF_INET6 and address_length != 16):
        return None
    try:
        address = str(ipaddress.ip_address(raw))
    except ValueError:
        return None
    return DiscoveryResponse(token, address, port)


def route_local_address(peer: tuple[str, int], family: int) -> str:
    """Return the local address selected for reaching peer on this host."""
    with socket.socket(family, socket.SOCK_DGRAM) as probe:
        probe.connect(peer)
        return probe.getsockname()[0]


def local_ipv4_broadcasts() -> list[tuple[str, str]]:
    """Return local IPv4 discovery targets, with loopback first."""
    addresses: list[tuple[str, str]] = [("127.0.0.1", "127.0.0.1")]
    seen = set(addresses)
    for interface_addresses in psutil.net_if_addrs().values():
        for address in interface_addresses:
            candidate = (address.address, address.broadcast)
            if address.family == socket.AF_INET and address.broadcast and candidate not in seen:
                addresses.append(candidate)
                seen.add(candidate)
    return addresses


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, tcp_port: int, logger) -> None:
        self.tcp_port = tcp_port
        self.logger = logger
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        token = parse_discover(data)
        if token is None or self.transport is None:
            return
        try:
            family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
            local_address = route_local_address((addr[0], addr[1]), family)
            response = make_response(token, local_address, self.tcp_port)
            response_socket = socket.socket(family, socket.SOCK_DGRAM)
            response_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            response_socket.bind((local_address, 0))
            response_socket.sendto(response, addr)
            response_socket.close()
            self.logger.info("Discovery response to %s: %s:%d", addr, local_address, self.tcp_port)
        except OSError as exc:
            self.logger.warning("Could not answer discovery request from %s: %s", addr, exc)


async def discover(port: int, timeout: float, interval: float, logger) -> DiscoveryResponse:
    loop = asyncio.get_running_loop()
    while True:
        packet, token = make_discover()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                queue.put_nowait(data)

        addresses = local_ipv4_broadcasts()
        transports = []
        for address, broadcast in addresses:
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    Receiver, local_addr=(address, 0), allow_broadcast=True
                )
                transports.append((transport, broadcast))
            except OSError as exc:
                logger.debug("Could not open discovery socket on %s: %s", address, exc)
        if not transports:
            transport, _ = await loop.create_datagram_endpoint(
                Receiver, local_addr=("0.0.0.0", 0), allow_broadcast=True
            )
            transports.append((transport, "255.255.255.255"))
        try:
            for transport, broadcast in transports:
                transport.sendto(packet, (broadcast, port))
            logger.info("Discovery request sent on %d IPv4 interface(s), UDP port %d", len(transports), port)
            end = loop.time() + timeout
            while loop.time() < end:
                remaining = end - loop.time()
                try:
                    data = await asyncio.wait_for(queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                response = parse_response(data, token)
                if response:
                    logger.info("Gateway discovered at %s:%d", response.address, response.port)
                    return response
        finally:
            for transport, _ in transports:
                transport.close()
        await asyncio.sleep(interval)
