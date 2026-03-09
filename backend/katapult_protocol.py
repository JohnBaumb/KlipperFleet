"""
Katapult bootloader protocol helpers.

Implements the wire protocol used by Katapult (formerly CanBoot) for
communicating with STM32 bootloaders over CAN and serial transports.
Protocol format matches flashtool.py exactly.
"""

import struct
import socket
import time
from typing import Optional, Tuple

# -- Wire framing --
CMD_HEADER = b'\x01\x88'
CMD_TRAILER = b'\x99\x03'

# -- Bootloader commands --
CONNECT = 0x11
SEND_BLOCK = 0x12
SEND_EOF = 0x13
REQUEST_BLOCK = 0x14
COMPLETE = 0x15
GET_CANBUS_ID = 0x16

# -- Response codes --
ACK_SUCCESS = 0xa0
NACK = 0xf1
ACK_ERROR = 0xf2
ACK_BUSY = 0xf3

# -- CAN admin --
CANBUS_ID_ADMIN = 0x3f0
CANBUS_CMD_SET_NODEID = 0x11
CANBUS_NODEID_OFFSET = 128
CAN_FMT = "<IB3x8s"


def crc16_ccitt(buf: bytes) -> int:
    crc = 0xFFFF
    for data in buf:
        data ^= crc & 0xFF
        data ^= (data & 0x0F) << 4
        crc = ((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)
    return crc & 0xFFFF


def build_command(cmd: int, payload: bytes = b"") -> bytearray:
    """Build a framed Katapult command packet."""
    word_cnt = (len(payload) // 4) & 0xFF
    out_cmd = bytearray(CMD_HEADER)
    out_cmd.append(cmd)
    out_cmd.append(word_cnt)
    if payload:
        out_cmd.extend(payload)
    crc = crc16_ccitt(out_cmd[2:])
    out_cmd.extend(struct.pack("<H", crc))
    out_cmd.extend(CMD_TRAILER)
    return out_cmd


def send_can_frame(interface: str, can_id: int, data: bytes) -> None:
    """Send a single CAN frame."""
    with socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as s:
        s.bind((interface,))
        can_pkt = struct.pack(CAN_FMT, can_id, len(data), data.ljust(8, b'\x00'))
        s.send(can_pkt)


def restart_firmware_can(interface: str, uuid_hex: str) -> str:
    """
    Send COMPLETE to a CAN device in Katapult mode to jump to application.

    Assigns a temporary node ID, then sends the COMPLETE command.
    Returns a status message.
    """
    uuid_bytes = bytes.fromhex(uuid_hex)

    # Assign temporary node ID (matches flashtool _set_node_id)
    set_id_payload = bytes([CANBUS_CMD_SET_NODEID]) + uuid_bytes + bytes([CANBUS_NODEID_OFFSET])
    send_can_frame(interface, CANBUS_ID_ADMIN, set_id_payload)
    time.sleep(0.1)

    # Send COMPLETE command to the assigned node
    # node_id = CANBUS_NODEID_OFFSET, decoded = node_id * 2 + 0x100 = 0x200
    complete_pkt = build_command(COMPLETE)
    send_can_frame(interface, 0x200, complete_pkt)

    return f"Jump command sent to CAN UUID {uuid_hex}"


def restart_firmware_serial(device_path: str, baud: int = 250000,
                            write_timeout: float = 3.0) -> str:
    """
    Send CONNECT + COMPLETE to a serial Katapult device to jump to application.

    Opens the serial port, sends CONNECT (waits for response), then sends
    COMPLETE. Returns a status message.
    """
    import serial as pyserial

    ser = pyserial.Serial(device_path, baud, timeout=2, write_timeout=write_timeout)
    try:
        time.sleep(0.1)

        # Send CONNECT and wait for bootloader response
        connect_cmd = build_command(CONNECT)
        ser.write(connect_cmd)
        time.sleep(0.5)
        # Read and discard CONNECT response
        ser.read(ser.in_waiting or 64)

        # Send COMPLETE to jump to application
        complete_cmd = build_command(COMPLETE)
        ser.write(complete_cmd)
        time.sleep(0.3)
    finally:
        ser.close()

    return "COMPLETE command sent. Device should jump to application."
