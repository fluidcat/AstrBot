import asyncio
import base64
import io
import os
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image as PILImage  # 使用别名避免冲突

from astrbot.core.message.components import (
    At,
    File,
    Image,
    Music,
    Plain,
    Video,
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
        self.session_id = self.session_id.removesuffix("_wxid")
        if (
            any(isinstance(m, At) for m in message.chain)
            and any(isinstance(m, Plain) for m in message.chain)
        ):
            await self._send_at_text(message)
        else:
            for comp in message.chain:
                await asyncio.sleep(1)
                if isinstance(comp, Plain):
                    await self._send_text(comp)
                elif isinstance(comp, At):
                    await self._send_at(comp)
                elif isinstance(comp, Image):
                    await self._send_image(comp)
                elif isinstance(comp, WechatEmoji):
                    await self._send_emoji(comp)
                elif isinstance(comp, Record):
                    await self._send_voice(comp)
                elif isinstance(comp, Video):
                    await self._send_video(comp)
                elif isinstance(comp, File):
                    await self._send_file(comp)
                elif isinstance(comp, Music):
                    await self._send_music(comp)
        await super().send(message)

    async def _send_image(self, comp: Image):
        file_path = await comp.convert_to_file_path()
        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id

        if comp.cdn_xml:
            await self.adapter.client.send_cdn_img_msg(session_id, comp.cdn_xml)
        else:
            await self.adapter.client.send_image_message(session_id, Path(file_path))

    async def _send_at_text(self, message: MessageChain):

        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id

        ats = []
        at_text = ""
        plain = ""
        for comp in message.chain:
            if isinstance(comp, At):
                ats.append(comp.qq)
                at_text += f"@{comp.name or comp.qq}\u2005"
            elif isinstance(comp, Plain):
                plain = comp.text
        await self.adapter.client.send_text_message(session_id, at_text + plain, ats)

    async def _send_at(self, comp: At):
        if self.message_obj.type != MessageType.GROUP_MESSAGE:
            return
        ats = comp.name or comp.qq
        at_text = f"@{ats}\u2005"

        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id
        await self.adapter.client.send_text_message(session_id, at_text, [ats])

    async def _send_text(self, comp: Plain):
        message_text = comp.text

        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id

        await self.adapter.client.send_text_message(session_id, message_text)

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

        if comp.cdn_xml:
            await self.adapter.client.send_cdn_file_msg(session_id, comp.cdn_xml)
        else:
            record_path = await comp.convert_to_file_path()
            ext = os.path.splitext(record_path)[1].lower()[1:]
            await self.adapter.client.send_voice_message(session_id, Path(record_path), ext)

    async def _send_video(self, comp: Video):
        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id

        if comp.cdn_xml:
            await self.adapter.client.send_cdn_video_msg(session_id, comp.cdn_xml)
        else:
            record_path = await comp.convert_to_file_path()
            ext = os.path.splitext(record_path)[1].lower()[1:]
            await self.adapter.client.send_voice_message(session_id, Path(record_path), ext)

    async def _send_file(self, comp: File):
        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id

        if comp.cdn_xml:
            await self.adapter.client.send_cdn_file_msg(session_id, comp.cdn_xml)
        else:
            raise NotImplementedError("暂不支持发送本地文件")

    # async def _send_xml(self, comp: Xml):
    #     if self.get_group_id() and "#" in self.session_id:
    #         session_id = self.session_id.split("#")[0]
    #     else:
    #         session_id = self.session_id
    #     await self.adapter.client.send_app_message(session_id, comp.data, comp.resid)

    async def _send_music(self, comp: Music):
        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id
        await self.adapter.client.send_app_message(session_id, comp.content, 3)

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
