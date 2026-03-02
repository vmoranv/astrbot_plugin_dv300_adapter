import asyncio
import struct
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class UdpFrame:
    data: bytes
    addr: Tuple[str, int]


class _QueueDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        self.queue.put_nowait(UdpFrame(data=data, addr=addr))


class Dv300Protocol:
    MAGIC = 0x41424F54
    VERSION = 1

    MSG_HELLO = 1
    MSG_HEARTBEAT = 2
    MSG_COMMAND = 3
    MSG_ACK = 4
    MSG_ERROR = 5
    MSG_CAPABILITY = 6
    MSG_CAMERA_FRAME = 10
    MSG_AUDIO_FRAME = 11

    CMD_NONE = 0
    CMD_PING = 1
    CMD_START_CAMERA = 2
    CMD_STOP_CAMERA = 3
    CMD_START_MIC = 4
    CMD_STOP_MIC = 5
    CMD_QUERY_CAPABILITY = 6

    HEADER_FMT = "<IHHIII"
    HEADER_SIZE = struct.calcsize(HEADER_FMT)

    CAMERA_META_FMT = "<HHII"
    CAMERA_META_SIZE = struct.calcsize(CAMERA_META_FMT)

    AUDIO_META_FMT = "<IHHI"
    AUDIO_META_SIZE = struct.calcsize(AUDIO_META_FMT)

    CAP_FMT = "<BBHHHHIHH24s24s"
    CAP_SIZE = struct.calcsize(CAP_FMT)

    _seq = 1

    @classmethod
    def _next_seq(cls) -> int:
        cur = cls._seq
        cls._seq = (cls._seq + 1) & 0xFFFFFFFF
        if cls._seq == 0:
            cls._seq = 1
        return cur

    @classmethod
    def pack_packet(cls, msg_type: int, payload: bytes = b"") -> bytes:
        header = struct.pack(
            cls.HEADER_FMT,
            cls.MAGIC,
            cls.VERSION,
            msg_type,
            cls._next_seq(),
            int(time.time() * 1000) & 0xFFFFFFFF,
            len(payload),
        )
        return header + payload

    @classmethod
    def pack_command_payload(cls, command: int, argument: str = "") -> bytes:
        arg = (argument or "").encode("utf-8", errors="ignore")[:63]
        arg_buf = arg + b"\x00" * (64 - len(arg))
        return struct.pack("<I64s", command, arg_buf)

    @classmethod
    def unpack_packet(cls, data: bytes) -> Optional[Dict[str, Any]]:
        if len(data) < cls.HEADER_SIZE:
            return None
        magic, version, msg_type, seq, ts_ms, payload_size = struct.unpack(
            cls.HEADER_FMT, data[: cls.HEADER_SIZE]
        )
        if magic != cls.MAGIC or version != cls.VERSION:
            return None
        if len(data) < cls.HEADER_SIZE + payload_size:
            return None
        payload = data[cls.HEADER_SIZE : cls.HEADER_SIZE + payload_size]
        return {
            "type": msg_type,
            "seq": seq,
            "timestamp_ms": ts_ms,
            "payload_size": payload_size,
            "payload": payload,
        }

    @classmethod
    def parse_capability(cls, payload: bytes) -> Optional[Dict[str, Any]]:
        if len(payload) < cls.CAP_SIZE:
            return None
        (
            camera_supported,
            microphone_supported,
            camera_max_width,
            camera_max_height,
            camera_max_fps,
            _reserved,
            microphone_max_sample_rate,
            microphone_max_channels,
            microphone_max_bits,
            camera_backend,
            microphone_backend,
        ) = struct.unpack(cls.CAP_FMT, payload[: cls.CAP_SIZE])

        return {
            "camera_supported": bool(camera_supported),
            "microphone_supported": bool(microphone_supported),
            "camera_max_width": camera_max_width,
            "camera_max_height": camera_max_height,
            "camera_max_fps": camera_max_fps,
            "microphone_max_sample_rate": microphone_max_sample_rate,
            "microphone_max_channels": microphone_max_channels,
            "microphone_max_bits": microphone_max_bits,
            "camera_backend": camera_backend.split(b"\x00", 1)[0].decode(
                "utf-8", errors="ignore"
            ),
            "microphone_backend": microphone_backend.split(b"\x00", 1)[0].decode(
                "utf-8", errors="ignore"
            ),
        }

    @classmethod
    def parse_camera_meta(cls, payload: bytes) -> Optional[Dict[str, Any]]:
        if len(payload) < cls.CAMERA_META_SIZE:
            return None
        width, height, fmt, frame_len = struct.unpack(
            cls.CAMERA_META_FMT, payload[: cls.CAMERA_META_SIZE]
        )
        return {
            "width": width,
            "height": height,
            "format": fmt,
            "frame_len": frame_len,
        }

    @classmethod
    def parse_audio_meta(cls, payload: bytes) -> Optional[Dict[str, Any]]:
        if len(payload) < cls.AUDIO_META_SIZE:
            return None
        sample_rate, channels, bits_per_sample, frame_len = struct.unpack(
            cls.AUDIO_META_FMT, payload[: cls.AUDIO_META_SIZE]
        )
        return {
            "sample_rate": sample_rate,
            "channels": channels,
            "bits_per_sample": bits_per_sample,
            "frame_len": frame_len,
        }


class Dv300UdpClient:
    def __init__(self, bind_ip: str, bind_port: int):
        self.bind_ip = bind_ip
        self.bind_port = bind_port
        self.queue: asyncio.Queue = asyncio.Queue()
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional[_QueueDatagramProtocol] = None

    async def start(self):
        loop = asyncio.get_running_loop()
        self.transport, self.protocol = await loop.create_datagram_endpoint(
            lambda: _QueueDatagramProtocol(self.queue),
            local_addr=(self.bind_ip, self.bind_port),
        )

    async def recv(self) -> UdpFrame:
        return await self.queue.get()

    def sendto(self, data: bytes, addr: Tuple[str, int]):
        if self.transport is None:
            raise RuntimeError("UDP transport is not ready")
        self.transport.sendto(data, addr)

    async def close(self):
        if self.transport is not None:
            self.transport.close()
            self.transport = None
