"""
server/config.py
================
配置加载模块。

职责：
1. 读取 config.yaml（以及可选的环境变量覆盖）。
2. 用 pydantic 做校验，给出明确的错误信息。
3. 暴露一个全局可用的 `Settings` 单例。

设计原则：
- 不硬编码任何密钥 / token，全部来自配置文件或环境变量。
- 所有可配置项都有合理默认值，保证开箱即用（只读监测，不交易）。
"""
from __future__ import annotations

import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# 配置数据模型（pydantic）
# 把 yaml 的松散结构校验成强类型对象，避免运行时 KeyError。
# ---------------------------------------------------------------------------

class PolymarketSettings(BaseModel):
    """Polymarket 公开接口地址。均为公开只读接口，无需任何认证。"""
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    # 公开成交流（activity）。若该地址变化，可在此处替换。
    ws_trades_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/activity"


class DiscoverySettings(BaseModel):
    """市场发现相关配置。"""
    # 命中任意一个关键词即视为候选世界杯市场（大小写不敏感）
    keywords: List[str] = Field(default_factory=lambda: [
        "FIFA World Cup", "World Cup", "世界杯",
        # 2026 主办国 / 常见球队名（可自行扩充）
        "USA Soccer", "Mexico Soccer", "Canada Soccer",
    ])
    # 额外的明确球队白名单（出现即命中）
    team_whitelist: List[str] = Field(default_factory=list)
    # 排除关键词：标题命中则丢弃（例如已结算、股票市场等噪声）
    exclude_keywords: List[str] = Field(default_factory=lambda: [
        "stock", "index", "crypto", "bitcoin",
    ])
    # 市场发现刷新间隔（秒）。Gamma 接口较重，默认 10 分钟一次。
    refresh_interval_sec: int = 600
    # 仅保留活跃市场（volume > 该值 或 active=true）
    min_liquidity_usd: float = 1000.0
    # 同时监测的最大市场数（防止过载）
    max_markets: int = 80


class DetectorSettings(BaseModel):
    """冷门 & 大额判断阈值。"""
    # 大额阈值（美元，USDC 口径）。达到才进入“候选提醒”流程。
    large_trade_usd: float = 5000.0
    important_trade_usd: float = 20000.0
    severe_trade_usd: float = 100000.0

    # 冷门评分阈值，达到才提醒
    underdog_score_threshold: float = 70.0

    # 隐含概率上限：当前 outcome 价格高于此值则不视为冷门方向
    underdog_max_implied_prob: float = 0.35

    # 短窗口统计（秒）
    short_window_sec: int = 300  # 5 分钟

    # 价格异动判定：短窗口内涨幅超过该比例视为异动
    price_spike_ratio: float = 0.05

    # 成交额异常倍数：当前成交额相对近 1h 中位数的倍数
    volume_anomaly_multiplier: float = 5.0

    # 排除噪声：盘口深度低于此值（美元）视为不可靠，忽略
    min_book_depth_usd: float = 500.0

    # 短窗口累计成交（美元）也视为一次“大额事件”
    short_window_large_usd: float = 5000.0


class ServerSettings(BaseModel):
    """服务器对外 WebSocket 推送服务配置。"""
    host: str = "0.0.0.0"
    port: int = 8765
    # 客户端连接必须携带此 token（通过 Authorization/X-Auth-Token 头或 URL query）
    auth_token: str = "change-me-please"
    # WS 心跳间隔（秒）
    ping_interval_sec: int = 20
    # --- TLS（强烈建议公网部署时启用）---
    # 同时填 cert 与 key 则启用 wss://；留空则用明文 ws://
    ssl_cert: str = ""   # PEM 证书路径，如 /etc/letsencrypt/live/yourdomain/fullchain.pem
    ssl_key: str = ""    # PEM 私钥路径，如 /etc/letsencrypt/live/yourdomain/privkey.pem
    # 单 IP 最大并发客户端连接数（抗 DoS，0 表示用代码内置默认 8）
    max_clients_per_ip: int = 8


class SecondaryNotifierSettings(BaseModel):
    """可选的二级通知（默认全部关闭）。"""
    enable_telegram: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    enable_email: bool = False
    email_smtp_host: str = ""
    email_smtp_port: int = 465
    email_username: str = ""
    email_password: str = ""
    email_from: str = ""
    email_to: str = ""


class Settings(BaseModel):
    """全局配置根对象。"""
    polymarket: PolymarketSettings = Field(default_factory=PolymarketSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    detector: DetectorSettings = Field(default_factory=DetectorSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    secondary: SecondaryNotifierSettings = Field(default_factory=SecondaryNotifierSettings)

    # SQLite 数据库路径（用于持久化提醒，防重启丢失）
    db_path: str = "data/alerts.db"
    # 日志级别
    log_level: str = "INFO"
    # 日志目录
    log_dir: str = "logs"

    @field_validator("log_level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        up = v.upper()
        if up not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got {v}")
        return up

    @model_validator(mode="after")
    def _check_token_strength(self) -> "Settings":
        """
        启动时校验 auth_token：禁止用占位符 / 弱 token 对外提供服务。
        除非绑定到回环地址（仅本机自测），否则弱 token 直接报错，强制用户改。
        """
        weak_tokens = {"change-me-please", "changeme", "secret",
                        "password", "token", ""}
        tok = self.server.auth_token.strip()
        is_loopback = self.server.host in ("127.0.0.1", "localhost")
        if tok in weak_tokens and not is_loopback:
            raise ValueError(
                "server.auth_token 仍是默认占位符或为空。当 host 不是 127.0.0.1 时，"
                "必须设置一个强随机 token（建议 `openssl rand -hex 24` 生成）。"
            )
        if tok not in weak_tokens and len(tok) < 16:
            raise ValueError(
                f"server.auth_token 长度仅 {len(tok)}，过短。建议至少 16 字符"
                "（hex 24 = 48 字符更安全）。"
            )
        return self


# ---------------------------------------------------------------------------
# 加载逻辑
# ---------------------------------------------------------------------------

# 环境变量前缀，例如 POLYALERT_SERVER__AUTH_TOKEN 会覆盖 server.auth_token
ENV_PREFIX = "POLYALERT_"

EDITABLE_SETTINGS: dict[str, set[str]] = {
    "detector": {
        "large_trade_usd",
        "important_trade_usd",
        "severe_trade_usd",
        "underdog_score_threshold",
        "underdog_max_implied_prob",
        "short_window_sec",
        "price_spike_ratio",
        "volume_anomaly_multiplier",
        "min_book_depth_usd",
        "short_window_large_usd",
    },
    "discovery": {
        "refresh_interval_sec",
        "min_liquidity_usd",
        "max_markets",
    },
}

INTEGER_EDITABLE_FIELDS = {
    ("detector", "short_window_sec"),
    ("discovery", "refresh_interval_sec"),
    ("discovery", "max_markets"),
}


def _apply_env_overrides(raw: dict) -> dict:
    """
    极简的环境变量覆盖：仅支持标量字段，键名形如
    POLYALERT_DETECTOR__LARGE_TRADE_USD。
    这是为了方便 Docker / systemd 注入敏感字段（如 token），不强制使用。
    """
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX):].lower().split("__")
        node = raw
        for p in path[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                break
        else:
            node[path[-1]] = value
    return raw


@lru_cache(maxsize=1)
def get_settings(config_path: Optional[str] = None) -> Settings:
    """
    读取并校验配置。结果会被缓存；测试时可调用 load_settings(force=True)。
    """
    path = Path(config_path or os.environ.get("POLYALERT_CONFIG", "config.yaml"))
    raw: dict = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    raw = _apply_env_overrides(raw)
    return Settings(**raw)


def load_settings(config_path: Optional[str] = None, force: bool = False) -> Settings:
    """显式加载（可强制绕过缓存，供测试使用）。"""
    if force:
        get_settings.cache_clear()
    return get_settings(config_path)


def editable_settings_payload(settings: Settings) -> dict[str, dict[str, Any]]:
    """Return only settings the Windows client is allowed to read/update."""
    payload: dict[str, dict[str, Any]] = {}
    for section, fields in EDITABLE_SETTINGS.items():
        obj = getattr(settings, section)
        payload[section] = {name: getattr(obj, name) for name in sorted(fields)}
    return payload


def editable_settings_schema() -> dict[str, dict[str, dict[str, Any]]]:
    """Small UI schema for the Windows client."""
    labels = {
        "large_trade_usd": "普通提醒成交额",
        "important_trade_usd": "重要提醒成交额",
        "severe_trade_usd": "严重提醒成交额",
        "underdog_score_threshold": "冷门评分阈值",
        "underdog_max_implied_prob": "冷门概率上限",
        "short_window_sec": "短窗口秒数",
        "price_spike_ratio": "价格异动比例",
        "volume_anomaly_multiplier": "成交额异常倍数",
        "min_book_depth_usd": "最低盘口深度",
        "short_window_large_usd": "短窗口累计成交额",
        "refresh_interval_sec": "市场刷新间隔",
        "min_liquidity_usd": "最低市场流动性",
        "max_markets": "最大监测市场数",
    }
    schema: dict[str, dict[str, dict[str, Any]]] = {}
    for section, fields in EDITABLE_SETTINGS.items():
        schema[section] = {}
        for field in sorted(fields):
            schema[section][field] = {
                "label": labels.get(field, field),
                "type": "int" if (section, field) in INTEGER_EDITABLE_FIELDS else "float",
            }
    return schema


def apply_editable_settings_update(settings: Settings, updates: dict,
                                   config_path: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """
    Validate, persist, and apply an authenticated client settings update.

    Only fields in EDITABLE_SETTINGS may be changed. Sensitive fields such as
    auth_token, URLs, and notification credentials are intentionally excluded.
    """
    path = Path(config_path or os.environ.get("POLYALERT_CONFIG", "config.yaml"))
    raw = settings.model_dump()
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or raw

    candidate = deepcopy(raw)
    for section, values in (updates or {}).items():
        if section not in EDITABLE_SETTINGS or not isinstance(values, dict):
            continue
        target = candidate.setdefault(section, {})
        for field, raw_value in values.items():
            if field not in EDITABLE_SETTINGS[section]:
                continue
            target[field] = _coerce_editable_value(section, field, raw_value)

    validated = Settings(**candidate)
    _validate_editable_settings(validated)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(candidate, f, allow_unicode=True, sort_keys=False)

    for section, fields in EDITABLE_SETTINGS.items():
        src = getattr(validated, section)
        dst = getattr(settings, section)
        for field in fields:
            setattr(dst, field, getattr(src, field))

    return editable_settings_payload(settings)


def _coerce_editable_value(section: str, field: str, value: Any) -> int | float:
    if (section, field) in INTEGER_EDITABLE_FIELDS:
        return int(float(value))
    return float(value)


def _validate_editable_settings(settings: Settings) -> None:
    d = settings.detector
    disc = settings.discovery
    if d.large_trade_usd <= 0 or d.important_trade_usd <= 0 or d.severe_trade_usd <= 0:
        raise ValueError("成交额阈值必须大于 0")
    if not (d.large_trade_usd <= d.important_trade_usd <= d.severe_trade_usd):
        raise ValueError("成交额阈值必须满足 普通 <= 重要 <= 严重")
    if not (0 <= d.underdog_score_threshold <= 100):
        raise ValueError("冷门评分阈值必须在 0-100 之间")
    if not (0 < d.underdog_max_implied_prob <= 1):
        raise ValueError("冷门概率上限必须在 0-1 之间")
    if d.short_window_sec <= 0 or d.price_spike_ratio <= 0:
        raise ValueError("短窗口秒数和价格异动比例必须大于 0")
    if d.volume_anomaly_multiplier <= 0:
        raise ValueError("成交额异常倍数必须大于 0")
    if d.min_book_depth_usd < 0 or d.short_window_large_usd <= 0:
        raise ValueError("盘口深度不能为负，短窗口累计成交额必须大于 0")
    if disc.refresh_interval_sec < 60:
        raise ValueError("市场刷新间隔不能低于 60 秒")
    if disc.min_liquidity_usd < 0:
        raise ValueError("最低市场流动性不能为负")
    if not (1 <= disc.max_markets <= 1000):
        raise ValueError("最大监测市场数必须在 1-1000 之间")
