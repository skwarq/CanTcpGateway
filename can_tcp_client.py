#!/usr/bin/env python3
"""Expose a remote CAN gateway as a local SocketCAN interface."""

import argparse
import asyncio
import logging
import struct
import subprocess
import time
from datetime import datetime

import can
from discovery import discover

LOG = logging.getLogger("can-tcp-client")
FRAME_SIZE = 16
CAN_EFF_FLAG = 0x80000000


def frame_to_bytes(message: can.Message) -> bytes:
    can_id = message.arbitration_id | (CAN_EFF_FLAG if message.is_extended_id else 0)
    return struct.pack(">IB3x8s", can_id, len(message.data), bytes(message.data).ljust(8, b"\x00"))


def bytes_to_frame(packet: bytes) -> can.Message:
    can_id, dlc, data = struct.unpack(">IB3x8s", packet)
    if dlc > 8:
        raise ValueError("invalid CAN DLC")
    return can.Message(
        arbitration_id=can_id & 0x1FFFFFFF, is_extended_id=bool(can_id & CAN_EFF_FLAG), data=data[:dlc], check=True
    )


def format_frame(direction: str, message: can.Message) -> str:
    data = bytes(message.data).hex(" ").upper()
    result = (
        f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {direction:<3} "
        f"{'EXT' if message.is_extended_id else 'STD'} "
        f"0x{message.arbitration_id:08X} DLC={len(message.data)} [{data}]"
    )
    if message.is_extended_id:
        can_id = message.arbitration_id
        pf = (can_id >> 16) & 0xFF
        ps = (can_id >> 8) & 0xFF
        pgn = (can_id >> 8) & 0x3FFFF
        source = can_id & 0xFF
        destination = ps if pf < 0xF0 else 0xFF
        if pf < 0xF0:
            pgn &= 0x3FF00
        priority = (can_id >> 26) & 0x7
        target = "global" if destination == 0xFF else f"0x{destination:02X}"
        result += f" J1939(prio={priority}, PGN=0x{pgn:04X}, src=0x{source:02X}, dst={target})"
    return result


def create_vcan(interface: str) -> None:
    subprocess.run(["ip", "link", "add", "dev", interface, "type", "vcan"], check=False)
    subprocess.run(["ip", "link", "set", interface, "up"], check=True)


class Client:
    def __init__(self, bus: can.Bus, dump: bool, dump_ids: set[int] | None, dump_rate: int):
        self.bus = bus
        self.dump = dump
        self.dump_ids = dump_ids
        self.dump_rate = dump_rate
        self.dump_window = time.monotonic()
        self.dump_count = 0
        self.writer: asyncio.StreamWriter | None = None
        self.write_lock = asyncio.Lock()
        self.loop = asyncio.get_running_loop()

    def log_frame(self, direction: str, message: can.Message) -> None:
        if not self.dump or (self.dump_ids and message.arbitration_id not in self.dump_ids):
            return
        now = time.monotonic()
        if now - self.dump_window >= 1.0:
            self.dump_window = now
            self.dump_count = 0
        if self.dump_count >= self.dump_rate:
            return
        self.dump_count += 1
        LOG.info("%s", format_frame(direction, message))

    async def local_to_tcp(self) -> None:
        try:
            while True:
                message = await self.loop.run_in_executor(None, self.bus.recv, 1.0)
                if message is None or self.writer is None:
                    continue
                async with self.write_lock:
                    self.writer.write(frame_to_bytes(message))
                    await self.writer.drain()
                self.log_frame("TX", message)
        except (ConnectionError, asyncio.CancelledError):
            return

    async def tcp_to_local(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                packet = await reader.readexactly(FRAME_SIZE)
                message = bytes_to_frame(packet)
                self.bus.send(message)
                self.log_frame("RX", message)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            return

    async def run(
        self, host: str | None, port: int, reconnect_delay: float, discovery_port: int, discovery_interval: float
    ) -> None:
        use_discovery = host is None
        while True:
            try:
                if use_discovery:
                    found = await discover(discovery_port, min(discovery_interval, 1.0), discovery_interval, LOG)
                    host, port = found.address, found.port
                reader, self.writer = await asyncio.open_connection(host, port)
                LOG.info("Connected to %s:%d", host, port)
                tasks = {
                    asyncio.create_task(self.local_to_tcp()),
                    asyncio.create_task(self.tcp_to_local(reader)),
                }
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*done, return_exceptions=True)
                await asyncio.gather(*pending, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, asyncio.IncompleteReadError, OSError) as exc:
                LOG.info("TCP connection closed: %s; retrying in %.1fs", exc, reconnect_delay)
            finally:
                if self.writer:
                    self.writer.close()
                    try:
                        await self.writer.wait_closed()
                    except OSError:
                        pass
                self.writer = None
                if use_discovery:
                    host = None
            await asyncio.sleep(reconnect_delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?", help="TCP gateway address; omit to use UDP discovery")
    parser.add_argument("--port", type=int, default=29500)
    parser.add_argument("--interface", default="socketcan", help="python-can backend: socketcan, slcan, pcan, ...")
    parser.add_argument("--can", default="vcan1", help="local CAN channel, e.g. vcan1 or COM3")
    parser.add_argument("--bitrate", type=int, default=250000, help="CAN bitrate (default: 250000)")
    parser.add_argument("--create-vcan", action="store_true", help="create and enable local vcan")
    parser.add_argument("--dump", action="store_true", help="print formatted CAN frames")
    parser.add_argument("--dump-filter", help="comma-separated CAN IDs to print, e.g. 0x18FEE8F0,0x0CF022F0")
    parser.add_argument("--dump-rate", type=int, default=200, help="maximum dump lines per second (default: 200)")
    parser.add_argument("--reconnect-delay", type=float, default=2.0, help="TCP reconnect delay in seconds")
    parser.add_argument("--discovery-port", type=int, default=29501, help="UDP discovery port (default: 29501)")
    parser.add_argument("--discovery-interval", type=float, default=3.0, help="seconds between discovery requests")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    if args.create_vcan:
        create_vcan(args.can)
    dump_ids = None
    if args.dump_filter:
        dump_ids = {int(value.strip(), 0) for value in args.dump_filter.split(",")}
    if args.dump_rate < 1:
        raise SystemExit("--dump-rate must be positive")
    bus = can.Bus(interface=args.interface, channel=args.can, bitrate=args.bitrate, receive_own_messages=False)

    async def run_client() -> None:
        client = Client(bus, args.dump, dump_ids, args.dump_rate)
        await client.run(args.host, args.port, args.reconnect_delay, args.discovery_port, args.discovery_interval)

    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()
