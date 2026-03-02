import asyncio
from typing import Any, Dict, Optional, Tuple

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

from .client import Dv300Protocol, Dv300UdpClient
from .dv300_platform_event import Dv300PlatformEvent


@register_platform_adapter(
    "dv300",
    "OHOS DV300 UDP 适配器",
    default_config_tmpl={
        "local_bind_ip": "0.0.0.0",
        "local_bind_port": 19091,
        "device_ip": "127.0.0.1",
        "device_port": 19090,
        "self_id": "dv300_bot",
        "emit_media_events": False,
        "startup_query_capability": True,
    },
    adapter_display_name="DV300 Adapter",
)
class Dv300PlatformAdapter(Platform):
    def __init__(
        self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue
    ):
        try:
            super().__init__(platform_config, event_queue)
        except TypeError:
            super().__init__(event_queue)
            self.config = platform_config

        self.config = platform_config or {}
        self.settings = platform_settings or {}

        self.local_bind_ip = self.config.get("local_bind_ip", "0.0.0.0")
        self.local_bind_port = int(self.config.get("local_bind_port", 19091))
        self.default_device_ip = self.config.get("device_ip", "127.0.0.1")
        self.default_device_port = int(self.config.get("device_port", 19090))
        self.self_id = self.config.get("self_id", "dv300_bot")
        self.emit_media_events = bool(self.config.get("emit_media_events", False))
        self.startup_query_capability = bool(
            self.config.get("startup_query_capability", True)
        )

        self.client: Optional[Dv300UdpClient] = None
        self._device_addr: Optional[Tuple[str, int]] = None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="dv300",
            description="OHOS DV300 UDP platform adapter",
            id="dv300",
            default_config_tmpl=self.config,
            adapter_display_name="DV300 Adapter",
        )

    async def run(self):
        self.client = Dv300UdpClient(self.local_bind_ip, self.local_bind_port)
        await self.client.start()

        logger.info(
            "[dv300] adapter running on %s:%s, default peer=%s:%s",
            self.local_bind_ip,
            self.local_bind_port,
            self.default_device_ip,
            self.default_device_port,
        )

        if self.startup_query_capability:
            await self.send_command(
                Dv300Protocol.CMD_QUERY_CAPABILITY,
                addr=(self.default_device_ip, self.default_device_port),
            )

        while True:
            frame = await self.client.recv()
            packet = Dv300Protocol.unpack_packet(frame.data)
            if not packet:
                continue

            self._device_addr = frame.addr
            abm = await self.convert_message(packet, frame.addr)
            if abm is None:
                continue
            await self.handle_msg(abm)

    async def terminate(self):
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def send_by_session(self, session, message_chain: MessageChain):
        await super().send_by_session(session, message_chain)
        session_id = getattr(session, "session_id", None) or getattr(
            session, "id", None
        )
        text = self._extract_plain_text(message_chain)
        if text:
            await self.send_text_command(session_id, text)

    async def convert_message(
        self, packet: Dict[str, Any], addr: Tuple[str, int]
    ) -> Optional[AstrBotMessage]:
        ptype = packet["type"]
        payload = packet["payload"]

        if (
            ptype in (Dv300Protocol.MSG_CAMERA_FRAME, Dv300Protocol.MSG_AUDIO_FRAME)
            and not self.emit_media_events
        ):
            return None

        summary = self._packet_to_text(ptype, payload)
        if not summary:
            return None

        session_id = f"{addr[0]}:{addr[1]}"

        abm = AstrBotMessage()
        abm.type = MessageType.FRIEND_MESSAGE
        abm.self_id = self.self_id
        abm.session_id = session_id
        abm.message_id = f"{packet['seq']}-{packet['timestamp_ms']}"
        abm.sender = MessageMember(user_id=session_id, nickname=f"dv300@{session_id}")
        abm.message = [Plain(text=summary)]
        abm.message_str = summary
        abm.raw_message = {
            "type": ptype,
            "seq": packet["seq"],
            "timestamp_ms": packet["timestamp_ms"],
            "payload_size": packet["payload_size"],
            "from": {"ip": addr[0], "port": addr[1]},
        }
        abm.timestamp = packet["timestamp_ms"]
        return abm

    async def handle_msg(self, message: AstrBotMessage):
        event = Dv300PlatformEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            adapter=self,
        )
        self.commit_event(event)

    async def send_text_command(self, session_id: Optional[str], text: str):
        text = (text or "").strip()
        if not text:
            return

        addr = self._resolve_addr(session_id)
        cmd = self._map_text_to_command(text)
        if cmd is not None:
            await self.send_command(cmd, addr=addr)
            return

        # Fallback to ASCII command for compatibility.
        await self.send_ascii(text.upper(), addr=addr)

    async def send_command(
        self, command: int, argument: str = "", addr: Optional[Tuple[str, int]] = None
    ):
        payload = Dv300Protocol.pack_command_payload(command, argument)
        packet = Dv300Protocol.pack_packet(Dv300Protocol.MSG_COMMAND, payload)
        target = addr or self._resolve_addr(None)
        self.client.sendto(packet, target)

    async def send_ascii(self, text: str, addr: Optional[Tuple[str, int]] = None):
        packet = text.encode("utf-8", errors="ignore")
        target = addr or self._resolve_addr(None)
        self.client.sendto(packet, target)

    def _resolve_addr(self, session_id: Optional[str]) -> Tuple[str, int]:
        if session_id and ":" in session_id:
            host, port = session_id.rsplit(":", 1)
            try:
                return host, int(port)
            except ValueError:
                pass

        if self._device_addr is not None:
            return self._device_addr
        return self.default_device_ip, self.default_device_port

    @staticmethod
    def _extract_plain_text(message_chain: MessageChain) -> str:
        texts = []
        for comp in message_chain.chain:
            if isinstance(comp, Plain):
                if comp.text:
                    texts.append(comp.text)
        return "\n".join(texts).strip()

    @staticmethod
    def _map_text_to_command(text: str) -> Optional[int]:
        normalized = text.strip().lower()
        mapping = {
            "start_camera": Dv300Protocol.CMD_START_CAMERA,
            "start camera": Dv300Protocol.CMD_START_CAMERA,
            "开启摄像头": Dv300Protocol.CMD_START_CAMERA,
            "stop_camera": Dv300Protocol.CMD_STOP_CAMERA,
            "stop camera": Dv300Protocol.CMD_STOP_CAMERA,
            "关闭摄像头": Dv300Protocol.CMD_STOP_CAMERA,
            "start_mic": Dv300Protocol.CMD_START_MIC,
            "start mic": Dv300Protocol.CMD_START_MIC,
            "开启麦克风": Dv300Protocol.CMD_START_MIC,
            "stop_mic": Dv300Protocol.CMD_STOP_MIC,
            "stop mic": Dv300Protocol.CMD_STOP_MIC,
            "关闭麦克风": Dv300Protocol.CMD_STOP_MIC,
            "query_capability": Dv300Protocol.CMD_QUERY_CAPABILITY,
            "capability": Dv300Protocol.CMD_QUERY_CAPABILITY,
            "查询能力": Dv300Protocol.CMD_QUERY_CAPABILITY,
            "ping": Dv300Protocol.CMD_PING,
        }
        return mapping.get(normalized)

    @staticmethod
    def _packet_to_text(ptype: int, payload: bytes) -> Optional[str]:
        if ptype == Dv300Protocol.MSG_HELLO:
            return f"[dv300] hello: {payload.decode('utf-8', errors='ignore')}"

        if ptype == Dv300Protocol.MSG_HEARTBEAT:
            return "[dv300] heartbeat"

        if ptype == Dv300Protocol.MSG_ACK:
            return f"[dv300] ack: {payload.decode('utf-8', errors='ignore')}"

        if ptype == Dv300Protocol.MSG_ERROR:
            return f"[dv300] error: {payload.decode('utf-8', errors='ignore')}"

        if ptype == Dv300Protocol.MSG_CAPABILITY:
            cap = Dv300Protocol.parse_capability(payload)
            if not cap:
                return "[dv300] capability: invalid payload"
            return (
                "[dv300] capability "
                f"camera={cap['camera_supported']}({cap['camera_backend']}) "
                f"mic={cap['microphone_supported']}({cap['microphone_backend']}) "
                f"camera_max={cap['camera_max_width']}x{cap['camera_max_height']}@{cap['camera_max_fps']} "
                f"mic_max={cap['microphone_max_sample_rate']}Hz/{cap['microphone_max_channels']}ch/{cap['microphone_max_bits']}bit"
            )

        if ptype == Dv300Protocol.MSG_CAMERA_FRAME:
            meta = Dv300Protocol.parse_camera_meta(payload)
            if not meta:
                return "[dv300] camera frame: invalid payload"
            return (
                "[dv300] camera frame "
                f"{meta['width']}x{meta['height']} fmt={meta['format']} bytes={meta['frame_len']}"
            )

        if ptype == Dv300Protocol.MSG_AUDIO_FRAME:
            meta = Dv300Protocol.parse_audio_meta(payload)
            if not meta:
                return "[dv300] audio frame: invalid payload"
            return (
                "[dv300] audio frame "
                f"{meta['sample_rate']}Hz {meta['channels']}ch {meta['bits_per_sample']}bit bytes={meta['frame_len']}"
            )

        return None
