from __future__ import annotations

import http.client
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, TypeVar


SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
T = TypeVar("T")


class DataSourceError(RuntimeError):
    pass


def normalize_symbol(raw: str) -> str:
    symbol = raw.strip().upper().replace(" ", "")
    if "." in symbol:
        code, suffix = symbol.split(".", 1)
        if suffix not in {"SH", "SZ", "BJ"}:
            raise ValueError("仅支持 SH、SZ、BJ 市场")
    else:
        code = symbol
        if code.startswith(("5", "6", "9")):
            suffix = "SH"
        elif code.startswith(("4", "8")):
            suffix = "BJ"
        else:
            suffix = "SZ"
    if len(code) != 6 or not code.isdigit():
        raise ValueError("请输入 6 位股票代码，例如 600519")
    return f"{code}.{suffix}"


def search_symbols(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search Tencent's suggestion index and keep mainland A-share equities."""
    keyword = query.strip()
    if not keyword:
        return []
    safe_limit = max(1, min(int(limit), 20))
    params = urllib.parse.urlencode({"v": "2", "q": keyword, "t": "all"})
    text = _get_text(f"https://smartbox.gtimg.cn/s3/?{params}")
    try:
        encoded_value = text.split("=", 1)[1].strip().rstrip(";")
        decoded_value = json.loads(encoded_value)
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise DataSourceError("腾讯股票搜索返回格式异常") from exc

    suffixes = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in str(decoded_value).split("^"):
        parts = record.split("~")
        if len(parts) < 5:
            continue
        market, code, name, pinyin, asset_type = parts[:5]
        if market not in suffixes or asset_type != "GP-A" or len(code) != 6 or not code.isdigit():
            continue
        symbol = f"{code}.{suffixes[market]}"
        if symbol in seen:
            continue
        seen.add(symbol)
        results.append({"symbol": symbol, "code": code, "name": name, "pinyin": pinyin})
        if len(results) >= safe_limit:
            break
    return results


def _eastmoney_secid(symbol: str) -> str:
    code, suffix = normalize_symbol(symbol).split(".")
    return f"{'1' if suffix == 'SH' else '0'}.{code}"


def company_overview(symbol: str) -> list[dict[str, Any]]:
    """Return public company context; missing optional fields remain null."""
    normalized = normalize_symbol(symbol)
    query = urllib.parse.urlencode({
        "secid": _eastmoney_secid(normalized),
        "fields": "f57,f58,f84,f85,f116,f117,f127,f128,f162,f167,f189",
    })

    def fetch() -> list[dict[str, Any]]:
        payload = _get_json(
            f"https://push2.eastmoney.com/api/qt/stock/get?{query}",
            {"Referer": "https://quote.eastmoney.com/", "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DataSourceError("东方财富公司概况缺少 data 字段")
        code = str(data.get("f57") or normalized.split(".")[0])
        if len(code) != 6 or not code.isdigit():
            raise DataSourceError("东方财富公司概况代码字段异常")
        raw_date = str(data.get("f189") or "")
        listing_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 and raw_date.isdigit() else None
        return [{
            "symbol": normalized, "name": str(data.get("f58") or normalized),
            "industry": str(data.get("f127") or "") or None,
            "region": str(data.get("f128") or "") or None,
            "listing_date": listing_date,
            "total_shares": _optional_number(data.get("f84"), "f84"),
            "float_shares": _optional_number(data.get("f85"), "f85"),
            "total_market_cap": _optional_number(data.get("f116"), "f116"),
            "float_market_cap": _optional_number(data.get("f117"), "f117"),
            "pe_dynamic": _optional_number(data.get("f162"), "f162", scale=100),
            "pb": _optional_number(data.get("f167"), "f167", scale=100),
            "source": "eastmoney", "fetched_at": _iso_now(),
        }]
    try:
        return _observed("eastmoney", fetch)
    except DataSourceError:
        def fallback() -> list[dict[str, Any]]:
            code, suffix = normalized.split(".")
            text = _get_text(f"https://qt.gtimg.cn/q={suffix.lower()}{code}", encoding="gbk")
            try:
                parts = text.split("~")
                if len(parts) < 53:
                    raise ValueError("fields")
                return [{
                    "symbol": normalized, "name": str(parts[1] or normalized),
                    "industry": None, "region": None, "listing_date": None,
                    "total_shares": None, "float_shares": None,
                    "total_market_cap": _optional_number(parts[44], "tencent.market_cap", scale=0.00000001),
                    "float_market_cap": _optional_number(parts[45], "tencent.float_market_cap", scale=0.00000001),
                    "pe_dynamic": _optional_number(parts[52], "tencent.pe_dynamic"),
                    "pb": _optional_number(parts[46], "tencent.pb"),
                    "source": "tencent", "fetched_at": _iso_now(),
                    "partial": True,
                }]
            except (IndexError, ValueError) as exc:
                raise DataSourceError("腾讯公司概况返回格式异常") from exc
        return _observed("tencent", fallback)


def market_overview() -> list[dict[str, Any]]:
    """Return the Shanghai, Shenzhen and ChiNext headline indices."""
    symbols = {"sh000001": "000001.SH", "sz399001": "399001.SZ", "sz399006": "399006.SZ"}

    def fetch() -> list[dict[str, Any]]:
        text = _get_text("https://qt.gtimg.cn/q=s_sh000001,s_sz399001,s_sz399006", encoding="gbk")
        items: list[dict[str, Any]] = []
        for line in text.split(";"):
            if "=\"" not in line:
                continue
            key = line.split("=", 1)[0].replace("v_s_", "").strip()
            if key not in symbols:
                continue
            parts = line.split('="', 1)[1].rstrip('"').split("~")
            if len(parts) < 7:
                raise DataSourceError("腾讯大盘指数返回字段不足")
            items.append({
                "symbol": symbols[key], "name": str(parts[1]),
                "price": _number(parts[3], "index.price"),
                "change": _number(parts[4], "index.change"),
                "change_pct": _number(parts[5], "index.change_pct"),
                "volume": _optional_number(parts[6], "index.volume"),
                "source": "tencent", "fetched_at": _iso_now(),
            })
        if len(items) != 3:
            raise DataSourceError("腾讯大盘指数返回不完整")
        return items
    return _observed("tencent", fetch)


def _number(value: Any, field: str, *, required: bool = True, scale: float = 1.0) -> float | None:
    if value in (None, "", "-"):
        if required:
            raise DataSourceError(f"字段 {field} 缺失")
        return None
    if isinstance(value, bool):
        raise DataSourceError(f"字段 {field} 类型错误")
    try:
        result = float(value) / scale
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataSourceError(f"字段 {field} 不是有效数字") from exc
    if not math.isfinite(result):
        raise DataSourceError(f"字段 {field} 不是有限数字")
    return result


def _integer(value: Any, field: str, *, required: bool = True) -> int | None:
    number = _number(value, field, required=required)
    return None if number is None else int(number)


def _optional_number(value: Any, field: str, *, scale: float = 1.0) -> float | None:
    try:
        return _number(value, field, required=False, scale=scale)
    except DataSourceError:
        return None


def _get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 10) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise DataSourceError("数据源返回的 JSON 顶层不是对象")
                return payload
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, http.client.HTTPException, OSError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
    raise DataSourceError(f"数据请求失败：{last_error}") from last_error


def _get_text(url: str, encoding: str = "utf-8", headers: dict[str, str] | None = None, timeout: int = 10) -> str:
    request_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode(encoding, errors="replace")
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
    raise DataSourceError(f"数据请求失败：{last_error}") from last_error


_HEALTH_LOCK = threading.Lock()
_HEALTH: dict[str, dict[str, Any]] = {
    "hithink": {"label": "同花顺官方", "last_attempt_at": None, "last_success_at": None, "last_error": None, "last_duration_ms": None, "consecutive_failures": 0, "retry_after": None},
    "tencent": {"label": "腾讯公开行情", "last_attempt_at": None, "last_success_at": None, "last_error": None, "last_duration_ms": None, "consecutive_failures": 0, "retry_after": None},
    "eastmoney": {"label": "东方财富公开行情", "last_attempt_at": None, "last_success_at": None, "last_error": None, "last_duration_ms": None, "consecutive_failures": 0, "retry_after": None},
}


def _iso_now() -> str:
    return datetime.now(SHANGHAI).isoformat()


def _observed(source: str, operation: Callable[[], T]) -> T:
    now = datetime.now(SHANGHAI)
    with _HEALTH_LOCK:
        retry_after = _HEALTH[source].get("retry_after")
    if retry_after:
        retry_time = datetime.fromisoformat(retry_after)
        if retry_time > now:
            remaining = max(1, round((retry_time - now).total_seconds()))
            raise DataSourceError(f"{_HEALTH[source]['label']}暂时熔断，约 {remaining} 秒后重试")
    started = time.perf_counter()
    attempted_at = _iso_now()
    try:
        result = operation()
    except Exception as exc:
        duration = round((time.perf_counter() - started) * 1000)
        with _HEALTH_LOCK:
            failures = int(_HEALTH[source].get("consecutive_failures") or 0) + 1
            cooldown = min(15 * 60, 30 * (2 ** min(failures - 1, 5)))
            _HEALTH[source].update(
                last_attempt_at=attempted_at, last_error=str(exc), last_duration_ms=duration,
                consecutive_failures=failures,
                retry_after=(datetime.now(SHANGHAI) + timedelta(seconds=cooldown)).isoformat(),
            )
        raise
    duration = round((time.perf_counter() - started) * 1000)
    with _HEALTH_LOCK:
        _HEALTH[source].update(
            last_attempt_at=attempted_at, last_success_at=_iso_now(), last_error=None, last_duration_ms=duration,
            consecutive_failures=0, retry_after=None,
        )
    return result


def provider_health() -> list[dict[str, Any]]:
    has_key = bool(os.getenv("HITHINK_FINANCE_API_KEY", "").strip())
    with _HEALTH_LOCK:
        result = []
        for source, values in _HEALTH.items():
            item = {"source": source, **values}
            item["configured"] = has_key if source == "hithink" else True
            retry_after = item.get("retry_after")
            cooling_down = bool(retry_after and datetime.fromisoformat(retry_after) > datetime.now(SHANGHAI))
            item["status"] = "disabled" if source == "hithink" and not has_key else (
                "cooldown" if cooling_down else "error" if item["last_error"] else "ok" if item["last_success_at"] else "idle"
            )
            result.append(item)
        return result


def source_capabilities() -> list[dict[str, Any]]:
    return [
        {
            "source": "hithink", "label": "同花顺官方", "quotes": True, "daily_history": True,
            "adjust": "forward", "volume_unit": "股", "authentication": "HITHINK_FINANCE_API_KEY",
            "contract": "官方 REST API；权限与费用以当前账户和官方文档为准",
        },
        {
            "source": "tencent", "label": "腾讯公开行情", "quotes": True, "daily_history": True,
            "adjust": "forward", "volume_unit": "股（由手换算）", "authentication": None,
            "contract": "非正式公共接口，字段或可用性可能变化",
        },
        {
            "source": "eastmoney", "label": "东方财富公开行情", "quotes": True, "daily_history": True,
            "adjust": "forward", "volume_unit": "股（由手换算）", "authentication": None,
            "contract": "非正式公共接口，字段或可用性可能变化",
        },
    ]


class MarketProvider(ABC):
    name: str

    @abstractmethod
    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def history(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        raise NotImplementedError


class TencentProvider(MarketProvider):
    name = "tencent"

    @staticmethod
    def _key(symbol: str) -> str:
        code, suffix = normalize_symbol(symbol).split(".")
        prefix = "sh" if suffix == "SH" else "sz" if suffix == "SZ" else "bj"
        return f"{prefix}{code}"

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        normalized = [normalize_symbol(symbol) for symbol in symbols]
        keys = [self._key(symbol) for symbol in normalized]
        symbol_map = dict(zip(keys, normalized))
        text = _get_text(
            f"https://qt.gtimg.cn/q={','.join(keys)}", encoding="gbk", headers={"Referer": "https://gu.qq.com/"},
        )
        result: list[dict[str, Any]] = []
        for line in text.splitlines():
            if "=\"" not in line:
                continue
            key = line.split("=", 1)[0].removeprefix("v_")
            values = line.split('"', 1)[1].rsplit('"', 1)[0].split("~")
            if key not in symbol_map or len(values) < 37 or not values[3]:
                continue
            timestamp = None
            if values[30]:
                try:
                    timestamp = int(datetime.strptime(values[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI).timestamp() * 1000)
                except ValueError:
                    timestamp = None
            transaction = values[35].split("/")
            turnover = _optional_number(transaction[2], "tencent.turnover") if len(transaction) >= 3 else None
            result.append({
                "symbol": symbol_map[key], "name": values[1] or symbol_map[key],
                "price": _number(values[3], "tencent.price"),
                "open": _number(values[5], "tencent.open", required=False),
                "high": _number(values[33], "tencent.high", required=False),
                "low": _number(values[34], "tencent.low", required=False),
                "previous_close": _number(values[4], "tencent.previous_close", required=False),
                "change": _number(values[31], "tencent.change", required=False),
                "change_pct": _number(values[32], "tencent.change_pct", required=False),
                "volume": _number(values[36] or values[6], "tencent.volume", required=False, scale=0.01),
                "turnover": turnover, "timestamp": timestamp, "source": self.name,
            })
        if len(result) != len(normalized):
            missing = sorted(set(normalized) - {item["symbol"] for item in result})
            raise DataSourceError(f"腾讯未返回完整行情：{', '.join(missing)}")
        return result

    def history(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        key = self._key(symbol)
        safe_limit = max(30, min(int(limit), 1000))
        query = urllib.parse.urlencode({"param": f"{key},day,,,{safe_limit},qfq"})
        payload = _get_json(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{query}")
        stock = (payload.get("data") or {}).get(key) or {}
        rows = stock.get("qfqday") or stock.get("day") or []
        bars: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                date = datetime.strptime(str(row[0]), "%Y-%m-%d").strftime("%Y-%m-%d")
                bars.append({
                    "date": date,
                    "open": _number(row[1], "tencent.open"), "close": _number(row[2], "tencent.close"),
                    "high": _number(row[3], "tencent.high"), "low": _number(row[4], "tencent.low"),
                    "volume": _number(row[5], "tencent.volume", scale=0.01),
                    "turnover": _optional_number(row[6], "tencent.turnover") if len(row) > 6 else None,
                    "source": self.name, "adjust": "forward",
                })
            except (DataSourceError, ValueError):
                continue
        if not bars:
            raise DataSourceError(f"腾讯未取得 {symbol} 的历史日线")
        return bars[-int(limit):]


class EastmoneyProvider(MarketProvider):
    name = "eastmoney"

    @staticmethod
    def _secid(symbol: str) -> str:
        return _eastmoney_secid(symbol)

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        fields = "f57,f58,f43,f44,f45,f46,f60,f47,f48,f169,f170,f86"
        for raw_symbol in symbols:
            symbol = normalize_symbol(raw_symbol)
            query = urllib.parse.urlencode({"secid": self._secid(symbol), "fields": fields})
            payload = _get_json(
                f"https://push2.eastmoney.com/api/qt/stock/get?{query}", {"Referer": "https://quote.eastmoney.com/"},
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise DataSourceError(f"东方财富未找到 {symbol} 的行情")
            timestamp = _integer(data.get("f86"), "eastmoney.timestamp", required=False)
            result.append({
                "symbol": symbol, "name": data.get("f58") or symbol,
                "price": _number(data.get("f43"), "eastmoney.price", scale=100.0),
                "open": _number(data.get("f46"), "eastmoney.open", required=False, scale=100.0),
                "high": _number(data.get("f44"), "eastmoney.high", required=False, scale=100.0),
                "low": _number(data.get("f45"), "eastmoney.low", required=False, scale=100.0),
                "previous_close": _number(data.get("f60"), "eastmoney.previous_close", required=False, scale=100.0),
                "change": _number(data.get("f169"), "eastmoney.change", required=False, scale=100.0),
                "change_pct": _number(data.get("f170"), "eastmoney.change_pct", required=False, scale=100.0),
                "volume": _number(data.get("f47"), "eastmoney.volume", required=False, scale=0.01),
                "turnover": _number(data.get("f48"), "eastmoney.turnover", required=False),
                "timestamp": timestamp * 1000 if timestamp is not None else None, "source": self.name,
            })
        return result

    def history(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        safe_limit = max(30, min(int(limit), 1000))
        query = urllib.parse.urlencode({
            "secid": self._secid(symbol), "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101", "fqt": "1", "beg": "0", "end": "20500101", "lmt": str(safe_limit),
        })
        payload = _get_json(
            f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{query}", {"Referer": "https://quote.eastmoney.com/"},
        )
        data = payload.get("data") or {}
        bars: list[dict[str, Any]] = []
        for row in data.get("klines") or []:
            parts = row.split(",") if isinstance(row, str) else []
            if len(parts) < 7:
                continue
            try:
                date = datetime.strptime(parts[0], "%Y-%m-%d").strftime("%Y-%m-%d")
                bars.append({
                    "date": date,
                    "open": _number(parts[1], "eastmoney.open"), "close": _number(parts[2], "eastmoney.close"),
                    "high": _number(parts[3], "eastmoney.high"), "low": _number(parts[4], "eastmoney.low"),
                    "volume": _number(parts[5], "eastmoney.volume", scale=0.01),
                    "turnover": _number(parts[6], "eastmoney.turnover", required=False),
                    "source": self.name, "adjust": "forward",
                })
            except (DataSourceError, ValueError):
                continue
        if not bars:
            raise DataSourceError(f"东方财富未取得 {symbol} 的历史日线")
        return bars[-int(limit):]


class PublicProvider(MarketProvider):
    name = "public"

    def __init__(self) -> None:
        self.tencent = TencentProvider()
        self.eastmoney = EastmoneyProvider()

    @staticmethod
    def _fallback(providers: list[MarketProvider], method: str, *args: Any) -> Any:
        errors: list[str] = []
        for provider in providers:
            try:
                return _observed(provider.name, lambda p=provider: getattr(p, method)(*args))
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        raise DataSourceError("；".join(errors))

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._fallback([self.tencent, self.eastmoney], "quotes", symbols)

    def history(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        return self._fallback([self.eastmoney, self.tencent], "history", symbol, limit)


class HithinkProvider(MarketProvider):
    name = "hithink"
    base_url = "https://fuyao.aicubes.cn"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        payload = _get_json(f"{self.base_url}{path}?{query}", {"X-api-key": self.api_key})
        if payload.get("code") != 0:
            code = payload.get("code")
            request_id = payload.get("request_id")
            detail = payload.get("message") or f"业务错误 {code}"
            suffix = f"（request_id: {request_id}）" if request_id else ""
            raise DataSourceError(f"同花顺接口 {detail}{suffix}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DataSourceError("同花顺接口成功响应缺少 data 对象")
        return data

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        normalized = [normalize_symbol(symbol) for symbol in symbols]
        data = self._request("/api/a-share/prices/snapshot", {"thscodes": ",".join(normalized)})
        timestamp = _integer(data.get("timestamp"), "hithink.timestamp", required=False)
        items = data.get("item")
        if not isinstance(items, list):
            raise DataSourceError("同花顺快照缺少 item 数组")
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise DataSourceError("同花顺快照 item 类型错误")
            symbol = normalize_symbol(str(item.get("thscode") or ""))
            result.append({
                "symbol": symbol, "name": item.get("ticker") or symbol,
                "price": _number(item.get("last_price"), "hithink.last_price"),
                "open": _number(item.get("open_price"), "hithink.open_price", required=False),
                "high": _number(item.get("high_price"), "hithink.high_price", required=False),
                "low": _number(item.get("low_price"), "hithink.low_price", required=False),
                "previous_close": _number(item.get("prev_price"), "hithink.prev_price", required=False),
                "change": _number(item.get("price_change"), "hithink.price_change", required=False),
                "change_pct": _number(item.get("price_change_ratio_pct"), "hithink.change_pct", required=False),
                "volume": _number(item.get("volume"), "hithink.volume", required=False),
                "turnover": _number(item.get("turnover"), "hithink.turnover", required=False),
                "timestamp": timestamp, "source": self.name,
            })
        if {item["symbol"] for item in result} != set(normalized):
            raise DataSourceError("同花顺未返回全部请求标的")
        return result

    def history(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        safe_limit = max(1, min(int(limit), 1000))
        end = datetime.now(SHANGHAI)
        start = end - timedelta(days=max(safe_limit * 2, 365))
        data = self._request("/api/a-share/prices/historical", {
            "thscode": symbol, "interval": "1d", "start": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000), "adjust": "forward",
        })
        items = data.get("item")
        if not isinstance(items, list):
            raise DataSourceError("同花顺历史行情缺少 item 数组")
        bars: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise DataSourceError("同花顺历史行情 item 类型错误")
            date_ms = _integer(item.get("date_ms"), "hithink.date_ms")
            bars.append({
                "date": datetime.fromtimestamp(date_ms / 1000, SHANGHAI).strftime("%Y-%m-%d"),
                "open": _number(item.get("open_price"), "hithink.open_price"),
                "close": _number(item.get("close_price"), "hithink.close_price"),
                "high": _number(item.get("high_price"), "hithink.high_price"),
                "low": _number(item.get("low_price"), "hithink.low_price"),
                "volume": _number(item.get("volume"), "hithink.volume"),
                "turnover": _number(item.get("turnover"), "hithink.turnover", required=False),
                "source": self.name, "adjust": "forward",
            })
        bars.sort(key=lambda item: item["date"])
        if not bars:
            raise DataSourceError(f"同花顺未取得 {symbol} 的历史日线")
        return bars[-safe_limit:]


class AutoProvider(MarketProvider):
    name = "auto"

    def __init__(self, primary: HithinkProvider | None = None, route_name: str = "auto"):
        self.primary = primary
        self.public = PublicProvider()
        self.name = route_name

    def _run(self, method: str, *args: Any) -> Any:
        errors: list[str] = []
        if self.primary is not None:
            try:
                return _observed("hithink", lambda: getattr(self.primary, method)(*args))
            except Exception as exc:
                errors.append(f"hithink: {exc}")
        try:
            return getattr(self.public, method)(*args)
        except Exception as exc:
            errors.append(f"public: {exc}")
        raise DataSourceError("；".join(errors))

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._run("quotes", symbols)

    def history(self, symbol: str, limit: int = 250) -> list[dict[str, Any]]:
        return self._run("history", symbol, limit)


def get_provider(preference: str = "auto") -> MarketProvider:
    requested = (preference or os.getenv("MARKET_DATA_SOURCE", "auto")).lower()
    api_key = os.getenv("HITHINK_FINANCE_API_KEY", "").strip()
    if requested not in {"auto", "eastmoney", "hithink"}:
        raise ValueError("不支持的数据源设置")
    if requested == "hithink":
        if not api_key:
            raise DataSourceError("未检测到 HITHINK_FINANCE_API_KEY，请改用自动选择或公开行情")
        return AutoProvider(HithinkProvider(api_key), route_name="hithink")
    if requested == "auto":
        return AutoProvider(HithinkProvider(api_key) if api_key else None)
    return PublicProvider()
