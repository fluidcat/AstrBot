import asyncio
import base64
import json
import os
import time
from datetime import datetime
from typing import Optional, Dict, Any

import aiohttp
import anyio

from astrbot import logger
from astrbot.api.message_components import Plain, Image, At, Record
from astrbot.api.platform import Platform, PlatformMetadata
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.platform.astrbot_message import (
    AstrBotMessage,
    MessageMember,
    MessageType,
)
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .Client import Wechat857Client
from .wechat857_message_event import WeChat857MessageEvent
from ...register import register_platform_adapter

try:
    from .xml_data_parser import WeChat857DataParser
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
        self.wxid = None  # 用于保存登录成功后的 wxid
        self.nickname = None  # 用于保存登录成功后的 昵称
        self.device_id = None  # 用于保存设备id
        self.device_name = None  # 用于保存设备名
        self.first_login_time = None  # 用于保存设备首次登录时间
        self.credentials_file = os.path.join(
            get_astrbot_data_path(), "wechat857_credentials.json"
        )  # 持久化文件路径
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
        logger.info("WeChat857 适配器正在启动...")

        if loaded_credentials := self.load_credentials():
            self.device_name = loaded_credentials.get("device_name")
            self.device_id = loaded_credentials.get("device_id")
            self.wxid = loaded_credentials.get("wxid")
            self.first_login_time = loaded_credentials.get("first_login_time")

        is_login_in = self.wxid and await self.client.is_logged_in(self.wxid)

        # 检查在线状态
        if is_login_in:
            logger.info("WeChat857 设备已在线，凭据存在，跳过扫码登录。")
        else:
            # 1. 检查设备
            if not self.device_id or not self.device_name:
                logger.info("WeChat857 无可用设备，将生成新设备。")
                if not self.device_name:
                    self.device_name = self.client.create_device_name()
                if not self.device_id:
                    self.device_id = self.client.create_device_id()

            # 2. 获取登录二维码
            if not is_login_in:
                logger.info("WeChat857 设备已离线，开始扫码登录。")
                uuid, qr_code_url = await self.client.get_qr_code(self.device_name, self.device_id)

                if qr_code_url:
                    logger.info(f"请扫描以下二维码登录: {qr_code_url}")
                else:
                    logger.error("无法获取登录二维码。")
                    return

                # 3. 检测扫码状态
                login_successful = await self.check_login_status(uuid)

                if login_successful:
                    logger.info("登录成功，WeChat857适配器已连接。")
                else:
                    logger.warning("登录失败或超时，WeChat857 适配器将关闭。")
                    await self.terminate()
                    return

        self.save_credentials()  # 登录成功后保存凭据
        await self.client_prepare()

        self.polling_handle_task = asyncio.create_task(self.start_polling())

        self._shutdown_event = asyncio.Event()
        await self._shutdown_event.wait()

        logger.info("WeChat857 适配器已停止。")

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
                logger.info("WeChat857 已开启自动心跳")
            else:
                logger.warning("WeChat857 开启自动心跳失败")
        except ValueError:
            logger.warning("WeChat857 自动心跳已在运行")
        except Exception as e:
            if "在运行" not in str(e):
                logger.warning("WeChat857 自动心跳已在运行")

        # 先接受堆积消息
        logger.info("WeChat857 处理堆积消息中")
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

                logger.debug(f"WeChat857 接受到 {len(data)} 条堆积消息")
                await asyncio.sleep(1)

        logger.info("WeChat857 处理堆积消息完毕")

    async def start_polling(self):
        logger.info("WeChat857 开始等待消息")
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
        if os.path.exists(self.credentials_file):
            try:
                with open(self.credentials_file, "r") as f:
                    credentials = json.load(f)
                    logger.info("成功加载 WeChat857 凭据。")
                    return credentials
            except Exception as e:
                logger.error(f"加载 WeChat857 凭据失败: {e}")
        return None

    def save_credentials(self):
        self.first_login_time = self.first_login_time or int(datetime.now().timestamp())
        credentials = {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "wxid": self.wxid,
            "first_login_time": self.first_login_time
        }
        try:
            # 确保数据目录存在
            data_dir = os.path.dirname(self.credentials_file)
            os.makedirs(data_dir, exist_ok=True)
            with open(self.credentials_file, "w") as f:
                json.dump(credentials, f)
        except Exception as e:
            logger.error(f"保存 WeChat857 凭据失败: {e}")

    async def check_login_status(self, uuid: str):
        """
        循环检测扫码状态。
        """
        while True:
            try:
                stat, data = await self.client.check_login_uuid(uuid, self.device_id)
                if stat:
                    self.wxid = data.get("acctSectResp").get("userName")
                    self.nickname = data.get("acctSectResp").get("nickName")
                    logger.info(
                        f"登录成功，wxid: {self.wxid}, uuid: {uuid}"
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
        if '新春氛围视频' not in message['Content']:
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
        at_me = False
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

            msg_source = raw_message.get("MsgSource", "")
            if self.wxid in msg_source:
                at_me = True
            if "在群聊中@了你" in raw_message.get("PushContent", ""):
                at_me = True
            if at_me:
                abm.message.insert(0, At(qq=abm.self_id, name=""))
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
            logger.error(f"连接到 WeChat857 服务失败: {e}")
            return None
        except Exception as e:
            logger.error(f"获取群成员详情时发生错误: {e}")
            return None

    async def _process_message_content(
            self, abm: AstrBotMessage, raw_message: dict, msg_type: int, content: str
    ):
        """
        根据消息类型处理消息内容，填充 AstrBotMessage 的 message 列表。
        """
        if msg_type == 1:  # 文本消息
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

                    logger.debug(f"缓存文本消息，new_msg_id={new_msg_id}")
                    self.cached_texts[str(new_msg_id)] = content
            except Exception as e:
                logger.error(f"缓存文本消息失败: {e}")
        elif msg_type == 3:
            # 图片消息
            from_user_name = raw_message.get("FromUserName", {}).get("string", "")
            to_user_name = raw_message.get("ToUserName", {}).get("string", "")
            msg_id = raw_message.get("MsgId")
            image_bs64_data = await self.client.download_image(
                from_user_name, to_user_name, msg_id
            )
            if image_bs64_data:
                abm.message.append(Image.fromBase64(image_bs64_data))
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

                        logger.debug(f"缓存图片消息，new_msg_id={new_msg_id}")
                        self.cached_images[str(new_msg_id)] = image_bs64_data
                except Exception as e:
                    logger.error(f"缓存图片消息失败: {e}")
        elif msg_type == 47:
            # 视频消息 (注意：表情消息也是 47，需要区分) todo
            data_parser = WeChat857DataParser(
                content=content,
                is_private_chat=(abm.type != MessageType.GROUP_MESSAGE),
                raw_message=raw_message,
            )
            emoji_message = data_parser.parse_emoji()
            if emoji_message is not None:
                abm.message.append(emoji_message)
        elif msg_type == 50:
            logger.warning("收到语音/视频消息，待实现。")
        elif msg_type == 34:
            # 语音消息
            bufid = 0
            to_user_name = raw_message.get("ToUserName", {}).get("string", "")
            from_user_name = raw_message.get("FromUserName", {}).get("string", "")
            msg_id = raw_message.get("MsgId")
            data_parser = WeChat857DataParser(
                content=content,
                is_private_chat=(abm.type != MessageType.GROUP_MESSAGE),
                raw_message=raw_message,
            )

            voicemsg = data_parser._format_to_xml().find("voicemsg")
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
                abm.message.append(Record(file=file_path, url=file_path))
        elif msg_type == 49:
            try:
                parser = WeChat857DataParser(
                    content=content,
                    is_private_chat=(abm.type != MessageType.GROUP_MESSAGE),
                    cached_texts=self.cached_texts,
                    cached_images=self.cached_images,
                    raw_message=raw_message,
                    downloader=self.client.download_image,
                )
                components = await parser.parse_mutil_49()
                if components:
                    abm.message.extend(components)
                    abm.message_str = "\n".join(
                        c.text for c in components if isinstance(c, Plain)
                    )
            except Exception as e:
                logger.warning(f"msg_type 49 处理失败: {e}")
                abm.message.append(Plain("[XML 消息处理失败]"))
                abm.message_str = "[XML 消息处理失败]"
        else:
            logger.warning(f"收到未处理的消息类型: {msg_type}。")

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
