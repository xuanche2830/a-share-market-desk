from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen

from analysis import PATTERN_LABELS, analyze_market_snapshot, analyze_security
from indicators import backtest_alert, enrich_bars, evaluate_alert
from providers import (DataSourceError, company_overview, diagnose_hithink, get_provider, market_breadth, market_overview,
                       normalize_symbol, provider_health, search_symbols, source_capabilities)
from windows_desktop import IS_WINDOWS, SingleInstance, TrayController, open_dashboard_address, show_message


SOURCE_ROOT = Path(__file__).resolve().parent.parent
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
APP_ROOT = Path(sys.executable).resolve().parent if FROZEN else SOURCE_ROOT
PORTABLE_MARKER = APP_ROOT / "portable.mode"
PORTABLE_MODE = FROZEN and PORTABLE_MARKER.is_file()
WEB_DIR = BUNDLE_ROOT / "web"
DATA_DIR = APP_ROOT / "data"
STATE_FILE = DATA_DIR / "state.json"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"
BACKUP_DIR = DATA_DIR / "backups"
TRADING_CALENDAR_FILE = (
    DATA_DIR / "trading_calendar.json"
    if (DATA_DIR / "trading_calendar.json").exists()
    else BUNDLE_ROOT / "data" / "trading_calendar.json"
)
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
STATE_LOCK = threading.RLock()
CACHE_LOCK = threading.Lock()
EVALUATION_LOCK = threading.Lock()
MONITOR_STATUS_LOCK = threading.Lock()
DESKTOP_STATUS_LOCK = threading.Lock()
NOTIFICATION_SENDER_LOCK = threading.Lock()
LOGGER = logging.getLogger("marketdesk")
STATE_VERSION = 4
MAX_BACKUPS = 20
MAX_AUDIT = 1000
STAT_KEYS = ("checks", "matched", "triggered", "skipped", "blocked", "errors")


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent another Windows process from silently sharing the dashboard port."""

    allow_reuse_address = not IS_WINDOWS

    def server_bind(self) -> None:
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


_DESKTOP_STATUS: dict[str, Any] = {
    "tray_running": False,
    "native_notifications": False,
    "tray_error": None,
    "single_instance": False,
    "windowed": FROZEN and IS_WINDOWS,
    "portable_mode": PORTABLE_MODE,
    "data_location": "application_folder",
    "log_file": "data/logs/app.log",
}
_NATIVE_NOTIFICATION_SENDER: Any = None

DEFAULT_STATE: dict[str, Any] = {
    "schema_version": STATE_VERSION,
    "settings": {
        "source": "auto", "refresh_seconds": 60, "auto_refresh": True,
        "quiet_hours_enabled": True, "quiet_start": "15:01", "quiet_end": "09:29",
    },
    "watchlist": [
        {"symbol": "600519.SH", "name": "贵州茅台"},
        {"symbol": "300750.SZ", "name": "宁德时代"},
        {"symbol": "002594.SZ", "name": "比亚迪"},
    ],
    "alerts": [],
    "records": [],
    "audit": [],
}


def _fresh_default() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_STATE, ensure_ascii=False))


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    if not FROZEN:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        LOGGER.addHandler(console_handler)


def _update_desktop_status(**changes: Any) -> None:
    with DESKTOP_STATUS_LOCK:
        _DESKTOP_STATUS.update(changes)


def desktop_status() -> dict[str, Any]:
    with DESKTOP_STATUS_LOCK:
        return dict(_DESKTOP_STATUS)


def set_native_notification_sender(sender: Any = None) -> None:
    global _NATIVE_NOTIFICATION_SENDER
    with NOTIFICATION_SENDER_LOCK:
        _NATIVE_NOTIFICATION_SENDER = sender


def test_notification_delivery() -> dict[str, Any]:
    with NOTIFICATION_SENDER_LOCK:
        sender = _NATIVE_NOTIFICATION_SENDER
    tested_at = datetime.now(SHANGHAI).isoformat()
    if sender is None:
        return {
            "delivered": False, "channel": "browser", "browser_fallback": True,
            "reason": "当前实例未启用 Windows 托盘通知，请在浏览器中授权通知或正常双击 EXE 启动",
            "tested_at": tested_at,
        }
    try:
        delivered = bool(sender("自主看盘台 · 通知测试", "测试成功：预警通知通道可以调用。"))
    except Exception as exc:
        LOGGER.exception("Windows 通知测试异常")
        return {
            "delivered": False, "channel": "windows", "browser_fallback": False,
            "reason": f"Windows 通知调用异常：{type(exc).__name__}", "tested_at": tested_at,
        }
    return {
        "delivered": delivered, "channel": "windows", "browser_fallback": False,
        "reason": None if delivered else "Windows Shell 未接受通知请求；请检查通知设置或安全软件",
        "tested_at": tested_at,
    }


def activate_tray(controller: TrayController) -> bool:
    if controller.start():
        _update_desktop_status(tray_running=True, native_notifications=True, tray_error=None)
        return True
    LOGGER.warning("系统托盘不可用，继续以浏览器模式运行：%s", controller.failed or "未知错误")
    _update_desktop_status(
        tray_running=False,
        native_notifications=False,
        tray_error="系统托盘创建失败，已切换为浏览器通知模式",
    )
    return False


def load_state_unlocked() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        state = sanitize_state_document(_fresh_default())
        save_state_unlocked(state)
        return state
    try:
        state = sanitize_state_document(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError, AttributeError):
        backup = STATE_FILE.with_suffix(f".broken-{int(time.time())}.json")
        STATE_FILE.replace(backup)
        state = sanitize_state_document(_fresh_default())
        save_state_unlocked(state)
    return state


def load_state() -> dict[str, Any]:
    with STATE_LOCK:
        return load_state_unlocked()


def create_backup_unlocked(reason: str = "auto") -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(SHANGHAI)
    safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "-", reason).strip("-")[:24] or "auto"
    target = BACKUP_DIR / f"state-{now.strftime('%Y%m%d-%H%M%S-%f')}-{safe_reason}.json"
    target.write_bytes(STATE_FILE.read_bytes())
    backups = sorted(BACKUP_DIR.glob("state-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in backups[MAX_BACKUPS:]:
        stale.unlink(missing_ok=True)
    return {"name": target.name, "created_at": now.isoformat(), "reason": reason, "size": target.stat().st_size}


def list_backups() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not BACKUP_DIR.exists():
        return items
    for path in sorted(BACKUP_DIR.glob("state-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            items.append({
                "name": path.name, "created_at": datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI).isoformat(),
                "size": path.stat().st_size, "watchlist_count": len(payload.get("watchlist", [])),
                "alert_count": len(payload.get("alerts", [])), "record_count": len(payload.get("records", [])),
            })
        except (OSError, json.JSONDecodeError, AttributeError):
            items.append({"name": path.name, "created_at": None, "size": path.stat().st_size, "invalid": True})
    return items


def backup_path(name: str) -> Path:
    if Path(name).name != name or not re.fullmatch(r"state-\d{8}-\d{6}-\d{6}-[a-zA-Z0-9_-]+\.json", name):
        raise ValueError("备份文件名无效")
    path = BACKUP_DIR / name
    if not path.is_file():
        raise ValueError("未找到备份文件")
    return path


def save_state_unlocked(state: dict[str, Any], backup_reason: str | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if backup_reason:
        create_backup_unlocked(backup_reason)
    state["schema_version"] = STATE_VERSION
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def save_state(state: dict[str, Any], backup_reason: str | None = None) -> None:
    with STATE_LOCK:
        save_state_unlocked(state, backup_reason)


def update_state(operation: Any, backup_reason: str | None = None) -> tuple[dict[str, Any], Any]:
    """Apply one short state mutation without losing concurrent updates."""
    with STATE_LOCK:
        state = load_state_unlocked()
        result = operation(state)
        save_state_unlocked(state, backup_reason)
        return state, result


def sanitize_state_document(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("导入文件必须是 JSON 对象")
    if isinstance(payload.get("state"), dict):
        payload = payload["state"]
    version = int(payload.get("schema_version", 1))
    if version > STATE_VERSION:
        raise ValueError(f"导入文件版本 {version} 高于当前支持版本 {STATE_VERSION}")
    result = _fresh_default()
    settings = payload.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError("settings 字段格式错误")
    def boolean(mapping: dict[str, Any], key: str, default: bool) -> bool:
        value = mapping.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        return value

    def optional_number(value: Any, label: str) -> float | int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{label} 必须是有限数字或 null")
        return value

    result["settings"].update({
        "source": settings.get("source") if settings.get("source") in {"auto", "eastmoney", "hithink"} else "auto",
        "refresh_seconds": max(10, min(3600, int(settings.get("refresh_seconds", 60)))),
        "auto_refresh": boolean(settings, "auto_refresh", True),
        "quiet_hours_enabled": boolean(settings, "quiet_hours_enabled", True),
        "quiet_start": str(settings.get("quiet_start", "15:01")),
        "quiet_end": str(settings.get("quiet_end", "09:29")),
    })
    _clock_minutes(result["settings"]["quiet_start"])
    _clock_minutes(result["settings"]["quiet_end"])

    watchlist = payload.get("watchlist") or []
    if not isinstance(watchlist, list) or len(watchlist) > 200:
        raise ValueError("自选股列表格式错误或超过 200 只")
    result["watchlist"] = []
    seen_symbols: set[str] = set()
    for index, item in enumerate(watchlist):
        if not isinstance(item, dict):
            raise ValueError("自选股条目格式错误")
        symbol = normalize_symbol(str(item.get("symbol", "")))
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        result["watchlist"].append({
            "symbol": symbol, "name": str(item.get("name") or symbol)[:40],
            "group": str(item.get("group") or "默认")[:30], "note": str(item.get("note") or "")[:200],
            "position": index,
        })

    alerts = payload.get("alerts") or []
    if not isinstance(alerts, list) or len(alerts) > 500:
        raise ValueError("预警规则格式错误或超过 500 条")
    result["alerts"] = []
    seen_alert_ids: set[str] = set()
    for item in alerts:
        if not isinstance(item, dict):
            raise ValueError("预警规则条目格式错误")
        clean = validate_alert(item)
        alert_id = str(item.get("id") or uuid.uuid4())[:100]
        if alert_id in seen_alert_ids:
            alert_id = str(uuid.uuid4())
        seen_alert_ids.add(alert_id)
        raw_stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        clean.update({
            "id": alert_id, "enabled": boolean(item, "enabled", True),
            "once_per_day": boolean(item, "once_per_day", True),
            "cooldown_minutes": max(1, min(1440, int(item.get("cooldown_minutes", 60)))),
            "trading_hours_only": boolean(item, "trading_hours_only", True),
            "last_active": boolean(item, "last_active", False),
            "stats": {
                key: max(0, int(raw_stats.get(key, 0)))
                for key in STAT_KEYS
            },
        })
        for key in ("last_checked_at", "last_message", "last_triggered_at"):
            if item.get(key) is not None:
                clean[key] = str(item[key])[:500]
        result["alerts"].append(clean)

    records = payload.get("records") or []
    if not isinstance(records, list):
        raise ValueError("触发记录格式错误")
    result["records"] = []
    seen_record_ids: set[str] = set()
    for item in records[:200]:
        if not isinstance(item, dict):
            continue
        try:
            symbol = normalize_symbol(str(item.get("symbol", "")))
        except ValueError:
            continue
        record_id = str(item.get("id") or uuid.uuid4())[:100]
        if record_id in seen_record_ids:
            record_id = str(uuid.uuid4())
        seen_record_ids.add(record_id)
        result["records"].append({
            "id": record_id, "rule_id": str(item.get("rule_id") or "")[:100],
            "symbol": symbol, "name": str(item.get("name") or symbol)[:40], "type": str(item.get("type") or "")[:40],
            "message": str(item.get("message") or "")[:500],
            "value": optional_number(item.get("value"), "record.value"),
            "threshold": optional_number(item.get("threshold"), "record.threshold"),
            "triggered_at": str(item.get("triggered_at") or "")[:80],
            "data_timestamp": optional_number(item.get("data_timestamp"), "record.data_timestamp"),
            "source": str(item.get("source") or "")[:40],
            "notification_suppressed": boolean(item, "notification_suppressed", False),
            "notification_reason": str(item.get("notification_reason") or "")[:80] or None,
        })
    audit = payload.get("audit") or []
    if not isinstance(audit, list):
        raise ValueError("审计记录格式错误")
    result["audit"] = []
    for item in audit[:MAX_AUDIT]:
        if not isinstance(item, dict):
            continue
        try:
            symbol = normalize_symbol(str(item.get("symbol", "")))
        except ValueError:
            continue
        result["audit"].append({
            "id": str(item.get("id") or uuid.uuid4())[:100],
            "rule_id": str(item.get("rule_id") or "")[:100], "symbol": symbol,
            "type": str(item.get("type") or "")[:40], "checked_at": str(item.get("checked_at") or "")[:80],
            "status": str(item.get("status") or "")[:40], "message": str(item.get("message") or "")[:500],
            "value": optional_number(item.get("value"), "audit.value"),
            "threshold": optional_number(item.get("threshold"), "audit.threshold"),
            "data_timestamp": item.get("data_timestamp"), "source": str(item.get("source") or "")[:40],
        })
    return result


def load_trading_calendar() -> dict[str, Any]:
    try:
        payload = json.loads(TRADING_CALENDAR_FILE.read_text(encoding="utf-8"))
        closures = payload.get("closures")
        years = payload.get("covered_years")
        if not isinstance(closures, list) or not isinstance(years, list):
            raise ValueError("交易日历字段不完整")
        return payload
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return {"covered_years": [], "closures": [], "source": None, "source_url": None, "updated_at": None}


def market_session(now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI)
    minute = current.hour * 60 + current.minute
    calendar = load_trading_calendar()
    closure_map = {
        item.get("date"): item.get("name")
        for item in calendar.get("closures", [])
        if isinstance(item, dict) and isinstance(item.get("date"), str)
    }
    day = current.strftime("%Y-%m-%d")
    covered = current.year in calendar.get("covered_years", [])
    basis = (
        f"上交所官方休市安排（更新于 {calendar.get('updated_at')}）"
        if covered else "工作日与北京时间估算；当前年份缺少本地官方休市表"
    )
    if day in closure_map:
        status, label = "closed", f"休市（{closure_map[day]}）"
    elif current.weekday() >= 5:
        status, label = "closed", "休市（周末）"
    elif 9 * 60 + 15 <= minute < 9 * 60 + 30:
        status, label = "preopen", "集合竞价时段"
    elif 9 * 60 + 30 <= minute <= 11 * 60 + 30 or 13 * 60 <= minute <= 15 * 60:
        status, label = "trading", "交易时段"
    elif 11 * 60 + 30 < minute < 13 * 60:
        status, label = "break", "午间休市"
    else:
        status, label = "closed", "非交易时段"
    return {
        "status": status, "label": label, "basis": basis,
        "calendar_covered": covered, "calendar_source": calendar.get("source"),
        "calendar_source_url": calendar.get("source_url"),
    }


def _cache_file(kind: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{kind}-{digest}.json"


def _write_cache(kind: str, key: str, items: list[dict[str, Any]]) -> str | None:
    cached_at = datetime.now(SHANGHAI).isoformat()
    payload = {"cached_at": cached_at, "items": items}
    target = _cache_file(kind, key)
    try:
        with CACHE_LOCK:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, target)
    except OSError:
        return None
    return cached_at


def _read_cache(kind: str, key: str) -> tuple[list[dict[str, Any]], str] | None:
    target = _cache_file(kind, key)
    try:
        with CACHE_LOCK:
            payload = json.loads(target.read_text(encoding="utf-8"))
        items = payload.get("items")
        cached_at = payload.get("cached_at")
        if not isinstance(items, list) or not isinstance(cached_at, str):
            return None
        return items, cached_at
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def fetch_with_cache(
    kind: str, key: str, operation: Any,
) -> tuple[list[dict[str, Any]], str, str | None, str | None]:
    try:
        items = operation()
        _write_cache(kind, key, items)
        return items, "live", None, None
    except DataSourceError as exc:
        cached = _read_cache(kind, key)
        if cached is None:
            raise
        items, cached_at = cached
        return items, "cached", str(exc), cached_at


def completed_daily_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(SHANGHAI)
    if not bars:
        return bars
    today = now.strftime("%Y-%m-%d")
    if bars[-1].get("date") == today and (now.hour < 15 or (now.hour == 15 and now.minute < 5)):
        return bars[:-1]
    return bars


def _rule_signature(rule: dict[str, Any]) -> tuple[Any, ...]:
    return (
        rule.get("symbol"), rule.get("type"),
        json.dumps(rule.get("params") or {}, ensure_ascii=False, sort_keys=True),
    )


def _clock_minutes(value: str) -> int:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("安静时段必须使用 HH:MM 格式")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("安静时段必须是有效时间")
    return hour * 60 + minute


def is_quiet_time(settings: dict[str, Any], now: datetime | None = None) -> bool:
    if not settings.get("quiet_hours_enabled", True):
        return False
    current = now or datetime.now(SHANGHAI)
    minute = current.hour * 60 + current.minute
    start = _clock_minutes(str(settings.get("quiet_start", "15:01")))
    end = _clock_minutes(str(settings.get("quiet_end", "09:29")))
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def evaluate_rule_now(rule: dict[str, Any], provider: Any) -> tuple[Any, dict[str, Any]]:
    symbol = rule["symbol"]
    quotes = provider.quotes([symbol])
    quote = next((item for item in quotes if item.get("symbol") == symbol), None)
    bars: list[dict[str, Any]] = []
    if rule["type"] not in {"price_above", "price_below"}:
        bars = completed_daily_bars(provider.history(symbol, 260))
    result = evaluate_alert(rule, quote, bars, False)
    return result, quote or {}


def filter_records(records: list[dict[str, Any]], query: dict[str, list[str]]) -> list[dict[str, Any]]:
    symbol = query.get("symbol", [""])[0]
    kind = query.get("type", [""])[0]
    day = query.get("date", [""])[0]
    return [
        item for item in records
        if (not symbol or item.get("symbol") == symbol)
        and (not kind or item.get("type") == kind)
        and (not day or str(item.get("triggered_at") or "")[:10] == day)
    ]


def append_audit(
    state: dict[str, Any], rule: dict[str, Any], checked_at: datetime, status: str, message: str,
    *, value: Any = None, threshold: Any = None, data_timestamp: Any = None, source: str = "",
) -> None:
    state.setdefault("audit", []).insert(0, {
        "id": str(uuid.uuid4()), "rule_id": rule.get("id"), "symbol": rule.get("symbol"),
        "type": rule.get("type"), "checked_at": checked_at.isoformat(), "status": status,
        "message": message, "value": value, "threshold": threshold,
        "data_timestamp": data_timestamp, "source": source,
    })
    state["audit"] = state["audit"][:MAX_AUDIT]


def filtered_audit(state: dict[str, Any], query: dict[str, list[str]]) -> list[dict[str, Any]]:
    rule_id = query.get("rule_id", [""])[0]
    symbol = query.get("symbol", [""])[0]
    status = query.get("status", [""])[0]
    limit = max(1, min(200, int(query.get("limit", ["100"])[0])))
    return [
        item for item in state.get("audit", [])
        if (not rule_id or item.get("rule_id") == rule_id)
        and (not symbol or item.get("symbol") == symbol)
        and (not status or item.get("status") == status)
    ][:limit]


def run_rule_backtest(rule: dict[str, Any], provider: Any, limit: int = 750) -> dict[str, Any]:
    bars = completed_daily_bars(provider.history(rule["symbol"], max(60, min(1000, limit))))
    result = backtest_alert(rule, bars)
    result.update({
        "rule_id": rule["id"], "symbol": rule["symbol"], "type": rule["type"],
        "period": {"start": bars[0].get("date") if bars else None, "end": bars[-1].get("date") if bars else None},
        "sources": sorted({str(item.get("source")) for item in bars if item.get("source")}),
        "events": result["events"][-50:][::-1],
    })
    return result


def records_csv(records: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["股票代码", "股票名称", "规则类型", "触发说明", "触发时间", "数据时间", "数据源", "通知状态"])
    labels = {
        "price_above": "价格高于", "price_below": "价格低于", "ma_cross": "均线穿越",
        "breakout": "区间突破", "volume_surge": "成交量放大", "rsi_threshold": "RSI 区域",
        "macd_cross": "MACD 金叉/死叉", "breakout_volume": "突破并放量",
        "trend_state": "趋势状态", "candlestick_pattern": "K线形态",
    }

    def safe(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    for item in records:
        writer.writerow([safe(item.get("symbol")), safe(item.get("name")), labels.get(item.get("type"), item.get("type")),
                         safe(item.get("message")), item.get("triggered_at"), item.get("data_timestamp"), item.get("source"),
                         f"已静默：{item.get('notification_reason') or '安静时段'}" if item.get("notification_suppressed") else "已通知"])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def evaluate_all(now: datetime | None = None) -> dict[str, Any]:
    """Fetch once, then atomically apply rule results to the latest state."""
    with EVALUATION_LOCK:
        snapshot = load_state()
        enabled = [rule for rule in snapshot["alerts"] if rule.get("enabled", True)]
        if not enabled:
            return {
                "triggered": [], "records": snapshot["records"], "alerts": snapshot["alerts"],
                "skipped": False, "idle": True,
            }

        checked_at = now or datetime.now(SHANGHAI)
        session = market_session(checked_at)
        eligible = [
            rule for rule in enabled
            if not rule.get("trading_hours_only", True) or session["status"] == "trading"
        ]
        skipped_ids = {rule["id"] for rule in enabled if rule not in eligible}
        provider = get_provider(snapshot["settings"].get("source", "auto")) if eligible else None
        provider_name = provider.name if provider else "scheduler"
        symbols = sorted({rule["symbol"] for rule in eligible})
        try:
            quotes = {item["symbol"]: item for item in provider.quotes(symbols)} if provider and symbols else {}
            histories: dict[str, list[dict[str, Any]]] = {}
            for rule in eligible:
                symbol = rule["symbol"]
                if rule["type"] not in {"price_above", "price_below"} and symbol not in histories:
                    histories[symbol] = completed_daily_bars(provider.history(symbol, 260))
        except DataSourceError as exc:
            eligible_ids = {rule["id"] for rule in eligible}
            def mark_error(state: dict[str, Any]) -> None:
                for rule in state["alerts"]:
                    if rule.get("id") not in eligible_ids or not rule.get("enabled", True):
                        continue
                    stats = rule.setdefault("stats", {key: 0 for key in STAT_KEYS})
                    stats["checks"] += 1
                    stats["errors"] += 1
                    rule["last_checked_at"] = checked_at.isoformat()
                    rule["last_message"] = f"数据源失败：{exc}"
                    append_audit(state, rule, checked_at, "source_error", rule["last_message"], source=provider_name)
            update_state(mark_error)
            raise

        snapshot_rules = {rule["id"]: rule for rule in enabled}
        quiet = is_quiet_time(snapshot["settings"], checked_at)

        def apply_results(state: dict[str, Any]) -> list[dict[str, Any]]:
            triggered: list[dict[str, Any]] = []
            watch_names = {item["symbol"]: item.get("name") for item in state["watchlist"]}
            for rule in state["alerts"]:
                original = snapshot_rules.get(rule.get("id"))
                if not original or not rule.get("enabled", True) or _rule_signature(rule) != _rule_signature(original):
                    continue
                if rule.get("id") in skipped_ids:
                    stats = rule.setdefault("stats", {key: 0 for key in STAT_KEYS})
                    stats["checks"] += 1
                    stats["skipped"] += 1
                    rule["last_checked_at"] = checked_at.isoformat()
                    rule["last_message"] = f"已跳过：当前为{session['label']}，规则仅在交易时段触发"
                    append_audit(state, rule, checked_at, "skipped_session", rule["last_message"], source=provider_name)
                    continue
                symbol = rule["symbol"]
                stats = rule.setdefault("stats", {key: 0 for key in STAT_KEYS})
                stats["checks"] += 1
                result = evaluate_alert(
                    rule, quotes.get(symbol), histories.get(symbol, []), bool(rule.get("last_active")),
                )
                if result.active:
                    stats["matched"] += 1
                rule["last_active"] = result.active
                rule["last_checked_at"] = checked_at.isoformat()
                rule["last_message"] = result.message
                if not result.triggered:
                    quote = quotes.get(symbol) or {}
                    append_audit(
                        state, rule, checked_at, "condition_met" if result.active else "not_met", result.message,
                        value=result.value, threshold=result.threshold, data_timestamp=quote.get("timestamp"),
                        source=quote.get("source") or provider_name,
                    )
                    continue

                last_triggered = rule.get("last_triggered_at")
                if last_triggered:
                    last_time = datetime.fromisoformat(last_triggered)
                    elapsed_minutes = (checked_at - last_time).total_seconds() / 60
                    if rule.get("once_per_day", True) and last_time.date() == checked_at.date():
                        stats["blocked"] += 1
                        append_audit(state, rule, checked_at, "blocked_daily", f"条件命中，但每日一次限制仍生效：{result.message}", value=result.value, threshold=result.threshold, source=provider_name)
                        continue
                    if elapsed_minutes < int(rule.get("cooldown_minutes", 60)):
                        stats["blocked"] += 1
                        append_audit(state, rule, checked_at, "blocked_cooldown", f"条件命中，但仍在冷却期：{result.message}", value=result.value, threshold=result.threshold, source=provider_name)
                        continue

                quote = quotes.get(symbol) or {}
                record = {
                    "id": str(uuid.uuid4()), "rule_id": rule["id"], "symbol": symbol,
                    "name": quote.get("name") or watch_names.get(symbol) or symbol,
                    "type": rule["type"], "message": result.message, "value": result.value,
                    "threshold": result.threshold, "triggered_at": checked_at.isoformat(),
                    "data_timestamp": quote.get("timestamp"), "source": quote.get("source") or provider_name,
                    "notification_suppressed": quiet,
                    "notification_reason": "安静时段" if quiet else None,
                }
                rule["last_triggered_at"] = checked_at.isoformat()
                stats["triggered"] += 1
                state["records"].insert(0, record)
                append_audit(
                    state, rule, checked_at, "triggered_silent" if quiet else "triggered", result.message,
                    value=result.value, threshold=result.threshold, data_timestamp=quote.get("timestamp"),
                    source=quote.get("source") or provider_name,
                )
                triggered.append(record)
            state["records"] = state["records"][:200]
            return triggered

        latest, triggered = update_state(apply_results)
        return {
            "triggered": triggered, "records": latest["records"], "alerts": latest["alerts"],
            "skipped": not eligible, "skip_reason": session["label"] if not eligible else None, "idle": False,
        }


_MONITOR_STATUS: dict[str, Any] = {
    "running": False, "started_at": None, "last_attempt_at": None,
    "last_success_at": None, "last_error": None, "last_trigger_count": 0,
    "last_cycle": "waiting", "next_check_at": None, "paused": False,
}


def _update_monitor_status(**changes: Any) -> None:
    with MONITOR_STATUS_LOCK:
        _MONITOR_STATUS.update(changes)


def monitor_status() -> dict[str, Any]:
    with MONITOR_STATUS_LOCK:
        return dict(_MONITOR_STATUS)


def monitor_interval() -> int:
    state = load_state()
    configured = max(10, min(3600, int(state["settings"].get("refresh_seconds", 60))))
    session = market_session()["status"]
    if not any(rule.get("enabled", True) for rule in state["alerts"]):
        return 60
    if session in {"trading", "preopen"}:
        return configured
    if session == "break":
        return max(60, configured)
    return max(300, configured)


class AlertMonitor:
    def __init__(self, on_triggered: Any = None) -> None:
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.pause_lock = threading.Lock()
        self.paused = False
        self.on_triggered = on_triggered
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.wake_event.clear()
        self.thread = threading.Thread(target=self._run, name="alert-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        _update_monitor_status(running=False, next_check_at=None)

    def is_paused(self) -> bool:
        with self.pause_lock:
            return self.paused

    def set_paused(self, paused: bool) -> bool:
        with self.pause_lock:
            self.paused = bool(paused)
            current = self.paused
        _update_monitor_status(paused=current, last_cycle="paused" if current else "waiting", next_check_at=None)
        self.wake_event.set()
        LOGGER.info("后台预警%s", "已暂停" if current else "已恢复")
        return current

    def toggle_paused(self) -> bool:
        return self.set_paused(not self.is_paused())

    def _wait(self, seconds: int) -> None:
        self.wake_event.wait(seconds)
        self.wake_event.clear()

    def _run(self) -> None:
        _update_monitor_status(
            running=True, paused=self.is_paused(), started_at=datetime.now(SHANGHAI).isoformat(),
        )
        while not self.stop_event.is_set():
            if self.is_paused():
                _update_monitor_status(last_cycle="paused", next_check_at=None, interval_seconds=None)
                self._wait(60)
                continue
            attempted_at = datetime.now(SHANGHAI)
            _update_monitor_status(last_attempt_at=attempted_at.isoformat())
            try:
                result = evaluate_all()
                cycle = "idle" if result.get("idle") else "success"
                _update_monitor_status(
                    last_success_at=datetime.now(SHANGHAI).isoformat(), last_error=None,
                    last_trigger_count=len(result["triggered"]), last_cycle=cycle,
                )
                if result["triggered"]:
                    LOGGER.info("预警触发 %d 条", len(result["triggered"]))
                    if self.on_triggered:
                        try:
                            audible = [item for item in result["triggered"] if not item.get("notification_suppressed")]
                            if audible:
                                self.on_triggered(audible)
                        except Exception:
                            LOGGER.exception("发送原生通知失败")
            except DataSourceError as exc:
                _update_monitor_status(last_error=str(exc), last_trigger_count=0, last_cycle="source_error")
                LOGGER.warning("后台预警数据源异常：%s", exc)
            except Exception as exc:
                _update_monitor_status(last_error=f"后台监控异常：{exc}", last_trigger_count=0, last_cycle="error")
                LOGGER.exception("后台预警异常")

            interval = monitor_interval()
            next_check = datetime.now(SHANGHAI) + timedelta(seconds=interval)
            _update_monitor_status(next_check_at=next_check.isoformat(), interval_seconds=interval)
            self._wait(interval)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalMarketDesk/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.debug("%s %s", self.address_string(), fmt % args)

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_download(self, body: bytes, content_type: str, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def handle_api_error(self, exc: Exception) -> None:
        status = HTTPStatus.BAD_REQUEST if isinstance(exc, (ValueError, KeyError)) else HTTPStatus.BAD_GATEWAY
        LOGGER.warning("API %s 失败：%s", self.path, exc)
        self.send_json({"ok": False, "error": str(exc)}, status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            try:
                self.handle_get_api(parsed.path, parse_qs(parsed.query))
            except Exception as exc:
                self.handle_api_error(exc)
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/watchlist/bulk":
                raw_items = payload.get("items")
                if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 50:
                    raise ValueError("批量导入需要提供 1 到 50 个股票代码")
                symbols = list(dict.fromkeys(normalize_symbol(str(item)) for item in raw_items))
                state = load_state()
                provider = get_provider(state["settings"].get("source", "auto"))
                quotes = {item["symbol"]: item for item in provider.quotes(symbols)}
                group = str(payload.get("group") or "默认")[:30]
                def add_bulk(latest: dict[str, Any]) -> list[dict[str, Any]]:
                    existing = {item["symbol"] for item in latest["watchlist"]}
                    added = []
                    for symbol in symbols:
                        if len(latest["watchlist"]) >= 200:
                            break
                        if symbol in existing:
                            continue
                        quote = quotes.get(symbol)
                        if not quote:
                            continue
                        item = {"symbol": symbol, "name": quote.get("name") or symbol, "group": group, "note": "", "position": len(latest["watchlist"])}
                        latest["watchlist"].append(item)
                        added.append(item)
                    return added
                _, added = update_state(add_bulk, "watchlist-bulk")
                self.send_json({"ok": True, "items": added, "requested": len(symbols)}, HTTPStatus.CREATED)
            elif parsed.path == "/api/watchlist":
                state = load_state()
                symbol = normalize_symbol(str(payload["symbol"]))
                provider = get_provider(state["settings"].get("source", "auto"))
                quote = provider.quotes([symbol])[0]
                def add_watchlist(latest: dict[str, Any]) -> None:
                    if not any(item["symbol"] == symbol for item in latest["watchlist"]):
                        latest["watchlist"].append({"symbol": symbol, "name": quote.get("name") or symbol, "group": "默认", "note": "", "position": len(latest["watchlist"])})
                update_state(add_watchlist, "watchlist-add")
                self.send_json({"ok": True, "item": {"symbol": symbol, "name": quote.get("name") or symbol}})
            elif parsed.path == "/api/alerts":
                alert = validate_alert(payload)
                alert.update({"id": str(uuid.uuid4()), "enabled": True, "once_per_day": bool(payload.get("once_per_day", True)),
                              "cooldown_minutes": max(1, int(payload.get("cooldown_minutes", 60))),
                              "trading_hours_only": bool(payload.get("trading_hours_only", True)), "last_active": False,
                              "stats": {key: 0 for key in STAT_KEYS}})
                update_state(lambda state: state["alerts"].append(alert), "alert-add")
                self.send_json({"ok": True, "item": alert}, HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/alerts/") and parsed.path.endswith("/clone"):
                alert_id = unquote(parsed.path.split("/")[-2])
                def clone_alert(state: dict[str, Any]) -> dict[str, Any]:
                    source = next((item for item in state["alerts"] if item.get("id") == alert_id), None)
                    if not source:
                        raise ValueError("未找到预警规则")
                    clone = {
                        "symbol": source["symbol"], "type": source["type"], "params": dict(source.get("params") or {}),
                        "id": str(uuid.uuid4()), "enabled": False, "once_per_day": bool(source.get("once_per_day", True)),
                        "cooldown_minutes": int(source.get("cooldown_minutes", 60)),
                        "trading_hours_only": bool(source.get("trading_hours_only", True)), "last_active": False,
                        "stats": {key: 0 for key in STAT_KEYS},
                    }
                    state["alerts"].append(clone)
                    return clone
                _, clone = update_state(clone_alert, "alert-clone")
                self.send_json({"ok": True, "item": clone}, HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/alerts/") and parsed.path.endswith("/test"):
                alert_id = unquote(parsed.path.split("/")[-2])
                state = load_state()
                rule = next((item for item in state["alerts"] if item.get("id") == alert_id), None)
                if not rule:
                    raise ValueError("未找到预警规则")
                provider = get_provider(state["settings"].get("source", "auto"))
                result, quote = evaluate_rule_now(rule, provider)
                self.send_json({
                    "ok": True, "matched": result.active, "would_trigger": result.triggered,
                    "message": result.message, "value": result.value, "threshold": result.threshold,
                    "checked_at": datetime.now(SHANGHAI).isoformat(),
                    "data_timestamp": quote.get("timestamp"), "source": quote.get("source") or provider.name,
                    "dry_run": True,
                })
            elif parsed.path.startswith("/api/alerts/") and parsed.path.endswith("/backtest"):
                alert_id = unquote(parsed.path.split("/")[-2])
                state = load_state()
                rule = next((item for item in state["alerts"] if item.get("id") == alert_id), None)
                if not rule:
                    raise ValueError("未找到预警规则")
                provider = get_provider(state["settings"].get("source", "auto"))
                result = run_rule_backtest(rule, provider, int(payload.get("limit", 750)))
                self.send_json({"ok": True, **result})
            elif parsed.path == "/api/backups":
                with STATE_LOCK:
                    item = create_backup_unlocked("manual")
                self.send_json({"ok": True, "item": item}, HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/backups/") and parsed.path.endswith("/restore"):
                name = unquote(parsed.path.split("/")[-2])
                restored = sanitize_state_document(json.loads(backup_path(name).read_text(encoding="utf-8")))
                save_state(restored, "before-restore")
                self.send_json({"ok": True, "state": restored})
            elif parsed.path == "/api/import":
                imported = sanitize_state_document(payload)
                save_state(imported, "before-import")
                self.send_json({"ok": True, "state": imported})
            elif parsed.path == "/api/source-test":
                state = load_state()
                preference = str(payload.get("source") or state["settings"].get("source", "auto"))
                provider = get_provider(preference)
                default_symbol = state["watchlist"][0]["symbol"] if state["watchlist"] else "600519.SH"
                symbol = normalize_symbol(str(payload.get("symbol") or default_symbol))
                started = time.perf_counter()
                quotes = provider.quotes([symbol])
                bars = provider.history(symbol, 10)
                elapsed = round((time.perf_counter() - started) * 1000)
                self.send_json({
                    "ok": True, "provider": provider.name, "symbol": symbol, "latency_ms": elapsed,
                    "quote_count": len(quotes), "bar_count": len(bars),
                    "sources": sorted({item.get("source") for item in quotes + bars if item.get("source")}),
                    "tested_at": datetime.now(SHANGHAI).isoformat(),
                })
            elif parsed.path == "/api/hithink-test":
                state = load_state()
                default_symbol = state["watchlist"][0]["symbol"] if state["watchlist"] else "600519.SH"
                symbol = normalize_symbol(str(payload.get("symbol") or default_symbol))
                self.send_json({"ok": True, **diagnose_hithink(symbol), "tested_at": datetime.now(SHANGHAI).isoformat()})
            elif parsed.path == "/api/notification-test":
                self.send_json({"ok": True, **test_notification_delivery()})
            elif parsed.path == "/api/evaluate":
                try:
                    result = evaluate_all()
                    self.send_json({"ok": True, **result})
                except DataSourceError as exc:
                    state = load_state()
                    self.send_json({
                        "ok": True, "triggered": [], "records": state["records"], "alerts": state["alerts"],
                        "skipped": True, "warning": f"实时数据不可用，本轮预警未执行：{exc}",
                    })
            else:
                self.send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.handle_api_error(exc)

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/settings":
                def change_settings(state: dict[str, Any]) -> None:
                    if "refresh_seconds" in payload:
                        state["settings"]["refresh_seconds"] = max(10, min(3600, int(payload["refresh_seconds"])))
                    if "auto_refresh" in payload:
                        state["settings"]["auto_refresh"] = bool(payload["auto_refresh"])
                    if "source" in payload and payload["source"] in {"auto", "eastmoney", "hithink"}:
                        state["settings"]["source"] = payload["source"]
                    if "quiet_hours_enabled" in payload:
                        state["settings"]["quiet_hours_enabled"] = bool(payload["quiet_hours_enabled"])
                    for key in ("quiet_start", "quiet_end"):
                        if key in payload:
                            value = str(payload[key])
                            _clock_minutes(value)
                            state["settings"][key] = value
                state, _ = update_state(change_settings, "settings-change")
                self.send_json({"ok": True, "settings": state["settings"]})
            elif parsed.path.startswith("/api/watchlist/"):
                symbol = normalize_symbol(unquote(parsed.path.rsplit("/", 1)[-1]))
                def change_watchlist(state: dict[str, Any]) -> dict[str, Any]:
                    item = next((entry for entry in state["watchlist"] if entry["symbol"] == symbol), None)
                    if not item:
                        raise ValueError("未找到自选股")
                    if "group" in payload:
                        item["group"] = str(payload["group"] or "默认")[:30]
                    if "note" in payload:
                        item["note"] = str(payload["note"] or "")[:200]
                    return item
                _, item = update_state(change_watchlist, "watchlist-edit")
                self.send_json({"ok": True, "item": item})
            elif parsed.path.startswith("/api/alerts/"):
                alert_id = unquote(parsed.path.rsplit("/", 1)[-1])
                def change_alert(state: dict[str, Any]) -> dict[str, Any]:
                    rule = next((item for item in state["alerts"] if item["id"] == alert_id), None)
                    if not rule:
                        raise ValueError("未找到预警规则")
                    for key in ("enabled", "once_per_day", "trading_hours_only"):
                        if key in payload:
                            rule[key] = bool(payload[key])
                    if "cooldown_minutes" in payload:
                        rule["cooldown_minutes"] = max(1, min(1440, int(payload["cooldown_minutes"])))
                    if any(key in payload for key in ("symbol", "type", "params")):
                        validated = validate_alert({
                            "symbol": payload.get("symbol", rule["symbol"]), "type": payload.get("type", rule["type"]),
                            "params": payload.get("params", rule.get("params") or {}),
                        })
                        rule.update(validated)
                        rule["last_active"] = False
                        rule.pop("last_triggered_at", None)
                        rule.pop("last_checked_at", None)
                        rule.pop("last_message", None)
                    if payload.get("reset"):
                        rule["last_active"] = False
                        rule.pop("last_triggered_at", None)
                    return rule
                _, rule = update_state(change_alert, "alert-edit")
                self.send_json({"ok": True, "item": rule})
            else:
                self.send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.handle_api_error(exc)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not (parsed.path.startswith("/api/watchlist/") or parsed.path.startswith("/api/alerts/") or parsed.path in {"/api/records", "/api/audit"}):
                self.send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)
                return
            def delete_item(state: dict[str, Any]) -> None:
                if parsed.path.startswith("/api/watchlist/"):
                    symbol = normalize_symbol(unquote(parsed.path.rsplit("/", 1)[-1]))
                    state["watchlist"] = [item for item in state["watchlist"] if item["symbol"] != symbol]
                    state["alerts"] = [item for item in state["alerts"] if item["symbol"] != symbol]
                elif parsed.path.startswith("/api/alerts/"):
                    alert_id = unquote(parsed.path.rsplit("/", 1)[-1])
                    state["alerts"] = [item for item in state["alerts"] if item["id"] != alert_id]
                elif parsed.path == "/api/records":
                    state["records"] = []
                else:
                    state["audit"] = []
            update_state(delete_item, "delete")
            self.send_json({"ok": True})
        except Exception as exc:
            self.handle_api_error(exc)

    def handle_get_api(self, path: str, query: dict[str, list[str]]) -> None:
        state = load_state()
        if path == "/api/health":
            configuration_warning = None
            try:
                provider_name = get_provider(state["settings"].get("source", "auto")).name
            except DataSourceError as exc:
                provider_name = "unavailable"
                configuration_warning = str(exc)
            self.send_json({
                "ok": True, "app": "自主看盘台", "provider": provider_name,
                "preference": state["settings"].get("source", "auto"),
                "has_hithink_key": bool(os.getenv("HITHINK_FINANCE_API_KEY")),
                "server_time": datetime.now(SHANGHAI).isoformat(), "market_session": market_session(),
                "sources": provider_health(), "capabilities": source_capabilities(),
                "monitor": monitor_status(), "desktop": desktop_status(),
                "configuration_warning": configuration_warning,
            })
        elif path == "/api/state":
            safe_state = dict(state)
            safe_state.pop("audit", None)
            safe_state["runtime"] = {"has_hithink_key": bool(os.getenv("HITHINK_FINANCE_API_KEY"))}
            self.send_json({"ok": True, "state": safe_state})
        elif path == "/api/export":
            body = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8-sig")
            self.send_download(body, "application/json; charset=utf-8", "marketdesk-state.json")
        elif path == "/api/backups":
            self.send_json({"ok": True, "items": list_backups(), "limit": MAX_BACKUPS})
        elif path == "/api/search":
            keyword = query.get("q", [""])[0].strip()
            if not keyword:
                self.send_json({"ok": True, "items": [], "warning": None})
                return
            if len(keyword) > 30:
                raise ValueError("搜索词不能超过 30 个字符")
            folded = keyword.casefold()
            local_items = []
            for item in state["watchlist"]:
                symbol = item["symbol"]
                name = item.get("name") or symbol
                if folded in symbol.casefold() or folded in symbol.split(".")[0] or folded in name.casefold():
                    local_items.append({"symbol": symbol, "code": symbol.split(".")[0], "name": name, "pinyin": ""})
            warning = None
            try:
                remote_items = search_symbols(keyword, 10)
            except DataSourceError as exc:
                remote_items = []
                warning = str(exc)
            merged = []
            seen = set()
            for item in local_items + remote_items:
                if item["symbol"] not in seen:
                    seen.add(item["symbol"])
                    merged.append(item)
            self.send_json({"ok": True, "items": merged[:10], "warning": warning})
        elif path == "/api/quotes":
            symbols = query.get("symbols", [",".join(item["symbol"] for item in state["watchlist"])])[0].split(",")
            symbols = [normalize_symbol(symbol) for symbol in symbols if symbol.strip()]
            provider = get_provider(state["settings"].get("source", "auto"))
            cache_key = ",".join(sorted(symbols))
            quotes, data_status, warning, cached_at = fetch_with_cache(
                "quotes", cache_key, lambda: provider.quotes(symbols),
            ) if symbols else ([], "live", None, None)
            name_map = {item["symbol"]: item.get("name") for item in state["watchlist"]}
            for quote in quotes:
                if quote.get("name") in (quote["symbol"], quote["symbol"].split(".")[0]):
                    quote["name"] = name_map.get(quote["symbol"]) or quote["name"]
            sources = sorted({item.get("source") for item in quotes if item.get("source")})
            self.send_json({
                "ok": True, "items": quotes, "provider": provider.name, "sources": sources,
                "data_status": data_status, "cached_at": cached_at, "warning": warning,
                "fetched_at": datetime.now(SHANGHAI).isoformat(), "market_session": market_session(),
            })
        elif path == "/api/history":
            symbol = normalize_symbol(query.get("symbol", [""])[0])
            limit = max(1, min(1000, int(query.get("limit", ["250"])[0])))
            provider = get_provider(state["settings"].get("source", "auto"))
            raw_bars, data_status, warning, cached_at = fetch_with_cache(
                "history", f"{symbol}:{limit}", lambda: provider.history(symbol, limit),
            )
            bars = enrich_bars(raw_bars[-limit:])
            completed = completed_daily_bars(bars)
            sources = sorted({item.get("source") for item in bars if item.get("source")})
            self.send_json({
                "ok": True, "symbol": symbol, "items": bars, "provider": provider.name, "sources": sources,
                "adjust": "forward", "completed_items": len(completed), "analysis": analyze_security(completed),
                "data_status": data_status, "cached_at": cached_at, "warning": warning,
                "fetched_at": datetime.now(SHANGHAI).isoformat(),
            })
        elif path == "/api/company":
            symbol = normalize_symbol(query.get("symbol", [""])[0])
            items, data_status, warning, cached_at = fetch_with_cache(
                "company", symbol, lambda: company_overview(symbol),
            )
            self.send_json({
                "ok": True, "item": items[0] if items else None, "data_status": data_status,
                "cached_at": cached_at, "warning": warning,
            })
        elif path == "/api/market-overview":
            try:
                items, data_status, warning, cached_at = fetch_with_cache(
                    "market", "mainland-headline", market_overview,
                )
            except DataSourceError as exc:
                items, data_status, warning, cached_at = [], "unavailable", str(exc), None
            try:
                breadth_items, breadth_status, breadth_warning, breadth_cached_at = fetch_with_cache(
                    "market-breadth", "shanghai-shenzhen", market_breadth,
                )
                breadth = breadth_items[0] if breadth_items else {"available": False, "reason": "市场宽度返回为空"}
            except DataSourceError as exc:
                breadth = {"available": False, "reason": str(exc), "scope": "沪深两市"}
                breadth_status, breadth_warning, breadth_cached_at = "unavailable", str(exc), None
            self.send_json({
                "ok": True, "items": items, "breadth": breadth,
                "analysis": analyze_market_snapshot(items, breadth),
                "data_status": data_status, "cached_at": cached_at, "warning": warning,
                "breadth_status": breadth_status, "breadth_cached_at": breadth_cached_at,
                "breadth_warning": breadth_warning,
            })
        elif path == "/api/audit":
            self.send_json({
                "ok": True, "items": filtered_audit(state, query),
                "summaries": [{
                    "rule_id": rule["id"], "symbol": rule["symbol"], "type": rule["type"],
                    "stats": rule.get("stats") or {key: 0 for key in STAT_KEYS},
                    "last_checked_at": rule.get("last_checked_at"), "last_message": rule.get("last_message"),
                } for rule in state["alerts"]],
                "total": len(state.get("audit", [])),
            })
        elif path == "/api/alerts":
            self.send_json({"ok": True, "items": state["alerts"], "records": state["records"]})
        elif path == "/api/records.csv":
            body = records_csv(filter_records(state["records"], query))
            self.send_download(body, "text/csv; charset=utf-8", "marketdesk-alert-records.csv")
        else:
            self.send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else unquote(path.lstrip("/"))
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith(("text/", "application/javascript")) else mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def validate_alert(payload: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol(str(payload["symbol"]))
    kind = str(payload["type"])
    params = dict(payload.get("params") or {})
    if kind in {"price_above", "price_below"}:
        threshold = float(params.get("threshold", 0))
        if threshold <= 0:
            raise ValueError("价格阈值必须大于 0")
        params = {"threshold": threshold}
    elif kind == "ma_cross":
        short, long = int(params.get("short", 5)), int(params.get("long", 20))
        if short < 2 or long <= short or long > 250:
            raise ValueError("均线周期需要满足 2 ≤ 短期 < 长期 ≤ 250")
        params = {"short": short, "long": long, "direction": "below" if params.get("direction") == "below" else "above"}
    elif kind == "breakout":
        lookback = int(params.get("lookback", 20))
        if not 2 <= lookback <= 250:
            raise ValueError("突破观察周期应在 2 到 250 日之间")
        params = {"lookback": lookback, "direction": "low" if params.get("direction") == "low" else "high"}
    elif kind == "volume_surge":
        window, multiple = int(params.get("window", 5)), float(params.get("multiple", 2))
        if not 2 <= window <= 60 or not 1 <= multiple <= 20:
            raise ValueError("成交量窗口或倍数超出范围")
        params = {"window": window, "multiple": multiple}
    elif kind == "rsi_threshold":
        period = int(params.get("period", 14))
        direction = "below" if params.get("direction") == "below" else "above"
        threshold = float(params.get("threshold", 30 if direction == "below" else 70))
        if not 2 <= period <= 60 or not 0 < threshold < 100:
            raise ValueError("RSI 周期应在 2 到 60 之间，阈值应在 0 到 100 之间")
        params = {"period": period, "direction": direction, "threshold": threshold}
    elif kind == "macd_cross":
        short = int(params.get("short", 12))
        long = int(params.get("long", 26))
        signal = int(params.get("signal", 9))
        if short < 2 or long <= short or long > 120 or not 2 <= signal <= 60:
            raise ValueError("MACD 周期需要满足 2 ≤ 短期 < 长期 ≤ 120，信号期为 2 到 60")
        params = {
            "short": short, "long": long, "signal": signal,
            "direction": "below" if params.get("direction") == "below" else "above",
        }
    elif kind == "breakout_volume":
        lookback = int(params.get("lookback", 20))
        window = int(params.get("window", 5))
        multiple = float(params.get("multiple", 2))
        if not 2 <= lookback <= 250 or not 2 <= window <= 60 or not 1 <= multiple <= 20:
            raise ValueError("组合规则的突破周期、成交量窗口或倍数超出范围")
        params = {
            "lookback": lookback, "direction": "low" if params.get("direction") == "low" else "high",
            "window": window, "multiple": multiple,
        }
    elif kind == "trend_state":
        state = str(params.get("state", "up"))
        if state not in {"up", "down", "transition_up", "transition_down", "range"}:
            raise ValueError("不支持的趋势状态")
        params = {"state": state}
    elif kind == "candlestick_pattern":
        pattern = str(params.get("pattern", "doji"))
        if pattern not in PATTERN_LABELS:
            raise ValueError("不支持的K线形态")
        params = {"pattern": pattern}
    else:
        raise ValueError("不支持的预警类型")
    return {"symbol": symbol, "type": kind, "params": params}


def _is_existing_dashboard(address: str) -> bool:
    try:
        with urlopen(f"{address}/api/health", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok") and payload.get("app") == "自主看盘台")
    except Exception:
        return False


def candidate_ports(preferred: int, allow_fallback: bool, attempts: int = 11) -> list[int]:
    """Return the requested port followed by a small deterministic fallback range."""
    if not 1 <= preferred <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    count = max(1, attempts if allow_fallback else 1)
    return list(range(preferred, min(preferred + count, 65536)))


def main() -> None:
    parser = argparse.ArgumentParser(description="自主看盘台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-multiple", action="store_true", help="允许同端口互斥锁之外的测试实例")
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument("--open", dest="open_browser", action="store_true")
    browser_group.add_argument("--no-open", dest="open_browser", action="store_false")
    tray_group = parser.add_mutually_exclusive_group()
    tray_group.add_argument("--tray", dest="use_tray", action="store_true")
    tray_group.add_argument("--no-tray", dest="use_tray", action="store_false")
    port_group = parser.add_mutually_exclusive_group()
    port_group.add_argument("--port-fallback", dest="port_fallback", action="store_true")
    port_group.add_argument("--no-port-fallback", dest="port_fallback", action="store_false")
    parser.set_defaults(open_browser=FROZEN, use_tray=FROZEN and IS_WINDOWS, port_fallback=PORTABLE_MODE)
    args = parser.parse_args()
    enforce_single_instance = FROZEN and IS_WINDOWS and not args.allow_multiple

    try:
        configure_logging()
    except OSError as exc:
        if FROZEN and IS_WINDOWS:
            show_message("自主看盘台无法启动", f"无法创建日志目录：{exc}", error=True)
        else:
            print(f"无法创建日志目录：{exc}", file=sys.stderr)
        return

    server: LocalThreadingHTTPServer | None = None
    instance: SingleInstance | None = None
    address = f"http://{args.host}:{args.port}"
    last_error: OSError | None = None
    for port in candidate_ports(args.port, args.port_fallback):
        candidate_address = f"http://{args.host}:{port}"
        candidate_instance = SingleInstance(port)
        if enforce_single_instance and not candidate_instance.acquire():
            if _is_existing_dashboard(candidate_address):
                if args.open_browser:
                    open_dashboard_address(candidate_address, private=PORTABLE_MODE)
                return
            continue
        try:
            candidate_server = LocalThreadingHTTPServer((args.host, port), Handler)
        except OSError as exc:
            last_error = exc
            existing = _is_existing_dashboard(candidate_address)
            candidate_instance.release()
            if existing:
                if args.open_browser:
                    open_dashboard_address(candidate_address, private=PORTABLE_MODE)
                return
            LOGGER.warning("端口 %s 不可用：%s", port, exc)
            continue
        server = candidate_server
        instance = candidate_instance
        address = candidate_address
        if port != args.port:
            LOGGER.info("首选端口 %s 被占用，已自动切换到 %s", args.port, port)
        break

    if server is None or instance is None:
        detail = str(last_error or "候选端口均不可用")
        LOGGER.error("无法监听首选端口及备用端口：%s", detail)
        if FROZEN and IS_WINDOWS and args.open_browser:
            show_message("自主看盘台", f"端口 {args.port} 及备用端口均被占用。\n\n详情：{detail}", error=True)
        elif not FROZEN:
            print(f"无法监听 {address}：{detail}", file=sys.stderr)
        return

    _update_desktop_status(single_instance=enforce_single_instance)
    LOGGER.info("自主看盘台启动，地址=%s，冻结打包=%s，便携模式=%s", address, FROZEN, PORTABLE_MODE)

    tray: TrayController | None = None

    def notify_records(records: list[dict[str, Any]]) -> None:
        if not tray or not tray.running:
            return
        for record in records:
            title = f"自主看盘台 · {record.get('name') or record.get('symbol')}"
            tray.show_notification(title, str(record.get("message") or "预警条件已触发"))

    monitor = AlertMonitor(on_triggered=notify_records)
    if args.use_tray:
        tray = TrayController(
            address=address,
            log_dir=LOG_DIR,
            on_toggle_monitor=monitor.toggle_paused,
            is_monitor_paused=monitor.is_paused,
            on_exit=server.shutdown,
            portable_mode=PORTABLE_MODE,
        )
        if not activate_tray(tray):
            tray = None
        else:
            set_native_notification_sender(tray.show_notification)

    monitor.start()
    if not FROZEN:
        print(f"自主看盘台已启动：{address}")
        print("按 Ctrl+C 停止。")
    if args.open_browser:
        browser_timer = threading.Timer(0.8, lambda: open_dashboard_address(address, private=PORTABLE_MODE))
        browser_timer.daemon = True
        browser_timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if not FROZEN:
            print("\n已停止。")
    finally:
        LOGGER.info("自主看盘台正在停止")
        monitor.stop()
        if tray:
            tray.stop()
        set_native_notification_sender(None)
        _update_desktop_status(tray_running=False, native_notifications=False)
        server.server_close()
        instance.release()
        LOGGER.info("自主看盘台已停止")


if __name__ == "__main__":
    main()
