"""天气工具：Open-Meteo，免 key。"""

from typing import Literal

import httpx
from pydantic import BaseModel, Field

from app.tools.registry import tool

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# 城市 → 坐标。Open-Meteo 单次请求实测 2~4s，两次串行会顶到超时线。
# 内置常见城市坐标：命中就跳过地理编码，直接省掉一半耗时（服务重启后依然有效）。
_BUILTIN_COORDS = {
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "杭州": (30.2741, 120.1551),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "成都": (30.5728, 104.0668),
    "武汉": (30.5928, 114.3055),
    "南京": (32.0603, 118.7969),
    "西安": (34.3416, 108.9398),
    "重庆": (29.5630, 106.5516),
}

# 地理编码结果缓存（仅进程内，服务重启失效）
_GEO_CACHE: dict = {}


class WeatherArgs(BaseModel):
    city: str = Field(min_length=1, description="城市名，中文，如 杭州")
    date: Literal["today", "tomorrow"] = Field(default="today", description="查询哪一天")


@tool(
    name="get_weather",
    description="查询中国城市今天/明天的天气（最高最低温、降雨概率）。需要城市名；查历史天气不要用它。",
    args_model=WeatherArgs,
    timeout=12.0,
)
async def get_weather(city: str, date: str = "today") -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        loc = _GEO_CACHE.get(city)
        if loc is None:
            coord = _BUILTIN_COORDS.get(city)
            if coord is not None:
                loc = {"latitude": coord[0], "longitude": coord[1], "name": city}
            else:
                geo = await client.get(
                    GEO_URL, params={"name": city, "count": 1, "language": "zh", "format": "json"}
                )
                geo.raise_for_status()
                places = geo.json().get("results") or []
                if not places:
                    # 查不到城市属业务结果：返回 success，让模型改说法而不是重试
                    return {"found": False, "message": f"未找到城市 {city}，请确认城市名"}
                loc = places[0]
            _GEO_CACHE[city] = loc

        resp = await client.get(
            FORECAST_URL,
            params={
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "Asia/Shanghai",
                "forecast_days": 2,
            },
        )
        resp.raise_for_status()
        daily = resp.json()["daily"]
        idx = 0 if date == "today" else 1
        return {
            "found": True,
            "city": loc["name"],
            "date": daily["time"][idx],
            "temperature_max": daily["temperature_2m_max"][idx],
            "temperature_min": daily["temperature_2m_min"][idx],
            "rain_probability": daily["precipitation_probability_max"][idx],
        }
