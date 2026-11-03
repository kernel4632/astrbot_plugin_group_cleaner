"""把插件父目录加入 sys.path，并为非 AstrBot 环境安装最小 astrbot 桩模块。

本插件的 cleaner/state 模块只依赖标准库；但 main.py 依赖 astrbot 的装饰器与
基础类。为了让测试能 import main.py（例如回归测试"提醒关闭时直接执行不应死锁"），
这里在缺少真实 astrbot 包时注册一组最小可用的桩模块。
"""

import sys
import types
from pathlib import Path

PARENT = Path(__file__).resolve().parents[1].parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))


def _install_astrbot_stubs() -> None:
    try:  # 真实 AstrBot 环境下无需桩
        import astrbot  # noqa: F401
    except Exception:
        pass
    else:
        return

    def _module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    # astrbot
    _module("astrbot")

    # astrbot.api
    api = _module("astrbot.api")

    class _Logger:
        def _noop(self, *a, **k):
            return None

        debug = info = warning = error = critical = _noop

    api.logger = _Logger()
    api.message_components = types.SimpleNamespace(
        At=lambda **k: k, Plain=lambda *a, **k: a
    )

    # astrbot.api.event {filter, MessageChain}
    event = _module("astrbot.api.event")

    class _Decorator:
        """接收任意参数并原样返回被装饰函数。"""

        def __call__(self, *args, **kwargs):
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]

            def _wrap(func):
                return func

            return _wrap

    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"

    class EventMessageType:
        GROUP_MESSAGE = "group"

    flt = types.SimpleNamespace(
        on_astrbot_loaded=_Decorator(),
        command=_Decorator(),
        platform_adapter_type=_Decorator(),
        event_message_type=_Decorator(),
        PlatformAdapterType=PlatformAdapterType,
        EventMessageType=EventMessageType,
    )
    event.filter = flt
    event.MessageChain = lambda *a, **k: types.SimpleNamespace(chain=[])

    # astrbot.api.star {Context, Star}
    star = _module("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    star.Context = Context
    star.Star = Star

    # astrbot.core {AstrBotConfig}
    core = _module("astrbot.core")

    class AstrBotConfig(dict):
        pass

    core.AstrBotConfig = AstrBotConfig

    # astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter
    _module("astrbot.core.platform")
    _module("astrbot.core.platform.sources")
    _module("astrbot.core.platform.sources.aiocqhttp")
    adapter = _module(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter"
    )

    class AiocqhttpAdapter:
        pass

    class AiocqhttpMessageEvent:
        pass

    adapter.AiocqhttpAdapter = AiocqhttpAdapter
    adapter.AiocqhttpMessageEvent = AiocqhttpMessageEvent

    # astrbot.core.star.star_tools {StarTools}
    _module("astrbot.core.star")
    star_tools = _module("astrbot.core.star.star_tools")

    class StarTools:
        @staticmethod
        def get_data_dir(name: str) -> Path:
            return Path("/tmp")

    star_tools.StarTools = StarTools


_install_astrbot_stubs()
