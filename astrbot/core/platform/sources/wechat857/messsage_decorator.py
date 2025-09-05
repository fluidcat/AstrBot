from asyncio.log import logger
from typing import Any, Callable, Dict, Optional, Tuple

from astrbot.core.message.components import BaseMessageComponent

class WechatMsg:
    _routes: Dict[Tuple[str, Optional[str]], Callable] = {}  # (msg_type, content_type) -> handler

    @classmethod
    def do(cls, msg_type: str, content_type: Optional[str] = None):
        """支持双类型注册的装饰器"""
        def decorator(fn: Callable[..., Any]):
            key = (msg_type, content_type)
            if key in cls._routes:
                raise KeyError(f"路由冲突: msg_type={msg_type}, content_type={content_type}")
            cls._routes[key] = fn  # 注册处理器
            return fn

        return decorator

    @classmethod
    async def convert(cls, msg_type: str, content_type: Optional[str] = None, *args: Any, **kwargs: Any)-> list[BaseMessageComponent] | None:
        """双层路由分发策略：
        1. 优先匹配 (msg_type, content_type)
        2. 降级匹配 (msg_type, None)
        3. 全部失败则日志
        """
        # 精确匹配
        if handler := cls._routes.get((msg_type, content_type)):
            return await handler(*args, **kwargs)

        # 降级匹配
        if handler := cls._routes.get((msg_type, None)):
            return await handler(*args, **kwargs)
        logger.warning(f"未支持的消息类型: msg_type={msg_type}{', content_type='+content_type if content_type else ''}")
