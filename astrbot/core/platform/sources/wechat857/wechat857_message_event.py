import asyncio
import base64
import io
import os
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image as PILImage  # 使用别名避免冲突

from astrbot.core.message.components import (
    Image,
    Plain,
    WechatEmoji,
    Record,
)  # Import Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

if TYPE_CHECKING:
    from .wechat857_adapter import WeChat857Adapter


class WeChat857MessageEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        adapter: "WeChat857Adapter",  # 传递适配器实例
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.message_obj = message_obj  # Save the full message object
        self.adapter = adapter  # Save the adapter instance

    async def send(self, message: MessageChain):
        for comp in message.chain:
            await asyncio.sleep(1)
            if isinstance(comp, Plain):
                await self._send_text(comp)
            elif isinstance(comp, Image):
                await self._send_image(comp)
            elif isinstance(comp, WechatEmoji):
                await self._send_emoji(comp)
            elif isinstance(comp, Record):
                await self._send_voice(comp)
        await super().send(message)

    async def _send_image(self, comp: Image):
        file_path = await comp.convert_to_file_path()
        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id

        await self.adapter.client.send_image_message(session_id, Path(file_path))

    async def _send_text(self, comp: Plain):
        message_text = comp.text
        mention_id = []
        if (
            self.message_obj.type == MessageType.GROUP_MESSAGE  # 确保是群聊消息
            and self.adapter.settings.get(
                "reply_with_mention", False
            )  # 检查适配器设置是否启用 reply_with_mention
            and self.message_obj.sender  # 确保有发送者信息
            and (
                self.message_obj.sender.user_id or self.message_obj.sender.nickname
            )  # 确保发送者有 ID 或昵称
        ):
            # 优先使用 nickname，如果没有则使用 user_id
            mention_text = (
                self.message_obj.sender.nickname or self.message_obj.sender.user_id
            )
            mention_id = [self.message_obj.sender.user_id]
            message_text = f"@{mention_text}\u2005{message_text}"

        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id

        await self.adapter.client.send_text_message(session_id, message_text, mention_id)

    async def _send_emoji(self, comp: WechatEmoji):
        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id
        await self.adapter.client.send_emoji_message(session_id, comp.md5, comp.md5_len)

    async def _send_voice(self, comp: Record):
        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id

        record_path = await comp.convert_to_file_path()
        ext = os.path.splitext(record_path)[1].lower()

        await self.adapter.client.send_voice_message(session_id, Path(record_path), ext)

    @staticmethod
    def _validate_base64(b64: str) -> bytes:
        return base64.b64decode(b64, validate=True)

    @staticmethod
    def _compress_image(data: bytes) -> str:
        img = PILImage.open(io.BytesIO(data))
        buf = io.BytesIO()
        if img.format == "JPEG":
            img.save(buf, "JPEG", quality=80)
        else:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buf, "JPEG", quality=80)
        # logger.info("图片处理完成！！！")
        return base64.b64encode(buf.getvalue()).decode()


# TODO: 添加对其他消息组件类型的处理 (Record, Video, At等)
# elif isinstance(component, Record):
#     pass
# elif isinstance(component, Video):
#     pass
# elif isinstance(component, At):
#     pass
# ...
