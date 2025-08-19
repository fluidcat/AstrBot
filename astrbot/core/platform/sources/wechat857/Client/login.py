import hashlib
import string
from random import choice
from typing import Union

import aiohttp

from .base import *
from .errors import *


class LoginMixin(WechatAPIClientBase):
    async def is_running(self) -> bool:
        """检查WechatAPI是否在运行。

        Returns:
            bool: 如果WechatAPI正在运行返回True，否则返回False。
        """
        try:
            async with aiohttp.ClientSession() as session:
                response = await session.post(Uri.AutoHeartBeatLog + '?wxid=123')
                json_resp = await response.json()
                return json_resp.get("Success")
        except aiohttp.client_exceptions.ClientConnectorError:
            return False

    async def get_qr_code(self, device_name: str, device_id: str = "", proxy: Proxy = None) -> (
            str, str):
        """获取登录二维码。

        Args:
            device_name (str): 设备名称
            device_id (str, optional): 设备ID. Defaults to "".
            proxy (Proxy, optional): 代理信息. Defaults to None.

        Returns:
            tuple[str, str, str]: 返回UUID，URL，登录二维码

        Raises:
            根据error_handler处理错误
        """
        async with aiohttp.ClientSession() as session:
            json_param = {'DeviceName': device_name, 'DeviceID': device_id}
            if proxy:
                json_param['Proxy'] = {'ProxyIp': f'{proxy.ip}:{proxy.port}',
                                       'ProxyPassword': proxy.password,
                                       'ProxyUser': proxy.username}

            response = await session.post(Uri.LoginGetQRx, json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                qrcode_data = f'http://weixin.qq.com/x/{json_resp.get("Data").get("Uuid")}'
                url = f"https://api.pwmqr.com/qrcode/create/?url={qrcode_data}"

                return json_resp.get("Data").get("Uuid"), url
            else:
                self.error_handler(json_resp)

    async def check_login_uuid(self, uuid: str, device_id: str = "") -> tuple[bool, Union[dict, int]]:
        """检查登录的UUID状态。

        Args:
            uuid (str): 登录的UUID
            device_id (str, optional): 设备ID. Defaults to "".

        Returns:
            tuple[bool, Union[dict, int]]: 如果登录成功返回(True, 用户信息)，否则返回(False, 过期时间)

        Raises:
            根据error_handler处理错误
        """
        async with aiohttp.ClientSession() as session:
            json_param = {"uuid": uuid}
            response = await session.post(Uri.LoginCheckQR, params=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                if json_resp.get("Data").get("acctSectResp", ""):
                    self.wxid = json_resp.get("Data").get("acctSectResp").get("userName")
                    self.nickname = json_resp.get("Data").get("acctSectResp").get("nickName")
                    return True, json_resp.get("Data")
                else:
                    return False, json_resp.get("Data").get("expiredTime")
            else:
                self.error_handler(json_resp)

    async def log_out(self) -> bool:
        """登出当前账号。

        Returns:
            bool: 登出成功返回True，否则返回False

        Raises:
            UserLoggedOut: 如果未登录时调用
            根据error_handler处理错误
        """
        if not self.wxid:
            raise UserLoggedOut("请先登录")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid}
            response = await session.post(Uri.LogOut, json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                return True
            elif json_resp.get("Success"):
                return False
            else:
                self.error_handler(json_resp)

    async def awaken_login(self, wxid: str = "") -> str:
        """唤醒登录。

        Args:
            wxid (str, optional): 要唤醒的微信ID. Defaults to "".

        Returns:
            str: 返回新的登录UUID

        Raises:
            Exception: 如果未提供wxid且未登录
            LoginError: 如果无法获取UUID
            根据error_handler处理错误
        """
        if not wxid and not self.wxid:
            raise Exception("Please login using QRCode first")

        if not wxid and self.wxid:
            wxid = self.wxid

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": wxid}
            response = await session.post(Uri.LoginAwaken, json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                if uuid := json_resp.get("Data", {}).get("Uuid"):
                    return uuid
                else:
                    raise LoginError("Please login using QRCode first")
            else:
                self.error_handler(json_resp)

    async def get_cached_info(self, wxid: str = None) -> dict:
        """获取登录缓存信息。

        Args:
            wxid (str, optional): 要查询的微信ID. Defaults to None.

        Returns:
            dict: 返回缓存信息，如果未提供wxid且未登录返回空字典
        """
        if not wxid:
            wxid = self.wxid

        if not wxid:
            return {}

        async with aiohttp.ClientSession() as session:
            response = await session.post(Uri.GetCacheInfo + f'?wxid={wxid}')
            json_resp = await response.json()

            if json_resp.get("Success"):
                return json_resp.get("Data")
            else:
                return {}

    async def heartbeat(self) -> bool:
        """发送心跳包。

        Returns:
            bool: 成功返回True，否则返回False

        Raises:
            UserLoggedOut: 如果未登录时调用
            根据error_handler处理错误
        """
        if not self.wxid:
            raise UserLoggedOut("请先登录")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid}
            response = await session.post(Uri.HeartBeat, json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                return True
            else:
                self.error_handler(json_resp)

    async def start_auto_heartbeat(self) -> bool:
        """开始自动心跳。

        Returns:
            bool: 成功返回True，否则返回False

        Raises:
            UserLoggedOut: 如果未登录时调用
            根据error_handler处理错误
        """
        if not self.wxid:
            raise UserLoggedOut("请先登录")

        async with aiohttp.ClientSession() as session:
            params = {"wxid": self.wxid}
            response = await session.post(Uri.AutoHeartBeat, params=params)
            json_resp = await response.json()

            if json_resp.get("Success"):
                return True
            else:
                self.error_handler(json_resp)

    async def stop_auto_heartbeat(self) -> bool:
        """停止自动心跳。

        Returns:
            bool: 成功返回True，否则返回False

        Raises:
            UserLoggedOut: 如果未登录时调用
            根据error_handler处理错误
        """
        if not self.wxid:
            raise UserLoggedOut("请先登录")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid}
            response = await session.post(Uri.CloseAutoHeartBeat, json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                return True
            else:
                self.error_handler(json_resp)

    async def get_auto_heartbeat_status(self) -> bool:
        """获取自动心跳状态。

        Returns:
            bool: 如果正在运行返回True，否则返回False

        Raises:
            UserLoggedOut: 如果未登录时调用
            根据error_handler处理错误
        """
        if not self.wxid:
            raise UserLoggedOut("请先登录")

        async with aiohttp.ClientSession() as session:
            json_param = {"Wxid": self.wxid}
            response = await session.post(Uri.AutoHeartBeatLog, json=json_param)
            json_resp = await response.json()

            if json_resp.get("Success"):
                return len(json_resp.get("Data")) > 0
            else:
                return self.error_handler(json_resp)

    @staticmethod
    def create_device_name() -> str:
        """生成一个随机的设备名。

        Returns:
            str: 返回生成的设备名
        """
        first_names = [
            "Oliver", "Emma", "Liam", "Ava", "Noah", "Sophia", "Elijah", "Isabella",
            "James", "Mia", "William", "Amelia", "Benjamin", "Harper", "Lucas", "Evelyn",
            "Henry", "Abigail", "Alexander", "Ella", "Jackson", "Scarlett", "Sebastian",
            "Grace", "Aiden", "Chloe", "Matthew", "Zoey", "Samuel", "Lily", "David",
            "Aria", "Joseph", "Riley", "Carter", "Nora", "Owen", "Luna", "Daniel",
            "Sofia", "Gabriel", "Ellie", "Matthew", "Avery", "Isaac", "Mila", "Leo",
            "Julian", "Layla"
        ]

        last_names = [
            "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
            "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
            "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
            "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
            "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill",
            "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell",
            "Mitchell", "Carter", "Roberts", "Gomez", "Phillips", "Evans"
        ]

        return choice(first_names) + " " + choice(last_names) + "'s Pad"

    @staticmethod
    def create_device_id(s: str = "") -> str:
        """生成设备ID。

        Args:
            s (str, optional): 用于生成ID的字符串. Defaults to "".

        Returns:
            str: 返回生成的设备ID
        """
        if s == "" or s == "string":
            s = ''.join(choice(string.ascii_letters) for _ in range(15))
        md5_hash = hashlib.md5(s.encode()).hexdigest()
        return "49" + md5_hash[2:]
