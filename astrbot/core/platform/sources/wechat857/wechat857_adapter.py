import asyncio
import base64
import json
import os
import time
from datetime import datetime
from typing import Optional, Dict, Any
from xml.etree.ElementTree import tostring

import aiohttp
import anyio

from astrbot import logger
from astrbot.api.message_components import Plain, Image, At, Record
from astrbot.api.platform import Platform, PlatformMetadata
from astrbot.core import astrbot_config as global_config
from astrbot.core.message.components import *
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.platform.astrbot_message import (
    AstrBotMessage,
    MessageMember,
    MessageType,
)
from .messsage_decorator import WechatMsg
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .Client import Wechat857Client
from .wechat857_message_event import WeChat857MessageEvent
from ...register import register_platform_adapter

try:
    from defusedxml import ElementTree as eT
except ImportError as e:
    logger.warning(
        f"警告: 可能未安装 defusedxml 依赖库，将导致无法解析微信的 表情包、引用 类型的消息: {str(e)}"
    )


@register_platform_adapter("wechat857", "WeChat857 消息平台适配器")
class WeChat857Adapter(Platform):
    def __init__(
            self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue
    ) -> None:
        super().__init__(event_queue)
        self._shutdown_event = None
        self.config = platform_config
        self.settings = platform_settings
        self.bot_id = self.config.get("id")
        self.unique_session = platform_settings.get("unique_session", False)

        self.metadata = PlatformMetadata(
            name="wechat857",
            description="WeChat857 消息平台适配器",
            id=self.config.get("id", "wechat857"),
        )

        # 保存配置信息
        self.host = self.config.get("host")
        self.port = self.config.get("port")
        self.client = Wechat857Client(self.host, self.port)
        self.base_url = f"http://{self.host}:{self.port}"
        self.wxid = self.config.get("wxid", "")  # 用于保存登录成功后的 wxid
        self.nickname = None  # 用于保存登录成功后的 昵称
        self.device_id = None  # 用于保存设备id
        self.device_name = None  # 用于保存设备名
        self.first_login_time = None  # 用于保存设备首次登录时间
        self.ws_handle_task = None
        self.polling_handle_task = None

        # 添加图片消息缓存，用于引用消息处理
        self.cached_images = {}
        """缓存图片消息。key是NewMsgId (对应引用消息的svrid)，value是图片的base64数据"""
        # 设置缓存大小限制，避免内存占用过大
        self.max_image_cache = 50

        # 添加文本消息缓存，用于引用消息处理
        self.cached_texts = {}
        """缓存文本消息。key是NewMsgId (对应引用消息的svrid)，value是消息文本内容"""
        # 设置文本缓存大小限制
        self.max_text_cache = 100

    async def run(self) -> None:
        """
        启动平台适配器的运行实例。
        """
        self._shutdown_event = asyncio.Event()

        logger.info(f"{self.bot_id} 消息平台正在启动...")

        if loaded_credentials := self.load_credentials():
            self.device_name = loaded_credentials.get("device_name")
            self.device_id = loaded_credentials.get("device_id")
            self.first_login_time = loaded_credentials.get("first_login_time")

        is_login_in = self.wxid and await self.client.is_logged_in(self.wxid)

        # 检查在线状态
        if is_login_in:
            logger.info(f"{self.bot_id} 设备已在线，凭据存在，跳过扫码登录。")
        else:
            # 1. 检查设备
            if not self.device_id or not self.device_name:
                logger.info(f"{self.bot_id} 无可用设备，将生成新设备。")
                if not self.device_name:
                    self.device_name = self.client.create_device_name()
                if not self.device_id:
                    self.device_id = self.client.create_device_id()

            # 2. 获取登录二维码
            if not is_login_in:
                logger.info(f"{self.bot_id} 设备离线状态，开始扫码登录。")
                uuid, qr_code_url = await self.client.get_qr_code(self.device_name, self.device_id)

                if qr_code_url:
                    logger.info(f"请扫描以下二维码登录: {qr_code_url}")
                else:
                    logger.error("无法获取登录二维码。")
                    return

                # 3. 检测扫码状态
                login_successful = await self.check_login_status(uuid)

                if login_successful:
                    logger.info(f"登录成功, {self.bot_id}适配器已连接。")
                else:
                    logger.info(f"登录失败或超时, {self.bot_id} 适配器将关闭。")
                    await self.terminate()
                    return

        self.save_credentials()  # 登录成功后保存凭据
        await self.client_prepare()

        self.polling_handle_task = asyncio.create_task(self.start_polling())

        await self._shutdown_event.wait()

        logger.info(f"{self.bot_id} 适配器已停止。")

    async def client_prepare(self):
        # client初始化必要数据
        self.client.wxid = self.wxid
        profile = await self.client.get_profile()
        user_info = profile.get("userInfo")
        self.client.nickname = user_info.get("NickName").get("string")
        self.client.alias = user_info.get("Alias")
        self.client.phone = user_info.get("BindMobile").get("string")
        self.client.first_login_time = self.first_login_time

        # 开启自动心跳
        try:
            success = await self.client.start_auto_heartbeat()
            if success:
                logger.info(f"{self.bot_id} 已开启自动心跳")
            else:
                logger.warning(f"{self.bot_id} 开启自动心跳失败")
        except ValueError:
            logger.warning(f"{self.bot_id} 自动心跳已在运行")
        except Exception as e:
            if "在运行" not in str(e):
                logger.warning(f"{self.bot_id} 自动心跳已在运行")

        # 先接受堆积消息
        logger.info(f"{self.bot_id} 处理堆积消息中")
        count = 0

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                                         connector=aiohttp.TCPConnector(limit=100)) as session:
            while True:
                data = await self.client.sync_message(session)
                data = data.get("AddMsgs")
                if not data:
                    if count > 2:
                        break
                    else:
                        count += 1
                        continue

                logger.debug(f"{self.bot_id} 接受到 {len(data)} 条堆积消息")
                await asyncio.sleep(1)

        logger.info(f"{self.bot_id} 处理堆积消息完毕")

    async def start_polling(self):
        logger.info(f"{self.bot_id} 开始等待消息")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                                         connector=aiohttp.TCPConnector(limit=100)) as session:
            data = []
            while True:
                try:
                    data = await self.client.sync_message(session)
                except Exception as e:
                    logger.warning(f"获取新消息失败 {e}")
                    await asyncio.sleep(5)
                    continue

                data = data.get("AddMsgs")
                if data:
                    for message in data:
                        asyncio.create_task(self.handle_message(message))
                await asyncio.sleep(1)

    def load_credentials(self):
        if not self.wxid:
            return None
        credentials_file = os.path.join(get_astrbot_data_path(), f"wechat857_{self.wxid}_credentials.json")
        if os.path.exists(credentials_file):
            try:
                with open(credentials_file, "r") as f:
                    credentials = json.load(f)
                    logger.info(f"成功加载 {self.bot_id} 凭据。")
                    return credentials
            except Exception as e:
                logger.error(f"加载 {self.bot_id} 凭据失败: {e}")
        return None

    def save_credentials(self):
        if not self.wxid:
            raise Exception("wxid为空, 请检查设备登录状态")
        credentials_file = os.path.join(get_astrbot_data_path(), f"wechat857_{self.wxid}_credentials.json")
        self.first_login_time = self.first_login_time or int(datetime.now().timestamp())
        credentials = {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "wxid": self.wxid,
            "first_login_time": self.first_login_time
        }
        try:
            # 确保数据目录存在
            data_dir = os.path.dirname(credentials_file)
            os.makedirs(data_dir, exist_ok=True)
            with open(credentials_file, "w") as f:
                json.dump(credentials, f)
        except Exception as e:
            logger.error(f"保存 {self.bot_id} 凭据失败: {e}")
        # 保存wxid到消息平台配置
        self.config["wxid"] = self.wxid
        global_config.save_config()

    async def check_login_status(self, uuid: str):
        """
        循环检测扫码状态。
        """
        while not self._shutdown_event.is_set():
            try:
                stat, data = await self.client.check_login_uuid(uuid, self.device_id)
                if stat:
                    self.wxid = data.get("acctSectResp").get("userName")
                    self.nickname = data.get("acctSectResp").get("nickName")
                    logger.info(
                        f"登录成功, wxid: {self.wxid}, uuid: {uuid}"
                    )
                    return True

                logger.info(f"等待登录中，过期倒计时：{data}s")
                await asyncio.sleep(5)
            except Exception as es:
                logger.error(f"登录失败：{es}")
                logger.error("可能二维码已过期，请重新获取。")
                break
        return False

    async def handle_message(self, message: Dict[str, Any]):
        """
        处理从 WebSocket 接收到的消息。
        """
        if "新春氛围视频" not in message["Content"]:
            logger.debug(f"收到 轮训 消息: {message}")
        try:
            if message.get("MsgId") and message.get("FromUserName"):
                abm = await self.convert_message(message)
                if abm:
                    # 创建 WeChat857MessageEvent 实例
                    message_event = WeChat857MessageEvent(
                        message_str=abm.message_str,
                        message_obj=abm,
                        platform_meta=self.meta(),
                        session_id=abm.session_id,
                        # 传递适配器实例，以便在事件中调用 send 方法
                        adapter=self,
                    )
                    # 提交事件到事件队列
                    self.commit_event(message_event)
            else:
                logger.warning(f"收到未知结构的 轮训 消息: {message}")

        except json.JSONDecodeError:
            logger.error(f"无法解析 轮训 消息为 JSON: {message}")
        except Exception as e:
            logger.error(f"处理 轮训 消息时发生错误: {e}")

    async def convert_message(self, raw_message: dict) -> AstrBotMessage | None:
        """
        将 WeChat857 原始消息转换为 AstrBotMessage。
        """
        abm = AstrBotMessage()
        abm.raw_message = raw_message
        abm.message_id = str(raw_message.get("MsgId"))
        abm.timestamp = raw_message.get("CreateTime")
        abm.self_id = self.wxid

        if int(time.time()) - abm.timestamp > 180:
            logger.warning(
                f"忽略 3 分钟前的旧消息：消息时间戳 {abm.timestamp} 超过当前时间 {int(time.time())}。"
            )
            return None

        from_user_name = raw_message.get("FromUserName", {}).get("string", "")
        to_user_name = raw_message.get("ToUserName", {}).get("string", "")
        content = raw_message.get("Content", {}).get("string", "")
        push_content = raw_message.get("PushContent", "")
        msg_type = raw_message.get("MsgType")

        abm.message_str = ""
        abm.message = []

        # 如果是机器人自己发送的消息、回显消息或系统消息，忽略
        if from_user_name == self.wxid:
            logger.info("忽略来自自己的消息。")
            return None

        if from_user_name in ["weixin", "newsapp", "newsapp_wechat"]:
            logger.info("忽略来自微信团队的消息。")
            return None

        # 先判断群聊/私聊并设置基本属性
        if await self._process_chat_type(
                abm, raw_message, from_user_name, to_user_name, content, push_content
        ):
            # 再根据消息类型处理消息内容
            await self._process_message_content(abm, raw_message, msg_type, content)

            return abm
        return None

    async def _process_chat_type(
            self,
            abm: AstrBotMessage,
            raw_message: dict,
            from_user_name: str,
            to_user_name: str,
            content: str,
            push_content: str,
    ):
        """
        判断消息是群聊还是私聊，并设置 AstrBotMessage 的基本属性。
        """
        if from_user_name == "weixin":
            return False
        if "@chatroom" in from_user_name:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = from_user_name

            parts = content.split(":\n", 1)
            sender_wxid = parts[0] if len(parts) == 2 else ""
            abm.sender = MessageMember(user_id=sender_wxid, nickname="")

            # 获取群聊发送者的nickname
            if sender_wxid:
                accurate_nickname = await self._get_group_member_nickname(
                    abm.group_id, sender_wxid
                )
                if accurate_nickname:
                    abm.sender.nickname = accurate_nickname

            # 对于群聊，session_id 可以是群聊 ID 或发送者 ID + 群聊 ID (如果 unique_session 为 True)
            if self.unique_session:
                abm.session_id = f"{from_user_name}#{abm.sender.user_id}"
            else:
                abm.session_id = from_user_name
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.group_id = ""
            nick_name = ""
            if push_content and " : " in push_content:
                nick_name = push_content.split(" : ")[0]
            abm.sender = MessageMember(user_id=from_user_name, nickname=nick_name)
            abm.session_id = from_user_name
        return True

    async def _get_group_member_nickname(
            self, group_id: str, member_wxid: str
    ) -> Optional[str]:

        try:
            member_list = await self.client.get_chatroom_member_list(group_id)
            if member_list:
                for member in member_list:
                    if member.get("UserName") == member_wxid:
                        return member.get("NickName")
                logger.warning(
                    f"在群 {group_id} 中未找到成员 {member_wxid} 的昵称"
                )
            else:
                logger.error("获取群成员详情失败")
            return None
        except aiohttp.ClientConnectorError as e:
            logger.error(f"连接到 {self.bot_id} 服务失败: {e}")
            return None
        except Exception as e:
            logger.error(f"获取群成员详情时发生错误: {e}")
            return None

    def _format_to_xml(self, content, is_private_chat):
        try:
            msg_str = content
            if not is_private_chat:
                parts = content.split(":\n", 1)
                msg_str = parts[1] if len(parts) == 2 else content

            return eT.fromstring(msg_str)
        except Exception as e:
            logger.error(f"[XML解析失败] {e}")
            raise

    async def _process_message_content(
            self, abm: AstrBotMessage, raw_message: dict, msg_type: int, content: str
    ):
        """
        根据消息类型处理消息内容，填充 AstrBotMessage 的 message 列表。
        """
        content_type = None
        if msg_type == 49:
            xml_content = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE))
            content_type = xml_content.findtext("appmsg/type")
        elif msg_type == 10002:
            xml_content = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE))
            content_type = xml_content.get("type")

        components = await WechatMsg.convert(str(msg_type), content_type, self, abm, raw_message, content)
        if components:
            components = components if isinstance(components, list) else [components]
            abm.message.extend(components)
            abm.message_str = "\n".join(c.text for c in components if isinstance(c, Plain))

    @WechatMsg.do(msg_type="1")
    async def convert_text_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        abm.message_str = content
        if abm.type == MessageType.GROUP_MESSAGE:
            parts = content.split(":\n", 1)
            if len(parts) == 2:
                message_content = parts[1]
                abm.message_str = message_content

                # 检查是否@了机器人，参考 gewechat 的实现方式
                # 微信大部分客户端在@用户昵称后面，紧接着是一个\u2005字符（四分之一空格）
                at_me = False

                # 检查 msg_source 中是否包含机器人的 wxid
                # wechatpadpro 的格式: <atuserlist>wxid</atuserlist>
                # gewechat 的格式: <atuserlist><![CDATA[wxid]]></atuserlist>
                msg_source = raw_message.get("MsgSource", "")
                if (
                        f"<atuserlist>{abm.self_id}</atuserlist>" in msg_source
                        or f"<atuserlist>{abm.self_id}," in msg_source
                        or f",{abm.self_id}</atuserlist>" in msg_source
                ):
                    at_me = True

                # 也检查 push_content 中是否有@提示
                push_content = raw_message.get("PushContent", "")
                if "在群聊中@了你" in push_content:
                    at_me = True

                if at_me:
                    # 被@了，在消息开头插入At组件（参考gewechat的做法）
                    bot_nickname = await self._get_group_member_nickname(
                        abm.group_id, abm.self_id
                    )
                    abm.message.insert(
                        0, At(qq=abm.self_id, name=bot_nickname or abm.self_id)
                    )

                    # 只有当消息内容不仅仅是@时才添加Plain组件
                    if "\u2005" in message_content:
                        # 检查@之后是否还有其他内容
                        parts = message_content.split("\u2005")
                        if len(parts) > 1 and any(
                                part.strip() for part in parts[1:]
                        ):
                            abm.message.append(Plain(message_content))
                    else:
                        # 检查是否只包含@机器人
                        is_pure_at = False
                        if (
                                bot_nickname
                                and message_content.strip() == f"@{bot_nickname}"
                        ):
                            is_pure_at = True
                        if not is_pure_at:
                            abm.message.append(Plain(message_content))
                else:
                    # 没有@机器人，作为普通文本处理
                    abm.message.append(Plain(message_content))
            else:
                abm.message.append(Plain(abm.message_str))
        else:  # 私聊消息
            abm.message.append(Plain(abm.message_str))

        # 缓存文本消息，以便引用消息可以查找
        try:
            # 获取msg_id作为缓存的key
            new_msg_id = raw_message.get("NewMsgId")
            if new_msg_id:
                # 限制缓存大小
                if (
                        len(self.cached_texts) >= self.max_text_cache
                        and self.cached_texts
                ):
                    # 删除最早的一条缓存
                    oldest_key = next(iter(self.cached_texts))
                    self.cached_texts.pop(oldest_key)

                logger.debug(f"缓存文本消息, new_msg_id={new_msg_id}")
                self.cached_texts[str(new_msg_id)] = content
        except Exception as e:
            logger.error(f"缓存文本消息失败: {e}")

    @WechatMsg.do(msg_type="3")
    async def convert_image_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 图片消息
        from_user_name = raw_message.get("FromUserName", {}).get("string", "")
        to_user_name = raw_message.get("ToUserName", {}).get("string", "")
        msg_id = raw_message.get("MsgId")
        image_bs64_data = await self.client.download_image(
            from_user_name, to_user_name, msg_id
        )
        if image_bs64_data:
            image = Image.fromBase64(image_bs64_data)
            img_xml = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE)).find("img")
            image.__dict__["cdn_xml"] = f'<msg>{tostring(img_xml, encoding="unicode")}</msg>'
            abm.message.append(image)
            # 缓存图片，以便引用消息可以查找
            try:
                # 获取msg_id作为缓存的key
                new_msg_id = raw_message.get("NewMsgId")
                if new_msg_id:
                    # 限制缓存大小
                    if (
                            len(self.cached_images) >= self.max_image_cache
                            and self.cached_images
                    ):
                        # 删除最早的一条缓存
                        oldest_key = next(iter(self.cached_images))
                        self.cached_images.pop(oldest_key)

                    logger.debug(f"缓存图片消息, new_msg_id={new_msg_id}")
                    self.cached_images[str(new_msg_id)] = image_bs64_data
            except Exception as e:
                logger.error(f"缓存图片消息失败: {e}")

    @WechatMsg.do(msg_type="34")
    async def convert_voice_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 语音消息
        bufid = 0
        to_user_name = raw_message.get("ToUserName", {}).get("string", "")
        from_user_name = raw_message.get("FromUserName", {}).get("string", "")
        msg_id = raw_message.get("MsgId")

        voicemsg = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE)).find("voicemsg")
        bufid = voicemsg.get("bufid") or "0"
        length = int(voicemsg.get("length") or 0)
        voiceurl = voicemsg.get("voiceurl") or ""
        voice_bs64_data = await self.client.download_voice(msg_id, voiceurl, length, bufid, from_user_name)
        if voice_bs64_data:
            voice_bs64_data = base64.b64decode(voice_bs64_data)
            temp_dir = os.path.join(get_astrbot_data_path(), "temp")
            file_path = os.path.join(
                temp_dir, f"wechat857_voice_{abm.message_id}.silk"
            )

            async with await anyio.open_file(file_path, "wb") as f:
                await f.write(voice_bs64_data)
            record = Record(file=file_path, url=file_path)
            record.__dict__["cdn_xml"] = f'<msg>{tostring(voicemsg, encoding="unicode")}</msg>'
            abm.message.append(record)

    @WechatMsg.do(msg_type="42")
    async def convert_card_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        card_xml = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE))
        card_dict = {
            "wxid": card_xml.get("username", ""),
            "v3": card_xml.get("username", ""),
            "province": card_xml.get("province", ""),
            "city": card_xml.get("city", ""),
            "sign": card_xml.get("sign", ""),
            "sex": card_xml.get("sex", ""), # 1:男, 2:女
            "ticket": card_xml.get("ticket", ""),
            "headimgurl": card_xml.get("smallheadimgurl", ""),
            "nickname": card_xml.get("nickname", ""),
            "alias": card_xml.get("alias", ""), # 微信号
            "scene": card_xml.get("scene", "")
        }
        contact = Contact(card_dict["wxid"])
        contact.__dict__.update(card_dict)
        return contact


    @WechatMsg.do(msg_type="43")
    async def convert_video_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 视频消息
        msg_id = raw_message.get("MsgId")
        video_b64 = await self.client.download_video(msg_id)
        if video_b64:
            video_byte = base64.b64decode(video_b64)
            temp_dir = os.path.join(get_astrbot_data_path(), "temp")
            file_path = os.path.join(
                temp_dir, f"wechat857_video_{abm.message_id}.mp4"
            )
            async with await anyio.open_file(file_path, "wb") as f:
                await f.write(video_byte)
            video = Video(file=file_path, url=file_path)
            video_xml = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE)).find("videomsg")
            video.__dict__["cdn_xml"] = f'<msg>{tostring(video_xml, encoding="unicode")}</msg>'
            abm.message.append(video)

    @WechatMsg.do(msg_type="47")
    async def convert_emoji_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 表情消息
        """
        处理 msg_type == 47 的表情消息(emoji)
        """
        try:
            emoji_element = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE)).find(".//emoji")
            if emoji_element is not None:
                return WechatEmoji(
                    md5=emoji_element.get("md5"),
                    md5_len=emoji_element.get("len"),
                    cdnurl=emoji_element.get("cdnurl"),
                )
        except Exception as e:
            logger.error(f"[parse_emoji] 解析失败: {e}")

        return None

    @WechatMsg.do(msg_type="48")
    async def convert_location_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        location_xml = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE)).find("location")
        location = Location(
            lat = location_xml.get("x"),
            lon = location_xml.get("y"),
            title = location_xml.get("poiname",""),
            content = location_xml.get("label", "")
        )
        location.__dict__["scale"] = location_xml.get("scale")
        return location

    @WechatMsg.do(msg_type="49", content_type="3")
    @WechatMsg.do(msg_type="49", content_type="76")
    @WechatMsg.do(msg_type="49", content_type="92")
    async def convert_music_share_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 音乐分享
        _xml = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE)).find("appmsg")
        music = Music(
            audio = _xml.findtext("dataurl", _xml.findtext("url")),
            url = _xml.findtext("url"),
            image = _xml.findtext("songalbumurl"),
            title = _xml.findtext("title"),
        )
        return music

    @WechatMsg.do(msg_type="49", content_type="4")
    async def convert_video_share_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 视频分享
        logger.warning("视频分享消息暂不支持")

    @WechatMsg.do(msg_type="49", content_type="5")
    @WechatMsg.do(msg_type="49", content_type="1")
    async def convert_url_share_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 链接、图文分享
        logger.warning("链接、图文分享消息暂不支持")

    @WechatMsg.do(msg_type="49", content_type="6")
    async def convert_file_share_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        xml_msg = self._format_to_xml(content, (abm.type != MessageType.GROUP_MESSAGE))
        filename = xml_msg.findtext("appmsg/title")
        attach_id = xml_msg.findtext("appmsg/appattach/attachid")
        appmsg = xml_msg.find("appmsg")
        if attach_id:
            file_b64 = await self.client.download_attach(attach_id)
            temp_dir = os.path.join(get_astrbot_data_path(), "temp")
            file_path = os.path.join(temp_dir, f"wechat857_file_{filename}")
            async with await anyio.open_file(file_path, "wb") as f:
                await f.write(base64.b64decode(file_b64))
            comp = File(name=filename, file=file_path)
            comp.__dict__["cdn_xml"] = f'<msg>{tostring(appmsg, encoding="unicode")}</msg>'
            return [comp]

    @WechatMsg.do(msg_type="49", content_type="16")
    async def convert_ticket_share_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 卡券分享
        logger.warning("卡券分享消息暂不支持")

    @WechatMsg.do(msg_type="49", content_type="19")
    async def convert_chat_record_share_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 聊天记录分享
        logger.warning("聊天记录分享消息暂不支持")

    @WechatMsg.do(msg_type="49", content_type="33")
    async def convert_micro_program_share_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 小程序分享
        logger.warning("小程序分享消息暂不支持")

    @WechatMsg.do(msg_type="49", content_type="57")
    async def convert_quote_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 引用消息
        """
        处理 type == 57 的引用消息: 支持文本(1)、图片(3)、嵌套(49)
        """
        components = []

        try:
            appmsg = self._format_to_xml(content, abm.type!=MessageType.GROUP_MESSAGE).find("appmsg")
            if appmsg is None:
                return [Plain("[引用消息解析失败]")]

            refermsg = appmsg.find("refermsg")
            if refermsg is None:
                return [Plain("[引用消息解析失败]")]

            quote_type = int(refermsg.findtext("type", "0"))
            nickname = refermsg.findtext("displayname", "未知发送者")
            quote_content = refermsg.findtext("content", "")
            svrid = refermsg.findtext("svrid")

            match quote_type:
                case 1:  # 文本引用
                    quoted_text = self.cached_texts.get(str(svrid), quote_content)
                    components.append(Plain(f"[引用] {nickname}: {quoted_text}"))

                case 3:  # 图片引用
                    quoted_image_b64 = self.cached_images.get(str(svrid))
                    if not quoted_image_b64:
                        try:
                            quote_xml = eT.fromstring(quote_content)
                            img = quote_xml.find("img")
                            cdn_url = (
                                img.get("cdnbigimgurl") or img.get("cdnmidimgurl")
                                if img is not None
                                else None
                            )
                            if cdn_url:
                                quoted_image_b64 = await self.client.download_image(
                                    self.from_user_name, self.to_user_name, self.msg_id
                                )
                        except Exception as e:
                            logger.warning(f"[引用图片解析失败] svrid={svrid} err={e}")

                    if quoted_image_b64:
                        components.extend(
                            [
                                Image.fromBase64(quoted_image_b64),
                                Plain(f"[引用] {nickname}: [引用的图片]"),
                            ]
                        )
                    else:
                        components.append(
                            Plain(f"[引用] {nickname}: [引用的图片 - 未能获取]")
                        )

                case 49:  # 嵌套引用
                    try:
                        nested_root = eT.fromstring(quote_content)
                        nested_title = nested_root.findtext(".//appmsg/title", "")
                        components.append(Plain(f"[引用] {nickname}: {nested_title}"))
                    except Exception as e:
                        logger.warning(f"[嵌套引用解析失败] err={e}")
                        components.append(Plain(f"[引用] {nickname}: [嵌套引用消息]"))

                case _:  # 其他未识别类型
                    logger.info(f"[未知引用类型] quote_type={quote_type}")
                    components.append(Plain(f"[引用] {nickname}: [不支持的引用类型]"))

            # 主消息标题
            title = appmsg.findtext("title", "")
            if title:
                components.append(Plain(title))

        except Exception as e:
            logger.error(f"[parse_reply] 总体解析失败: {e}")
            return [Plain("[引用消息解析失败]")]

        return components

    @WechatMsg.do(msg_type="49", content_type="115")
    async def convert_gift_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 礼物消息
        logger.warning("礼物消息暂不支持")

    @WechatMsg.do(msg_type="49", content_type="2000")
    async def convert_transfer_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 转账消息
        logger.warning("转账消息暂不支持")

    @WechatMsg.do(msg_type="49", content_type="2001")
    async def convert_red_packet_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # 红包消息
        logger.warning("红包消息暂不支持")

    @WechatMsg.do(msg_type="10002", content_type="ilinkvoip")
    async def convert_ilinkvoip_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        #接到语音电话消息 <sysmsg type="ilinkvoip"><voipmt><invite>CgAQweXrm8uhs78HGjZvOWNxODA5dkZHTWs4MzhSWVd6QWRqM2xZTkZrX2ltLndlY2hhdF8xNzU3MDQzMDE4NDAxXzMiJm85Y3E4MDl2RkdNazgzOFJZV3pBZGozbFlORmtAaW0ud2VjaGF0KiZvOWNxODA1TTY2Z1g1b01UVUxnSFBKaEhzamVJQGltLndlY2hhdDKoAQgCEqMBCMHl65vLobO/BxCx0/Lc6air2mgYASACKiZvOWNxODA5dkZHTWs4MzhSWVd6QWRqM2xZTkZrQGltLndlY2hhdDImbzljcTgwNU02NmdYNW9NVFVMZ0hQSmhIc2plSUBpbS53ZWNoYXQ6E3d4aWRfajQ0bWh5cDczdWJwMjFCE3d4aWRfa3VqajdvYmpwODMxMjJSBndlY2hhdFoHdm9pcC0ycDo/ChN3eGlkX2t1amo3b2JqcDgzMTIyEiZvOWNxODA1TTY2Z1g1b01UVUxnSFBKaEhzamVJQGltLndlY2hhdBgAOj8KE3d4aWRfajQ0bWh5cDczdWJwMjESJm85Y3E4MDl2RkdNazgzOFJZV3pBZGozbFlORmtAaW0ud2VjaGF0GABCE3d4aWRfajQ0bWh5cDczdWJwMjFItaSzv5Ez</invite></voipmt></sysmsg>
        logger.warning("语音电话消息暂不支持")

    @WechatMsg.do(msg_type="10002", content_type="ilinkvoip_cancel")
    async def convert_ilinkvoip_cancel_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        #<sysmsg type="ilinkvoip"><voipmt><cancel>CN7nn5mfzoKsBxIaCAMSFgje55+Zn86CrAcQk+mekri+qdtoGAE=</cancel></voipmt></sysmsg>
        #<voipmsg type="VoIPBubbleMsg"><VoIPBubbleMsg><msg><![CDATA[对方已取消]]></msg>\n<room_type>1</room_type>\n<red_dot>true</red_dot>\n<roomid>529184440743097310</roomid>\n<roomkey>0</roomkey>\n<inviteid>360751520</inviteid>\n<msg_type>100</msg_type>\n<timestamp>1757002379511</timestamp>\n<identity><![CDATA[1933106944032004954]]></identity>\n<duration>0</duration>\n<inviteid64>1757002375584</inviteid64>\n<business>1</business>\n<caller_memberid>0</caller_memberid>\n<callee_memberid>1</callee_memberid>\n</VoIPBubbleMsg></voipmsg>
        logger.warning("语音电话消息暂不支持")

    @WechatMsg.do(msg_type="10002", content_type="pat")
    async def convert_pat_message(self, abm: AstrBotMessage, raw_message: dict, content: str):
        # <sysmsg type="pat">\n<pat>\n  <fromusername>wxid_j44mhyp73ubp21</fromusername>\n  <chatusername>wxid_kujj7objp83122</chatusername>\n  <pattedusername>wxid_kujj7objp83122</pattedusername>\n  <patsuffix><![CDATA[]]></patsuffix>\n  <patsuffixversion>0</patsuffixversion>\n\n\n\n\n  <template><![CDATA["${wxid_j44mhyp73ubp21}" 拍了拍我]]></template>\n\n\n\n\n</pat>\n</sysmsg>
        logger.warning("拍一拍消息暂不支持")

    async def terminate(self):
        """
        终止一个平台的运行实例。
        """
        logger.info("终止 WeChat857 适配器。")
        try:
            if self.ws_handle_task:
                self.ws_handle_task.cancel()
            if self.polling_handle_task:
                self.polling_handle_task.cancel()
            self._shutdown_event.set()
        except Exception:
            pass

    def meta(self) -> PlatformMetadata:
        """
        得到一个平台的元数据。
        """
        return self.metadata

    async def send_by_session(
            self, session: MessageSesion, message_chain: MessageChain
    ):
        dummy_message_obj = AstrBotMessage()
        dummy_message_obj.session_id = session.session_id
        # 根据 session_id 判断消息类型
        if "@chatroom" in session.session_id:
            dummy_message_obj.type = MessageType.GROUP_MESSAGE
            if "#" in session.session_id:
                dummy_message_obj.group_id = session.session_id.split("#")[0]
            else:
                dummy_message_obj.group_id = session.session_id
            dummy_message_obj.sender = MessageMember(user_id="", nickname="")
        else:
            dummy_message_obj.type = MessageType.FRIEND_MESSAGE
            dummy_message_obj.group_id = ""
            dummy_message_obj.sender = MessageMember(user_id="", nickname="")
        sending_event = WeChat857MessageEvent(
            message_str="",
            message_obj=dummy_message_obj,
            platform_meta=self.meta(),
            session_id=session.session_id,
            adapter=self,
        )
        # 调用实例方法 send
        await sending_event.send(message_chain)
