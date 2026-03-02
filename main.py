from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_dv300_adapter",
    "vmoranv",
    "DV300 platform adapter for AstrBot",
    "0.1.0",
)
class Dv300AdapterPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # Import on startup so decorator registration takes effect.
        from .dv300_platform_adapter import Dv300PlatformAdapter  # noqa: F401
