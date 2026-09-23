#!/usr/bin/env python3
"""Bridge SocketCAN frames to TCP clients and TCP frames back to SocketCAN."""

import argparse
import asyncio
import logging
import struct
import sys

import can
from discovery import DiscoveryProtocol


LOG = logging.getLogger("can-tcp-gateway")


FRAME_SIZE = 16
CAN_EFF_FLAG = 0x80000000


def frame_to_bytes(message: can.Message) -> bytes:
    """Encode one Linux SocketCAN-like can_frame in network byte order."""
    data = bytes(message.data)
    can_id = message.arbitration_id | (CAN_EFF_FLAG if message.is_extended_id else 0)
    return struct.pack(
        ">IB3x8s",
        can_id,
        len(data),
        data.ljust(8, b"\x00"),
    )


def bytes_to_frame(packet: bytes) -> can.Message:
    if len(packet) != FRAME_SIZE:
        raise ValueError("invalid TCP frame size")
    can_id, dlc, data = struct.unpack(">IB3x8s", packet)
    is_extended_id = bool(can_id & CAN_EFF_FLAG)
    arbitration_id = can_id & 0x1FFFFFFF
    if not 0 <= arbitration_id <= 0x1FFFFFFF:
        raise ValueError("CAN id out of range")
    if dlc > 8:
        raise ValueError("invalid CAN DLC")
    return can.Message(
        arbitration_id=arbitration_id,
        is_extended_id=is_extended_id,
        data=data[:dlc],
        check=True,
    )


class Gateway:
    def __init__(self, bus: can.Bus, loop: asyncio.AbstractEventLoop):
        self.bus = bus
        self.loop = loop
        self.clients: dict[asyncio.StreamWriter, asyncio.Queue[bytes]] = {}
        self.sender_tasks: dict[asyncio.StreamWriter, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()

    async def broadcast(self, message: can.Message) -> None:
        packet = frame_to_bytes(message)
        async with self.lock:
            clients = list(self.clients.items())
        for writer, queue in clients:
            try:
                queue.put_nowait(packet)
            except asyncio.QueueFull:
                LOG.warning("Disconnecting slow TCP client: %s", writer.get_extra_info("peername"))
                await self.remove_client(writer)

    async def add_client(self, writer: asyncio.StreamWriter) -> None:
        async with self.lock:
            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
            self.clients[writer] = queue
            self.sender_tasks[writer] = asyncio.create_task(self.sender_loop(writer, queue))
        peer = writer.get_extra_info("peername")
        LOG.info("TCP client connected: %s", peer)

    async def remove_client(self, writer: asyncio.StreamWriter) -> None:
        async with self.lock:
            self.clients.pop(writer, None)
            sender = self.sender_tasks.pop(writer, None)
        if sender and sender is not asyncio.current_task():
            sender.cancel()
        if not writer.is_closing():
            writer.close()
            await writer.wait_closed()
        LOG.info("TCP client disconnected")

    async def sender_loop(self, writer: asyncio.StreamWriter, queue: asyncio.Queue[bytes]) -> None:
        try:
            while True:
                writer.write(await queue.get())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def client_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self.add_client(writer)
        try:
            while packet := await reader.readexactly(FRAME_SIZE):
                try:
                    message = bytes_to_frame(packet)
                    self.bus.send(message)
                except (ValueError, TypeError, can.CanError) as exc:
                    LOG.warning("Ignoring invalid TCP frame: %s", exc)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await self.remove_client(writer)

    async def can_reader(self) -> None:
        while True:
            message = await self.loop.run_in_executor(None, self.bus.recv, 1.0)
            if message is not None:
                await self.broadcast(message)


async def main(interface: str, channel: str, bitrate: int, host: str, port: int, discovery_port: int) -> None:
    loop = asyncio.get_running_loop()
    bus = can.Bus(interface=interface, channel=channel, bitrate=bitrate, receive_own_messages=False)
    gateway = Gateway(bus, loop)
    server = await asyncio.start_server(gateway.client_loop, host, port)
    discovery_transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(port, LOG), local_addr=("0.0.0.0", discovery_port)
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    LOG.info("Listening on %s; forwarding SocketCAN %s", addresses, interface)
    try:
        async with server:
            await asyncio.gather(server.serve_forever(), gateway.can_reader())
    finally:
        discovery_transport.close()
        bus.shutdown()


def parse_args() -> argparse.Namespace:
    windows = sys.platform == "win32"
    default_interface = "pcan" if windows else "socketcan"
    default_channel = "PCAN_USBBUS1" if windows else "can0"
    epilog = (
        "Windows PCAN example: CanTcpGateway.exe --interface pcan --can PCAN_USBBUS1 --bitrate 250000\n"
        "Available PCAN channels commonly include PCAN_USBBUS1, PCAN_USBBUS2, PCAN_USBBUS3."
        if windows
        else "Linux example: can_tcp_gateway.py --interface socketcan --can can0 --bitrate 250000"
    )
    parser = argparse.ArgumentParser(
        description=__doc__, epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--interface",
        default=default_interface,
        help=f"python-can backend (default: {default_interface}): socketcan, slcan, pcan, ...",
    )
    parser.add_argument(
        "--can", default=default_channel, help=f"CAN channel (default: {default_channel}), e.g. can0 or COM3"
    )
    parser.add_argument("--bitrate", type=int, default=250000, help="CAN bitrate (default: 250000)")
    parser.add_argument("--host", default="0.0.0.0", help="TCP bind address")
    parser.add_argument("--port", type=int, default=29500, help="TCP port (default: 29500)")
    parser.add_argument("--discovery-port", type=int, default=29501, help="UDP discovery port (default: 29501)")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    if len(sys.argv) == 1:
        parser.print_help()
        raise SystemExit(0)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main(args.interface, args.can, args.bitrate, args.host, args.port, args.discovery_port))
    except KeyboardInterrupt:
        pass
