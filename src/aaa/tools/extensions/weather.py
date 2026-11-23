"""天气工具：wttr.in 纯文本接口，无需 API Key。"""

import urllib.parse
import urllib.request

# 天气工具的网络超时（秒）默认值；实际值由 `config.toml` 的 tools.weather_timeout 传入。
WEATHER_TIMEOUT_SECONDS = 10


def weather_tool(timeout_seconds: int = WEATHER_TIMEOUT_SECONDS):
    """按给定超时构造天气工具函数；工具签名与文档面向模型。"""

    def get_weather(city: str) -> str:
        """查询某个城市的当前天气。

        使用 wttr.in 的纯文本接口，无需 API Key、无需额外依赖。

        Args:
            city: 城市名，中英文均可，例如 "Beijing" 或 "杭州"。

        Returns:
            单行天气摘要；网络失败时返回以 "Error:" 开头的说明。
        """
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3&lang=zh"
        request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        try:
            with urllib.request.urlopen(request, timeout=int(timeout_seconds)) as response:
                return response.read().decode("utf-8", errors="replace").strip()
        except Exception as exc:  # 工具失败要变成可读结果回填给模型，而不是让进程崩溃
            return f"Error: 天气查询失败（{exc}）"

    return get_weather


get_weather = weather_tool()
