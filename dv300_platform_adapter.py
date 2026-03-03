import asyncio
import io
import os
import re
import tempfile
import time
import wave
from typing import Any, Dict, Optional, Tuple

import aiohttp
from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
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
        "accept_raw_ascii_compat": False,
        "emit_system_events": False,
        "emit_media_events": False,
        "startup_query_capability": True,
        "enable_voice_asr": True,
        "asr_api_base": "https://api.openai.com/v1",
        "asr_api_key": "",
        "asr_model": "whisper-1",
        "asr_language": "zh",
        "voice_segment_ms": 1800,
        "voice_min_bytes": 3200,
        "voice_max_bytes": 32000,
        "enable_camera_multimodal": True,
        "camera_emit_interval_ms": 5000,
        "camera_prompt": "这是来自DV300摄像头的抓拍图像，不是屏幕截图。请仅描述相机画面内容。",
    },
    adapter_display_name="DV300 Adapter",
)
class Dv300PlatformAdapter(Platform):
    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        return default

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
        self.accept_raw_ascii_compat = self._as_bool(
            self.config.get("accept_raw_ascii_compat", False), False
        )

        self.emit_system_events = self._as_bool(
            self.config.get("emit_system_events", False), False
        )
        self.emit_media_events = self._as_bool(
            self.config.get("emit_media_events", False), False
        )
        self.startup_query_capability = self._as_bool(
            self.config.get("startup_query_capability", True)
        )

        self.enable_voice_asr = self._as_bool(
            self.config.get("enable_voice_asr", True), True
        )
        self.asr_api_base = self.config.get("asr_api_base", "https://api.openai.com/v1")
        self.asr_api_key = self.config.get("asr_api_key", "")
        self.asr_model = self.config.get("asr_model", "whisper-1")
        self.asr_language = self.config.get("asr_language", "zh")
        self.voice_segment_ms = int(self.config.get("voice_segment_ms", 1800))
        self.voice_min_bytes = int(self.config.get("voice_min_bytes", 3200))
        self.voice_max_bytes = int(self.config.get("voice_max_bytes", 32000))

        self.enable_camera_multimodal = self._as_bool(
            self.config.get("enable_camera_multimodal", True)
        )
        self.camera_emit_interval_ms = int(self.config.get("camera_emit_interval_ms", 5000))
        self.camera_prompt = self.config.get("camera_prompt", "这是来自DV300摄像头的抓拍图像，不是屏幕截图。请仅描述相机画面内容。")

        self.client: Optional[Dv300UdpClient] = None
        self._device_addr: Optional[Tuple[str, int]] = None

        self._audio_states: Dict[str, Dict[str, Any]] = {}
        self._last_camera_emit_ms: Dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._warned_missing_asr_key = False

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
                if not self.accept_raw_ascii_compat:
                    continue
                # Compatibility: old board-side may send raw UTF-8 text directly.
                if not self._is_probably_text_datagram(frame.data):
                    continue
                text = frame.data.decode("utf-8", errors="ignore").strip()
                if text:
                    await self._emit_text_message(text, frame.addr, "raw_ascii")
                continue

            self._device_addr = frame.addr
            await self._handle_packet(packet, frame.addr)

    async def terminate(self):
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

        if self.client is not None:
            await self.client.close()
            self.client = None

    async def send_by_session(self, session, message_chain: MessageChain):
        await super().send_by_session(session, message_chain)
        session_id = getattr(session, "session_id", None) or getattr(
            session, "id", None
        )

        for comp in message_chain.chain:
            if isinstance(comp, Plain) and comp.text:
                await self.send_text_command(session_id, comp.text)

    async def _handle_packet(self, packet: Dict[str, Any], addr: Tuple[str, int]):
        ptype = packet["type"]

        if ptype == Dv300Protocol.MSG_AUDIO_FRAME:
            await self._handle_audio_frame(packet, addr)
            return

        message = await self.convert_message(packet, addr)
        if message is not None:
            await self.handle_msg(message)

    async def convert_message(
        self, packet: Dict[str, Any], addr: Tuple[str, int]
    ) -> Optional[AstrBotMessage]:
        ptype = packet["type"]
        payload = packet["payload"]
        session_id = f"{addr[0]}:{addr[1]}"

        # Heartbeat is a transport keepalive signal only.
        # Never forward it into AstrBot conversation pipeline.
        if ptype == Dv300Protocol.MSG_HEARTBEAT:
            return None

        if ptype in (
            Dv300Protocol.MSG_HELLO,
            Dv300Protocol.MSG_ACK,
            Dv300Protocol.MSG_ERROR,
            Dv300Protocol.MSG_CAPABILITY,
        ):
            summary = self._packet_to_text(ptype, payload)
            if summary:
                logger.info(summary)
            if self.emit_system_events and summary:
                return self._build_plain_message(
                    session_id=session_id,
                    text=summary,
                    packet=packet,
                    addr=addr,
                )
            return None

        if ptype == Dv300Protocol.MSG_TEXT:
            text = self._sanitize_text(payload.decode("utf-8", errors="ignore"))
            if not text or self._is_heartbeat_text(text):
                return None
            if self._looks_like_system_log(text):
                logger.info("[dv300] ignore incoming log-like text: %s", text[:160])
                return None
            cmd = self._map_text_to_command(text)
            if cmd is not None:
                logger.info("[dv300] intercepted text command: %s", text)
                await self.send_command(cmd, addr=addr)
                return None
            return self._build_plain_message(
                session_id=session_id,
                text=text,
                packet=packet,
                addr=addr,
            )

        if ptype == Dv300Protocol.MSG_CAMERA_FRAME:
            return await self._build_camera_message(packet, addr)

        if self.emit_system_events:
            summary = self._packet_to_text(ptype, payload)
            if summary:
                return self._build_plain_message(
                    session_id=session_id,
                    text=summary,
                    packet=packet,
                    addr=addr,
                )
        return None

    async def handle_msg(self, message: AstrBotMessage):
        text = (getattr(message, "message_str", None) or "").strip()
        if self._is_heartbeat_text(text):
            return
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

        await self.send_text_packet(text, addr=addr)

    async def send_command(
        self, command: int, argument: str = "", addr: Optional[Tuple[str, int]] = None
    ):
        if self.client is None:
            logger.warning("[dv300] udp client not ready, drop command=%s", command)
            return
        payload = Dv300Protocol.pack_command_payload(command, argument)
        packet = Dv300Protocol.pack_packet(Dv300Protocol.MSG_COMMAND, payload)
        target = addr or self._resolve_addr(None)
        self.client.sendto(packet, target)

    async def send_text_packet(self, text: str, addr: Optional[Tuple[str, int]] = None):
        if self.client is None:
            logger.warning("[dv300] udp client not ready, drop text packet")
            return
        target = addr or self._resolve_addr(None)
        for chunk in self._iter_text_chunks(text, Dv300Protocol.CAP_SIZE):
            packet = Dv300Protocol.pack_packet(
                Dv300Protocol.MSG_TEXT,
                chunk.encode("utf-8", errors="ignore"),
            )
            self.client.sendto(packet, target)

    async def _emit_text_message(self, text: str, addr: Tuple[str, int], raw_type: str):
        text = self._sanitize_text(text)
        if not text:
            return
        if self._is_heartbeat_text(text):
            return
        if self._looks_like_system_log(text):
            logger.info("[dv300] drop log-like text (%s): %s", raw_type, text[:160])
            return
        cmd = self._map_text_to_command(text)
        if cmd is not None:
            logger.info("[dv300] intercepted text command (%s): %s", raw_type, text)
            await self.send_command(cmd, addr=addr)
            return
        if self._looks_like_local_slash_command(text):
            logger.info("[dv300] drop slash-like text (%s): %s", raw_type, text[:160])
            return
        session_id = f"{addr[0]}:{addr[1]}"
        packet = {
            "type": Dv300Protocol.MSG_TEXT,
            "seq": 0,
            "timestamp_ms": int(time.time() * 1000),
            "payload_size": len(text.encode("utf-8", errors="ignore")),
            "payload": text.encode("utf-8", errors="ignore"),
            "raw_type": raw_type,
        }
        message = self._build_plain_message(session_id, text, packet, addr)
        await self.handle_msg(message)

    async def _handle_audio_frame(self, packet: Dict[str, Any], addr: Tuple[str, int]):
        payload = packet["payload"]
        session_id = f"{addr[0]}:{addr[1]}"
        meta = Dv300Protocol.parse_audio_meta(payload)
        if not meta:
            if self.emit_media_events:
                text = "[dv300] audio frame: invalid payload"
                await self._emit_text_message(text, addr, "audio_meta_error")
            return

        start = Dv300Protocol.AUDIO_META_SIZE
        end = start + meta["frame_len"]
        if meta["frame_len"] <= 0 or end > len(payload):
            return

        audio_chunk = payload[start:end]

        state = self._audio_states.setdefault(
            session_id,
            {
                "pcm": bytearray(),
                "sample_rate": meta["sample_rate"],
                "channels": meta["channels"],
                "bits_per_sample": meta["bits_per_sample"],
                "first_ms": 0,
                "last_ms": 0,
            },
        )

        now_ms = int(time.time() * 1000)
        if state["first_ms"] == 0:
            state["first_ms"] = now_ms

        state["sample_rate"] = meta["sample_rate"]
        state["channels"] = meta["channels"]
        state["bits_per_sample"] = meta["bits_per_sample"]
        state["last_ms"] = now_ms
        state["pcm"].extend(audio_chunk)

        pcm_len = len(state["pcm"])
        elapsed = now_ms - state["first_ms"]
        if pcm_len < self.voice_min_bytes:
            return

        if elapsed < self.voice_segment_ms and pcm_len < self.voice_max_bytes:
            return

        pcm_bytes = bytes(state["pcm"])
        state["pcm"].clear()
        state["first_ms"] = 0

        task = asyncio.create_task(
            self._process_asr_segment(
                session_id=session_id,
                addr=addr,
                packet=packet,
                pcm_bytes=pcm_bytes,
                sample_rate=int(state["sample_rate"]),
                channels=int(state["channels"]),
                bits_per_sample=int(state["bits_per_sample"]),
            )
        )
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.discard(t))

    async def _process_asr_segment(
        self,
        session_id: str,
        addr: Tuple[str, int],
        packet: Dict[str, Any],
        pcm_bytes: bytes,
        sample_rate: int,
        channels: int,
        bits_per_sample: int,
    ):
        text = await self._transcribe_pcm(
            pcm_bytes=pcm_bytes,
            sample_rate=sample_rate,
            channels=channels,
            bits_per_sample=bits_per_sample,
        )

        if text:
            message = self._build_plain_message(
                session_id=session_id,
                text=text,
                packet=packet,
                addr=addr,
            )
            await self.handle_msg(message)
            return

        if self.emit_media_events:
            media_text = (
                "[dv300] audio segment dropped: "
                f"{sample_rate}Hz/{channels}ch/{bits_per_sample}bit bytes={len(pcm_bytes)}"
            )
            message = self._build_plain_message(
                session_id=session_id,
                text=media_text,
                packet=packet,
                addr=addr,
            )
            await self.handle_msg(message)

    async def _transcribe_pcm(
        self, pcm_bytes: bytes, sample_rate: int, channels: int, bits_per_sample: int
    ) -> str:
        if not self.enable_voice_asr:
            return ""

        base_url = (self.asr_api_base or "https://api.openai.com/v1").strip().rstrip("/")
        local_asr = self._is_local_asr_base(base_url)

        api_key = (self.asr_api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        if not api_key and not local_asr:
            if not self._warned_missing_asr_key:
                logger.warning("[dv300] ASR disabled: asr_api_key is empty")
                self._warned_missing_asr_key = True
            return ""

        wav_buf = io.BytesIO()
        sampwidth = max(1, bits_per_sample // 8)

        with wave.open(wav_buf, "wb") as wav_file:
            wav_file.setnchannels(max(1, channels))
            wav_file.setsampwidth(sampwidth)
            wav_file.setframerate(max(1, sample_rate))
            wav_file.writeframes(pcm_bytes)

        wav_bytes = wav_buf.getvalue()

        url = base_url + "/audio/transcriptions"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        data = aiohttp.FormData()
        data.add_field(
            "file",
            wav_bytes,
            filename="dv300_audio.wav",
            content_type="audio/wav",
        )
        data.add_field("model", self.asr_model)
        if self.asr_language:
            data.add_field("language", self.asr_language)

        timeout = aiohttp.ClientTimeout(total=45)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status >= 300:
                        body = await resp.text()
                        logger.warning(
                            "[dv300] ASR failed status=%s body=%s", resp.status, body
                        )
                        return ""

                    payload = await resp.json(content_type=None)
                    text = str(payload.get("text", "")).strip()
                    return text
        except Exception as exc:
            logger.warning("[dv300] ASR request error: %s", exc)
            return ""

    async def _build_camera_message(
        self, packet: Dict[str, Any], addr: Tuple[str, int]
    ) -> Optional[AstrBotMessage]:
        payload = packet["payload"]
        meta = Dv300Protocol.parse_camera_meta(payload)
        session_id = f"{addr[0]}:{addr[1]}"

        if not meta:
            if self.emit_media_events:
                return self._build_plain_message(
                    session_id=session_id,
                    text="[dv300] camera frame: invalid payload",
                    packet=packet,
                    addr=addr,
                )
            return None

        now_ms = int(time.time() * 1000)
        last_ms = self._last_camera_emit_ms.get(session_id, 0)
        if (now_ms - last_ms) < self.camera_emit_interval_ms:
            return None

        start = Dv300Protocol.CAMERA_META_SIZE
        end = start + meta["frame_len"]
        if meta["frame_len"] <= 0 or end > len(payload):
            return None

        frame_bytes = payload[start:end]
        if not self.enable_camera_multimodal or not self._looks_like_image(
            frame_bytes, int(meta["format"])
        ):
            if self.emit_media_events:
                text = (
                    "[dv300] camera frame "
                    f"{meta['width']}x{meta['height']} fmt={meta['format']} bytes={meta['frame_len']}"
                )
                return self._build_plain_message(
                    session_id=session_id,
                    text=text,
                    packet=packet,
                    addr=addr,
                )
            return None

        ext = self._guess_image_ext(frame_bytes, int(meta["format"]))
        image_path = self._write_temp_image(frame_bytes, ext)

        abm = AstrBotMessage()
        abm.type = MessageType.FRIEND_MESSAGE
        abm.self_id = self.self_id
        abm.session_id = session_id
        abm.message_id = f"{packet['seq']}-{packet['timestamp_ms']}"
        abm.sender = MessageMember(user_id=session_id, nickname=f"dv300@{session_id}")
        abm.message = [
            Plain(text=self.camera_prompt),
            Image.fromFileSystem(path=image_path),
        ]
        abm.message_str = self.camera_prompt
        abm.raw_message = {
            "type": packet["type"],
            "seq": packet["seq"],
            "timestamp_ms": packet["timestamp_ms"],
            "payload_size": packet["payload_size"],
            "from": {"ip": addr[0], "port": addr[1]},
            "camera_meta": meta,
            "image_path": image_path,
        }
        abm.timestamp = packet["timestamp_ms"]

        self._last_camera_emit_ms[session_id] = now_ms
        return abm

    def _build_plain_message(
        self,
        session_id: str,
        text: str,
        packet: Dict[str, Any],
        addr: Tuple[str, int],
    ) -> AstrBotMessage:
        abm = AstrBotMessage()
        abm.type = MessageType.FRIEND_MESSAGE
        abm.self_id = self.self_id
        abm.session_id = session_id
        abm.message_id = f"{packet.get('seq', 0)}-{packet.get('timestamp_ms', int(time.time() * 1000))}"
        abm.sender = MessageMember(user_id=session_id, nickname=f"dv300@{session_id}")
        abm.message = [Plain(text=text)]
        abm.message_str = text
        abm.raw_message = {
            "type": packet.get("type"),
            "seq": packet.get("seq", 0),
            "timestamp_ms": packet.get("timestamp_ms", int(time.time() * 1000)),
            "payload_size": packet.get("payload_size", len(text.encode("utf-8", errors="ignore"))),
            "from": {"ip": addr[0], "port": addr[1]},
        }
        abm.timestamp = packet.get("timestamp_ms", int(time.time() * 1000))
        return abm

    def _write_temp_image(self, data: bytes, ext: str) -> str:
        fd, path = tempfile.mkstemp(prefix="dv300_", suffix=f".{ext}")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(data)
        return path

    @staticmethod
    def _looks_like_image(frame_bytes: bytes, frame_format: int) -> bool:
        if frame_format in (Dv300Protocol.CAMERA_FMT_JPEG, Dv300Protocol.CAMERA_FMT_PNG):
            return True
        if frame_bytes.startswith(b"\xff\xd8\xff"):
            return True
        if frame_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return True
        return False

    @staticmethod
    def _guess_image_ext(frame_bytes: bytes, frame_format: int) -> str:
        if frame_format == Dv300Protocol.CAMERA_FMT_JPEG:
            return "jpg"
        if frame_format == Dv300Protocol.CAMERA_FMT_PNG:
            return "png"
        if frame_bytes.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if frame_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        return "bin"

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
    def _iter_text_chunks(text: str, max_bytes: int):
        normalized = (text or "").strip()
        if not normalized:
            return []
        if max_bytes <= 0:
            return [normalized]
        chunks = []
        current = []
        current_bytes = 0
        for ch in normalized:
            encoded = ch.encode("utf-8", errors="ignore")
            if not encoded:
                continue
            if current and (current_bytes + len(encoded) > max_bytes):
                chunks.append("".join(current))
                current = []
                current_bytes = 0
            if len(encoded) > max_bytes:
                continue
            current.append(ch)
            current_bytes += len(encoded)
        if current:
            chunks.append("".join(current))
        return chunks

    @staticmethod
    def _sanitize_text(text: str) -> str:
        raw = text or ""
        cleaned = "".join(ch for ch in raw if ch.isprintable() or ch.isspace())
        return " ".join(cleaned.strip().split())

    @staticmethod
    def _looks_like_system_log(text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False
        lower = normalized.lower()
        if "liteipcread failed" in lower or "send ioctl failed" in lower:
            return True
        if "ipcrpc:" in lower or "multimedia:" in lower:
            return True
        if normalized.startswith(("[err][", "[warn][", "[info][", "01-01 ", "1-01 ")):
            return True
        if re.match(r"^\d{1,2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}", normalized):
            return True
        return False

    @staticmethod
    def _looks_like_local_slash_command(text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized.startswith("/"):
            return False
        token = normalized.split(" ", 1)[0].rstrip(".,;:!?").lower()
        return re.match(r"^/[a-z_][a-z0-9_/-]*$", token) is not None

    @staticmethod
    def _map_text_to_command(text: str) -> Optional[int]:
        normalized = (
            Dv300PlatformAdapter._sanitize_text(text)
            .lower()
            .replace("\\", "/")
        )
        if not normalized:
            return None
        mapping = {
            "start_camera": Dv300Protocol.CMD_START_CAMERA,
            "/start_camera": Dv300Protocol.CMD_START_CAMERA,
            "/camera_on": Dv300Protocol.CMD_START_CAMERA,
            "art_camera": Dv300Protocol.CMD_START_CAMERA,
            "start camera": Dv300Protocol.CMD_START_CAMERA,
            "开启摄像头": Dv300Protocol.CMD_START_CAMERA,
            "stop_camera": Dv300Protocol.CMD_STOP_CAMERA,
            "/stop_camera": Dv300Protocol.CMD_STOP_CAMERA,
            "/camera_off": Dv300Protocol.CMD_STOP_CAMERA,
            "top_camera": Dv300Protocol.CMD_STOP_CAMERA,
            "stop camera": Dv300Protocol.CMD_STOP_CAMERA,
            "关闭摄像头": Dv300Protocol.CMD_STOP_CAMERA,
            "start_mic": Dv300Protocol.CMD_START_MIC,
            "/start_mic": Dv300Protocol.CMD_START_MIC,
            "/mic_on": Dv300Protocol.CMD_START_MIC,
            "art_mic": Dv300Protocol.CMD_START_MIC,
            "start mic": Dv300Protocol.CMD_START_MIC,
            "开启麦克风": Dv300Protocol.CMD_START_MIC,
            "stop_mic": Dv300Protocol.CMD_STOP_MIC,
            "/stop_mic": Dv300Protocol.CMD_STOP_MIC,
            "/mic_off": Dv300Protocol.CMD_STOP_MIC,
            "top_mic": Dv300Protocol.CMD_STOP_MIC,
            "stop mic": Dv300Protocol.CMD_STOP_MIC,
            "关闭麦克风": Dv300Protocol.CMD_STOP_MIC,
            "query_capability": Dv300Protocol.CMD_QUERY_CAPABILITY,
            "/query_capability": Dv300Protocol.CMD_QUERY_CAPABILITY,
            "capability": Dv300Protocol.CMD_QUERY_CAPABILITY,
            "/capability": Dv300Protocol.CMD_QUERY_CAPABILITY,
            "/cap": Dv300Protocol.CMD_QUERY_CAPABILITY,
            "查询能力": Dv300Protocol.CMD_QUERY_CAPABILITY,
            "ping": Dv300Protocol.CMD_PING,
            "/ping": Dv300Protocol.CMD_PING,
            "snapshot": Dv300Protocol.CMD_CAPTURE_SNAPSHOT,
            "/snapshot": Dv300Protocol.CMD_CAPTURE_SNAPSHOT,
            "capture": Dv300Protocol.CMD_CAPTURE_SNAPSHOT,
            "/capture": Dv300Protocol.CMD_CAPTURE_SNAPSHOT,
            "take_photo": Dv300Protocol.CMD_CAPTURE_SNAPSHOT,
            "/take_photo": Dv300Protocol.CMD_CAPTURE_SNAPSHOT,
            "拍照": Dv300Protocol.CMD_CAPTURE_SNAPSHOT,
            "截图": Dv300Protocol.CMD_CAPTURE_SNAPSHOT,
        }
        direct = mapping.get(normalized)
        if direct is not None:
            return direct

        stripped = normalized.rstrip(".,;:!?")
        direct = mapping.get(stripped)
        if direct is not None:
            return direct

        first_token = stripped.split(" ", 1)[0]
        direct = mapping.get(first_token)
        if direct is not None:
            return direct

        if first_token.startswith("/"):
            return mapping.get(first_token[1:])

        # Single-token command completion: require >=3 chars and unique prefix.
        if " " not in stripped:
            compact = re.sub(r"[^a-z0-9_]", "", first_token.lstrip("/"))
            if len(compact) >= 3:
                aliases = {
                    "start_camera": Dv300Protocol.CMD_START_CAMERA,
                    "stop_camera": Dv300Protocol.CMD_STOP_CAMERA,
                    "start_mic": Dv300Protocol.CMD_START_MIC,
                    "stop_mic": Dv300Protocol.CMD_STOP_MIC,
                    "snapshot": Dv300Protocol.CMD_CAPTURE_SNAPSHOT,
                    "capability": Dv300Protocol.CMD_QUERY_CAPABILITY,
                    "ping": Dv300Protocol.CMD_PING,
                }
                matched = {
                    cmd
                    for name, cmd in aliases.items()
                    if name.startswith(compact)
                }
                if len(matched) == 1:
                    return next(iter(matched))

        return None

    @staticmethod
    def _is_heartbeat_text(text: str) -> bool:
        normalized = " ".join((text or "").strip().lower().split())
        if not normalized:
            return False
        if normalized in {"hb", "heartbeat", "[dv300] heartbeat"}:
            return True
        if normalized.startswith("heartbeat"):
            return True
        if "heartbeat" in normalized and "dv300" in normalized:
            return True
        return False

    @staticmethod
    def _is_probably_text_datagram(data: bytes) -> bool:
        if not data:
            return False
        if b"\x00" in data:
            return False
        sample = data[:256]
        printable = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13))
        return (printable / len(sample)) >= 0.9

    @staticmethod
    def _is_local_asr_base(base_url: str) -> bool:
        normalized = (base_url or "").strip().lower()
        return ("127.0.0.1" in normalized) or ("localhost" in normalized)

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

        if ptype == Dv300Protocol.MSG_TEXT:
            return payload.decode("utf-8", errors="ignore").strip()

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



