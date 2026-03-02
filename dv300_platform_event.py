from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata


class Dv300PlatformEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        adapter,
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._adapter = adapter

    async def send(self, message: MessageChain):
        for comp in message.chain:
            if isinstance(comp, Plain):
                await self._adapter.send_text_command(self.session_id, comp.text)
        await super().send(message)
