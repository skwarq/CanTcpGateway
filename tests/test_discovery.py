import asyncio
import socket

import pytest
from unittest.mock import patch

from discovery import (
    DiscoveryProtocol,
    make_discover,
    make_response,
    parse_discover,
    parse_response,
    route_local_address,
    local_ipv4_broadcasts,
)


def test_discover_round_trip() -> None:
    packet, token = make_discover()

    assert parse_discover(packet) == token
    assert parse_discover(packet[:-1]) is None
    assert parse_discover(b"invalid") is None


@pytest.mark.parametrize("address", ["192.168.10.20", "2001:db8::20"])
def test_response_round_trip(address: str) -> None:
    token = bytes(range(16))
    packet = make_response(token, address, 29500)

    response = parse_response(packet, token)

    assert response is not None
    assert response.token == token
    assert response.address == address
    assert response.port == 29500


def test_response_with_wrong_token_is_ignored() -> None:
    token = bytes(range(16))
    packet = make_response(token, "127.0.0.1", 29500)

    assert parse_response(packet, b"x" * 16) is None


def test_response_with_invalid_packet_is_ignored() -> None:
    token = bytes(range(16))
    packet = bytearray(make_response(token, "127.0.0.1", 29500))
    packet[0:4] = b"BAD!"

    assert parse_response(bytes(packet), token) is None


def test_response_rejects_mismatched_address_family() -> None:
    token = bytes(range(16))
    packet = bytearray(make_response(token, "127.0.0.1", 29500))
    packet[26] = socket.AF_INET6

    assert parse_response(bytes(packet), token) is None


def test_local_ipv4_broadcasts_are_deduplicated() -> None:
    class Address:
        def __init__(self, family: int, address: str) -> None:
            self.family = family
            self.address = address
            self.broadcast = None

    fake_interfaces = {
        "eth0": [Address(socket.AF_INET, "192.168.1.10"), Address(socket.AF_INET6, "fe80::1")],
        "eth1": [Address(socket.AF_INET, "192.168.1.10"), Address(socket.AF_INET, "10.0.0.10")],
    }
    with patch("discovery.psutil.net_if_addrs", return_value=fake_interfaces):
        fake_interfaces["eth0"][0].broadcast = "192.168.1.255"
        fake_interfaces["eth1"][0].broadcast = "192.168.1.255"
        fake_interfaces["eth1"][1].broadcast = "10.0.0.255"
        assert local_ipv4_broadcasts() == [
            ("127.0.0.1", "127.0.0.1"),
            ("192.168.1.10", "192.168.1.255"),
            ("10.0.0.10", "10.0.0.255"),
        ]


def test_local_ipv4_broadcasts_always_start_with_loopback() -> None:
    with patch("discovery.psutil.net_if_addrs", return_value={}):
        assert local_ipv4_broadcasts() == [("127.0.0.1", "127.0.0.1")]


def test_route_selects_an_ipv4_local_address() -> None:
    """Verify interface selection using the OS routing table.

    This does not create interfaces, so it is safe on developer machines and
    CI runners.  The loopback route is available on all supported platforms.
    """
    local_address = route_local_address(("127.0.0.1", 29500), socket.AF_INET)

    assert local_address == "127.0.0.1"


def test_discovery_protocol_answers_with_local_route_address() -> None:
    asyncio.run(_test_discovery_protocol_answers_with_local_route_address())


async def _test_discovery_protocol_answers_with_local_route_address() -> None:
    loop = asyncio.get_running_loop()
    received: asyncio.Future[bytes] = loop.create_future()

    class ClientProtocol(asyncio.DatagramProtocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            packet, _ = make_discover(bytes(range(16)))
            transport.sendto(packet, ("127.0.0.1", server_port))

        def datagram_received(self, data: bytes, addr) -> None:
            if not received.done():
                received.set_result(data)

    server_transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(29500, _TestLogger()), local_addr=("127.0.0.1", 0)
    )
    server_port = server_transport.get_extra_info("sockname")[1]
    client_transport, _ = await loop.create_datagram_endpoint(ClientProtocol, local_addr=("127.0.0.1", 0))
    try:
        packet = await asyncio.wait_for(received, timeout=1.0)
        response = parse_response(packet, bytes(range(16)))
        assert response is not None
        assert response.address == "127.0.0.1"
        assert response.port == 29500
    finally:
        client_transport.close()
        server_transport.close()


class _TestLogger:
    def info(self, *args) -> None:
        pass

    def warning(self, *args) -> None:
        pass
