import can
import pytest

from can_tcp_client import bytes_to_frame, frame_to_bytes
from can_tcp_gateway import FRAME_SIZE


def test_extended_frame_round_trip() -> None:
    message = can.Message(
        arbitration_id=0x18FEE8F0,
        is_extended_id=True,
        data=[0xFF, 0x01, 0x02, 0x03],
    )

    packet = frame_to_bytes(message)
    restored = bytes_to_frame(packet)

    assert len(packet) == FRAME_SIZE
    assert restored.arbitration_id == message.arbitration_id
    assert restored.is_extended_id is True
    assert bytes(restored.data) == bytes(message.data)


def test_standard_frame_round_trip() -> None:
    message = can.Message(arbitration_id=0x321, is_extended_id=False, data=[1, 2, 3])

    restored = bytes_to_frame(frame_to_bytes(message))

    assert restored.arbitration_id == 0x321
    assert restored.is_extended_id is False
    assert bytes(restored.data) == b"\x01\x02\x03"


def test_invalid_dlc_is_rejected() -> None:
    packet = b"\x00\x00\x00\x01\x09\x00\x00\x00" + bytes(8)

    try:
        bytes_to_frame(packet)
    except ValueError as error:
        assert "DLC" in str(error)
    else:
        raise AssertionError("invalid DLC was accepted")


@pytest.mark.parametrize(
    ("arbitration_id", "data"),
    [
        # J1939 address claim from a control function.
        (0x18EEFF81, bytes.fromhex("1400E0AF008200A0")),
        # Typical machine speed message observed on the ISOBUS.
        (0x18FEE8F0, bytes.fromhex("FFFF0000FFFFFFFF")),
        # Task Controller status message.
        (0x0CCBFFF7, bytes.fromhex("FEFFFFFF01FE00FF")),
        # VT object-pool transport traffic.
        (0x14E726F7, bytes.fromhex("0102030405060708")),
        # Standard 11-bit CAN frame used by some diagnostic tools.
        (0x7DF, bytes.fromhex("02010C0000000000")),
    ],
)
def test_typical_isobus_frames_are_transparent(arbitration_id: int, data: bytes) -> None:
    message = can.Message(
        arbitration_id=arbitration_id,
        is_extended_id=arbitration_id > 0x7FF,
        data=data,
    )

    restored = bytes_to_frame(frame_to_bytes(message))

    assert restored.arbitration_id == arbitration_id
    assert restored.is_extended_id is (arbitration_id > 0x7FF)
    assert bytes(restored.data) == data
