from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any, Iterable


def moving_average(values: Iterable[float], window: int) -> list[float | None]:
    numbers = [float(value) for value in values]
    if window <= 0:
        raise ValueError("window must be positive")
    result: list[float | None] = []
    rolling = 0.0
    for index, value in enumerate(numbers):
        rolling += value
        if index >= window:
            rolling -= numbers[index - window]
        result.append(rolling / window if index + 1 >= window else None)
    return result


def exponential_moving_average(values: Iterable[float], period: int) -> list[float]:
    """Return an EMA seeded with the first observed value."""
    numbers = [float(value) for value in values]
    if period <= 0:
        raise ValueError("period must be positive")
    if not numbers:
        return []
    alpha = 2.0 / (period + 1)
    result = [numbers[0]]
    for value in numbers[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def relative_strength_index(values: Iterable[float], period: int = 14) -> list[float | None]:
    """Return Wilder RSI; the first value appears after ``period`` changes."""
    numbers = [float(value) for value in values]
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(numbers)
    if len(numbers) <= period:
        return result

    changes = [numbers[index] - numbers[index - 1] for index in range(1, len(numbers))]
    average_gain = sum(max(change, 0.0) for change in changes[:period]) / period
    average_loss = sum(max(-change, 0.0) for change in changes[:period]) / period

    def rsi_value(gain: float, loss: float) -> float:
        if gain == 0.0 and loss == 0.0:
            return 50.0
        if loss == 0.0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    result[period] = rsi_value(average_gain, average_loss)
    for index in range(period + 1, len(numbers)):
        change = changes[index - 1]
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
        result[index] = rsi_value(average_gain, average_loss)
    return result


def moving_average_convergence_divergence(
    values: Iterable[float], short: int = 12, long: int = 26, signal: int = 9,
) -> tuple[list[float], list[float], list[float]]:
    """Return DIF, DEA and the Chinese-market MACD histogram ``2 * (DIF - DEA)``."""
    numbers = [float(value) for value in values]
    if short <= 0 or long <= short or signal <= 0:
        raise ValueError("MACD periods must satisfy 0 < short < long and signal > 0")
    if not numbers:
        return [], [], []
    short_ema = exponential_moving_average(numbers, short)
    long_ema = exponential_moving_average(numbers, long)
    dif = [short_value - long_value for short_value, long_value in zip(short_ema, long_ema)]
    dea = exponential_moving_average(dif, signal)
    histogram = [2.0 * (dif_value - dea_value) for dif_value, dea_value in zip(dif, dea)]
    return dif, dea, histogram


def enrich_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closes = [float(bar["close"]) for bar in bars]
    averages = {window: moving_average(closes, window) for window in (5, 10, 20, 60)}
    rsi14 = relative_strength_index(closes, 14)
    macd_dif, macd_dea, macd_hist = moving_average_convergence_divergence(closes)
    enriched: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        item = dict(bar)
        for window, values in averages.items():
            item[f"ma{window}"] = values[index]
        item["rsi14"] = rsi14[index]
        item["macd_dif"] = macd_dif[index]
        item["macd_dea"] = macd_dea[index]
        item["macd_hist"] = macd_hist[index]
        enriched.append(item)
    return enriched


@dataclass(frozen=True)
class Evaluation:
    active: bool
    triggered: bool
    value: float | None
    threshold: float | None
    message: str


def evaluate_alert(
    rule: dict[str, Any],
    quote: dict[str, Any] | None,
    bars: list[dict[str, Any]],
    previous_active: bool = False,
) -> Evaluation:
    kind = rule.get("type")
    params = rule.get("params", {})

    if kind in {"price_above", "price_below"}:
        if not quote or quote.get("price") is None:
            return Evaluation(False, False, None, None, "缺少最新价格")
        value = float(quote["price"])
        threshold = float(params.get("threshold", 0))
        active = value >= threshold if kind == "price_above" else value <= threshold
        label = "高于" if kind == "price_above" else "低于"
        relation = label if active else f"未{label}"
        return Evaluation(active, active and not previous_active, value, threshold, f"最新价 {value:.2f} {relation} {threshold:.2f}")

    if len(bars) < 3:
        return Evaluation(False, False, None, None, "历史数据不足")

    if kind == "ma_cross":
        short = int(params.get("short", 5))
        long = int(params.get("long", 20))
        direction = params.get("direction", "above")
        if short >= long or len(bars) < long + 1:
            return Evaluation(False, False, None, None, "均线周期或历史数据不足")
        closes = [float(bar["close"]) for bar in bars]
        short_ma = moving_average(closes, short)
        long_ma = moving_average(closes, long)
        prev_diff = float(short_ma[-2]) - float(long_ma[-2])
        diff = float(short_ma[-1]) - float(long_ma[-1])
        active = diff >= 0 if direction == "above" else diff <= 0
        crossed = prev_diff < 0 <= diff if direction == "above" else prev_diff > 0 >= diff
        label = "上穿" if direction == "above" else "下穿"
        return Evaluation(active, crossed, diff, 0.0, f"MA{short} {label} MA{long}，差值 {diff:.3f}")

    if kind == "breakout":
        lookback = int(params.get("lookback", 20))
        direction = params.get("direction", "high")
        if len(bars) < lookback + 1:
            return Evaluation(False, False, None, None, "区间历史数据不足")
        current = bars[-1]
        previous = bars[-lookback - 1 : -1]
        if direction == "high":
            value = float(current["close"])
            threshold = max(float(bar["high"]) for bar in previous)
            active = value > threshold
            message = f"收盘价 {value:.2f} 突破前 {lookback} 日高点 {threshold:.2f}"
        else:
            value = float(current["close"])
            threshold = min(float(bar["low"]) for bar in previous)
            active = value < threshold
            message = f"收盘价 {value:.2f} 跌破前 {lookback} 日低点 {threshold:.2f}"
        return Evaluation(active, active and not previous_active, value, threshold, message)

    if kind == "volume_surge":
        window = int(params.get("window", 5))
        multiple = float(params.get("multiple", 2))
        if len(bars) < window + 1:
            return Evaluation(False, False, None, None, "成交量历史数据不足")
        current_volume = float(bars[-1]["volume"])
        previous = [float(bar["volume"]) for bar in bars[-window - 1 : -1]]
        average = sum(previous) / len(previous)
        ratio = current_volume / average if average else 0.0
        active = ratio >= multiple
        return Evaluation(active, active and not previous_active, ratio, multiple, f"成交量为前 {window} 日均量的 {ratio:.2f} 倍")

    if kind == "rsi_threshold":
        period = int(params.get("period", 14))
        direction = params.get("direction", "above")
        threshold = float(params.get("threshold", 70 if direction == "above" else 30))
        if len(bars) <= period:
            return Evaluation(False, False, None, threshold, "RSI 历史数据不足")
        values = relative_strength_index([float(bar["close"]) for bar in bars], period)
        value = values[-1]
        if value is None:
            return Evaluation(False, False, None, threshold, "RSI 历史数据不足")
        active = value >= threshold if direction == "above" else value <= threshold
        relation = "高于" if direction == "above" else "低于"
        state = relation if active else f"未{relation}"
        return Evaluation(active, active and not previous_active, value, threshold, f"RSI({period}) {value:.2f} {state} {threshold:.2f}")

    if kind == "macd_cross":
        short = int(params.get("short", 12))
        long = int(params.get("long", 26))
        signal = int(params.get("signal", 9))
        direction = params.get("direction", "above")
        if len(bars) < long + signal:
            return Evaluation(False, False, None, 0.0, "MACD 历史数据不足")
        dif, dea, _ = moving_average_convergence_divergence(
            [float(bar["close"]) for bar in bars], short, long, signal,
        )
        previous = dif[-2] - dea[-2]
        current = dif[-1] - dea[-1]
        active = current >= 0 if direction == "above" else current <= 0
        crossed = previous < 0 <= current if direction == "above" else previous > 0 >= current
        label = "金叉" if direction == "above" else "死叉"
        return Evaluation(active, crossed, current, 0.0, f"MACD {label}，DIF−DEA 为 {current:.3f}")

    if kind == "breakout_volume":
        breakout = evaluate_alert(
            {"type": "breakout", "params": {
                "lookback": params.get("lookback", 20),
                "direction": params.get("direction", "high"),
            }},
            quote,
            bars,
        )
        volume = evaluate_alert(
            {"type": "volume_surge", "params": {
                "window": params.get("window", 5),
                "multiple": params.get("multiple", 2),
            }},
            quote,
            bars,
        )
        if breakout.value is None or volume.value is None:
            return Evaluation(False, False, None, None, f"组合规则数据不足：{breakout.message}；{volume.message}")
        active = breakout.active and volume.active
        message = f"组合条件{'同时成立' if active else '未同时成立'}：{breakout.message}；{volume.message}"
        return Evaluation(active, active and not previous_active, volume.value, volume.threshold, message)

    return Evaluation(False, False, None, None, "不支持的预警类型")


def backtest_alert(rule: dict[str, Any], bars: list[dict[str, Any]], horizons: tuple[int, ...] = (1, 5, 20)) -> dict[str, Any]:
    """Replay a rule on completed daily bars without using future data to decide events."""
    clean_bars = [dict(bar) for bar in bars if bar.get("close") is not None and bar.get("date")]
    events: list[dict[str, Any]] = []
    previous_active = False
    for index, bar in enumerate(clean_bars):
        history = clean_bars[: index + 1]
        quote = {"price": float(bar["close"]), "timestamp": bar.get("date"), "source": bar.get("source")}
        result = evaluate_alert(rule, quote, history, previous_active)
        previous_active = result.active
        if not result.triggered:
            continue
        event = {
            "date": str(bar["date"]), "close": float(bar["close"]),
            "value": result.value, "threshold": result.threshold, "message": result.message,
            "forward_returns": {},
        }
        for horizon in horizons:
            target = index + horizon
            event["forward_returns"][str(horizon)] = (
                (float(clean_bars[target]["close"]) / float(bar["close"]) - 1.0) * 100.0
                if target < len(clean_bars) and float(bar["close"]) != 0 else None
            )
        events.append(event)

    stats: dict[str, dict[str, float | int | None]] = {}
    for horizon in horizons:
        values = [
            float(event["forward_returns"][str(horizon)]) for event in events
            if event["forward_returns"][str(horizon)] is not None
        ]
        stats[str(horizon)] = {
            "samples": len(values),
            "average_pct": sum(values) / len(values) if values else None,
            "median_pct": median(values) if values else None,
            "positive_pct": sum(value > 0 for value in values) / len(values) * 100.0 if values else None,
            "minimum_pct": min(values) if values else None,
            "maximum_pct": max(values) if values else None,
        }
    return {
        "bar_count": len(clean_bars), "event_count": len(events), "events": events,
        "stats": stats,
        "basis": "按已完成日K逐日回放；每次只使用当日及更早数据。",
        "caveat": "价格阈值按历史日收盘价近似，无法重建盘中穿越；结果仅用于检查规则行为，不代表未来收益。",
    }
