from __future__ import annotations

import math
from typing import Any


TREND_LABELS = {
    "up": "上升结构",
    "down": "下降结构",
    "transition_up": "可能转强",
    "transition_down": "可能转弱",
    "range": "震荡整理",
    "insufficient": "数据不足",
}

PATTERN_LABELS = {
    "doji": "十字线",
    "long_bullish": "长实体阳线",
    "long_bearish": "长实体阴线",
    "hammer": "锤头形态",
    "shooting_star": "长上影形态",
    "bullish_engulfing": "阳包阴",
    "bearish_engulfing": "阴包阳",
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ohlc(bar: dict[str, Any]) -> tuple[float, float, float, float] | None:
    values = tuple(_number(bar.get(key)) for key in ("open", "high", "low", "close"))
    if any(value is None for value in values):
        return None
    opened, high, low, closed = values
    if high < low or high < max(opened, closed) or low > min(opened, closed):
        return None
    return opened, high, low, closed


def _candle_shape(bar: dict[str, Any]) -> dict[str, Any] | None:
    values = _ohlc(bar)
    if not values:
        return None
    opened, high, low, closed = values
    span = max(high - low, 0.0)
    body = abs(closed - opened)
    upper = high - max(opened, closed)
    lower = min(opened, closed) - low
    body_ratio = body / span if span else 0.0
    direction = "doji" if span == 0 or body_ratio <= 0.10 else "bullish" if closed > opened else "bearish"
    return {
        "direction": direction,
        "label": {"bullish": "阳线", "bearish": "阴线", "doji": "十字线"}[direction],
        "body_ratio": round(body_ratio, 4),
        "upper_ratio": round(upper / span, 4) if span else 0.0,
        "lower_ratio": round(lower / span, 4) if span else 0.0,
        "open": opened,
        "high": high,
        "low": low,
        "close": closed,
    }


def detect_candlestick_patterns(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {"available": False, "label": "等待日K", "patterns": [], "explanation": "没有可用的已完成日K"}
    current = _candle_shape(bars[-1])
    if not current:
        return {"available": False, "label": "数据异常", "patterns": [], "explanation": "最新日K缺少有效开高低收"}

    patterns: list[dict[str, str]] = []
    body = abs(current["close"] - current["open"])
    span = max(current["high"] - current["low"], 1e-12)
    upper = current["high"] - max(current["open"], current["close"])
    lower = min(current["open"], current["close"]) - current["low"]
    recent_closes = [_number(item.get("close")) for item in bars[-6:-1]]
    recent_closes = [value for value in recent_closes if value is not None]
    short_move = recent_closes[-1] - recent_closes[0] if len(recent_closes) >= 2 else 0.0

    def add(key: str, bias: str, explanation: str) -> None:
        if not any(item["key"] == key for item in patterns):
            patterns.append({"key": key, "label": PATTERN_LABELS[key], "bias": bias, "explanation": explanation})

    if current["direction"] == "doji":
        add("doji", "neutral", "实体不超过当日振幅的 10%，多空收盘接近平衡")
    if current["direction"] == "bullish" and current["body_ratio"] >= 0.60:
        add("long_bullish", "positive", "阳线实体占当日振幅至少 60%，当日收盘力量相对集中")
    if current["direction"] == "bearish" and current["body_ratio"] >= 0.60:
        add("long_bearish", "negative", "阴线实体占当日振幅至少 60%，当日收盘压力相对集中")
    if short_move < 0 and lower >= max(body * 2.0, span * 0.35) and upper <= max(body, span * 0.15):
        add("hammer", "watch_positive", "此前短线回落，最新K线下影至少约为实体两倍；只表示下方出现承接")
    if short_move > 0 and upper >= max(body * 2.0, span * 0.35) and lower <= max(body, span * 0.15):
        add("shooting_star", "watch_negative", "此前短线上行，最新K线上影至少约为实体两倍；只表示上方出现压力")

    if len(bars) >= 2:
        previous = _candle_shape(bars[-2])
        if previous:
            covers = current["open"] <= previous["close"] and current["close"] >= previous["open"]
            if previous["direction"] == "bearish" and current["direction"] == "bullish" and covers:
                add("bullish_engulfing", "watch_positive", "最新阳线实体覆盖前一根阴线实体，需要后续收盘确认")
            covers = current["open"] >= previous["close"] and current["close"] <= previous["open"]
            if previous["direction"] == "bullish" and current["direction"] == "bearish" and covers:
                add("bearish_engulfing", "watch_negative", "最新阴线实体覆盖前一根阳线实体，需要后续收盘确认")

    explanation = (
        f"{current['label']}，实体占当日振幅 {current['body_ratio'] * 100:.0f}%"
        f"，上影 {current['upper_ratio'] * 100:.0f}%、下影 {current['lower_ratio'] * 100:.0f}%"
    )
    return {
        "available": True,
        "date": bars[-1].get("date"),
        "label": current["label"],
        "direction": current["direction"],
        "features": current,
        "patterns": patterns,
        "pattern_keys": [item["key"] for item in patterns],
        "explanation": explanation,
    }


def _trend_state(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if len(bars) < 20:
        return {"state": "insufficient", "label": TREND_LABELS["insufficient"], "evidence": ["至少需要 20 根已完成日K"]}
    last = bars[-1]
    close = _number(last.get("close"))
    ma5, ma20, ma60 = (_number(last.get(key)) for key in ("ma5", "ma20", "ma60"))
    earlier_ma20 = _number(bars[-6].get("ma20")) if len(bars) >= 25 else None
    if close is None or ma5 is None or ma20 is None:
        return {"state": "insufficient", "label": TREND_LABELS["insufficient"], "evidence": ["均线数据不足"]}

    slope20 = ((ma20 / earlier_ma20 - 1.0) * 100.0) if earlier_ma20 not in (None, 0) else None
    evidence: list[str] = []
    conflicts: list[str] = []
    if ma60 is not None and ma5 > ma20 > ma60 and close > ma20 and (slope20 or 0) > 0:
        state = "up"
        evidence.extend(["MA5 > MA20 > MA60", "收盘位于 MA20 上方", "MA20 最近 5 个交易日向上"])
        invalidation = f"若后续收盘跌回 MA20（当前约 {ma20:.2f}）下方，结构条件将减弱"
    elif ma60 is not None and ma5 < ma20 < ma60 and close < ma20 and (slope20 or 0) < 0:
        state = "down"
        evidence.extend(["MA5 < MA20 < MA60", "收盘位于 MA20 下方", "MA20 最近 5 个交易日向下"])
        invalidation = f"若后续收盘重返 MA20（当前约 {ma20:.2f}）上方且均线转平，结构条件将减弱"
    elif close > ma20 and ma5 > ma20 and (slope20 or 0) > 0:
        state = "transition_up"
        evidence.extend(["收盘位于 MA20 上方", "MA5 已高于 MA20", "MA20 最近 5 个交易日向上"])
        if ma60 is not None and ma20 <= ma60:
            conflicts.append("MA20 尚未高于 MA60，长期结构仍未同步")
        invalidation = f"观察收盘能否持续站在 MA20（当前约 {ma20:.2f}）上方"
    elif close < ma20 and ma5 < ma20 and (slope20 or 0) < 0:
        state = "transition_down"
        evidence.extend(["收盘位于 MA20 下方", "MA5 已低于 MA20", "MA20 最近 5 个交易日向下"])
        if ma60 is not None and ma20 >= ma60:
            conflicts.append("MA20 尚未低于 MA60，长期结构仍未同步")
        invalidation = f"观察收盘能否重新站回 MA20（当前约 {ma20:.2f}）上方"
    else:
        state = "range"
        evidence.append("价格与均线没有形成一致的多周期排列")
        if slope20 is not None:
            evidence.append(f"MA20 最近 5 个交易日变化 {slope20:+.2f}%")
        invalidation = "等待价格与 MA5、MA20、MA60 出现一致方向后再判断结构变化"

    return {
        "state": state,
        "label": TREND_LABELS[state],
        "evidence": evidence,
        "conflicts": conflicts,
        "ma20_slope_5d_pct": round(slope20, 4) if slope20 is not None else None,
        "invalidation": invalidation,
    }


def assess_reversal(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Track a recent reversal-shaped candle through price confirmation or invalidation.

    Confirmation here has a deliberately narrow meaning: a later completed
    daily close crossed the candidate candle's high/low.  It is not a claim
    that a durable market reversal has occurred.
    """
    if len(bars) < 2:
        return {"available": False, "state": "insufficient", "label": "转折数据不足", "message": "至少需要两根已完成日K"}
    positive = {"hammer", "bullish_engulfing"}
    negative = {"shooting_star", "bearish_engulfing"}
    start = max(0, len(bars) - 6)
    for index in range(len(bars) - 1, start - 1, -1):
        candidate = detect_candlestick_patterns(bars[:index + 1])
        keys = set(candidate.get("pattern_keys") or [])
        direction = "up" if keys & positive else "down" if keys & negative else None
        if not direction:
            continue
        features = candidate.get("features") or {}
        high, low = _number(features.get("high")), _number(features.get("low"))
        if high is None or low is None:
            continue
        pattern_key = next((key for key in candidate.get("pattern_keys", []) if key in (positive if direction == "up" else negative)), None)
        pattern_label = PATTERN_LABELS.get(pattern_key or "", "形态")
        later = bars[index + 1:]
        confirmation_date = None
        invalidation_date = None
        for item in later:
            close = _number(item.get("close"))
            if close is None:
                continue
            if direction == "up":
                if close < low:
                    invalidation_date = item.get("date")
                    break
                if confirmation_date is None and close > high:
                    confirmation_date = item.get("date")
            else:
                if close > high:
                    invalidation_date = item.get("date")
                    break
                if confirmation_date is None and close < low:
                    confirmation_date = item.get("date")
        candidate_date = candidate.get("date")
        if invalidation_date:
            state = f"invalidated_{direction}"
            label = "向上候选已失效" if direction == "up" else "向下候选已失效"
            message = f"{candidate_date} 的{pattern_label}已在 {invalidation_date} 被反向收盘越界"
        elif confirmation_date:
            state = f"confirmed_{direction}"
            label = "向上转折获价格确认" if direction == "up" else "向下转折获价格确认"
            threshold = high if direction == "up" else low
            verb = "高于形态高点" if direction == "up" else "低于形态低点"
            message = f"{candidate_date} 的{pattern_label}后，{confirmation_date} 收盘{verb} {threshold:.2f}"
        else:
            state = f"pending_{direction}"
            label = "等待向上确认" if direction == "up" else "等待向下确认"
            threshold = high if direction == "up" else low
            verb = "高于形态高点" if direction == "up" else "低于形态低点"
            message = f"{candidate_date} 出现{pattern_label}，等待后续已完成日K收盘{verb} {threshold:.2f}"
        return {
            "available": True,
            "state": state,
            "label": label,
            "direction": direction,
            "pattern": pattern_key,
            "pattern_label": pattern_label,
            "candidate_date": candidate_date,
            "confirmation_date": confirmation_date,
            "invalidation_date": invalidation_date,
            "confirmation_level": high if direction == "up" else low,
            "invalidation_level": low if direction == "up" else high,
            "message": message,
            "caveat": "这里只确认价格越过形态边界，不等于趋势已经反转。",
        }
    return {
        "available": True,
        "state": "none",
        "label": "近期无转折候选",
        "message": "最近 6 根已完成日K没有出现受支持的锤头、长上影、阳包阴或阴包阳候选",
        "caveat": "没有形态候选不代表风险或机会不存在。",
    }


def analyze_security(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {"available": False, "headline": "等待历史数据", "signals": [], "suggestions": [], "signature": "empty"}
    candle = detect_candlestick_patterns(bars)
    trend = _trend_state(bars)
    reversal = assess_reversal(bars)
    last = bars[-1]
    signals: list[dict[str, str]] = []
    evidence = list(trend.get("evidence") or [])
    conflicts = list(trend.get("conflicts") or [])

    for pattern in candle.get("patterns", []):
        severity = "attention"
        signals.append({"key": pattern["key"], "category": "kline", "severity": severity, "title": pattern["label"], "message": pattern["explanation"]})

    if reversal.get("state") in {"confirmed_up", "confirmed_down"}:
        signals.append({
            "key": str(reversal["state"]), "category": "reversal", "severity": "attention",
            "title": str(reversal["label"]), "message": f"{reversal['message']}；{reversal['caveat']}",
        })
        aligned = (
            reversal["state"] == "confirmed_up" and trend.get("state") in {"up", "transition_up"}
        ) or (
            reversal["state"] == "confirmed_down" and trend.get("state") in {"down", "transition_down"}
        )
        (evidence if aligned else conflicts).append(
            "价格确认方向与当前均线结构一致" if aligned else "价格已越过形态边界，但均线结构尚未同向确认"
        )

    rsi = _number(last.get("rsi14"))
    if rsi is not None:
        evidence.append(f"RSI(14) 为 {rsi:.2f}")
        if rsi >= 70:
            signals.append({"key": "rsi_high", "category": "momentum", "severity": "attention", "title": "RSI 进入相对高位", "message": "高位描述近期涨幅集中，不等同于见顶"})
        elif rsi <= 30:
            signals.append({"key": "rsi_low", "category": "momentum", "severity": "attention", "title": "RSI 进入相对低位", "message": "低位描述近期跌幅集中，不等同于见底"})

    dif = _number(last.get("macd_dif"))
    dea = _number(last.get("macd_dea"))
    if len(bars) >= 2 and dif is not None and dea is not None:
        previous_dif = _number(bars[-2].get("macd_dif"))
        previous_dea = _number(bars[-2].get("macd_dea"))
        if previous_dif is not None and previous_dea is not None:
            before, current = previous_dif - previous_dea, dif - dea
            if before < 0 <= current:
                signals.append({"key": "macd_golden", "category": "momentum", "severity": "attention", "title": "MACD 金叉", "message": "最近两根已完成日K之间 DIF 上穿 DEA"})
            elif before > 0 >= current:
                signals.append({"key": "macd_dead", "category": "momentum", "severity": "attention", "title": "MACD 死叉", "message": "最近两根已完成日K之间 DIF 下穿 DEA"})
            evidence.append(f"MACD 当前 DIF {'高于' if current >= 0 else '低于'} DEA")

    previous = bars[-21:-1]
    close = _number(last.get("close"))
    valid_lows = [value for item in previous if (value := _number(item.get("low"))) is not None]
    valid_highs = [value for item in previous if (value := _number(item.get("high"))) is not None]
    support = min(valid_lows) if valid_lows else None
    resistance = max(valid_highs) if valid_highs else None
    if close is not None and support is not None and resistance is not None:
        if close > resistance:
            signals.append({"key": "breakout_20", "category": "level", "severity": "attention", "title": "收盘突破前20日高点", "message": f"收盘 {close:.2f} 高于此前区间高点 {resistance:.2f}"})
        elif close < support:
            signals.append({"key": "breakdown_20", "category": "level", "severity": "attention", "title": "收盘跌破前20日低点", "message": f"收盘 {close:.2f} 低于此前区间低点 {support:.2f}"})

    positive_patterns = [item for item in candle.get("patterns", []) if item["bias"] == "watch_positive"]
    negative_patterns = [item for item in candle.get("patterns", []) if item["bias"] == "watch_negative"]
    suggestions = [trend.get("invalidation")]
    if reversal.get("state", "").startswith("pending_"):
        suggestions.append(str(reversal.get("message")))
    elif reversal.get("state", "").startswith("confirmed_"):
        suggestions.append(f"{reversal.get('message')}；继续观察是否守住确认边界，不能据此推断持续上涨或下跌")
    elif reversal.get("state", "").startswith("invalidated_"):
        suggestions.append(str(reversal.get("message")))
    if positive_patterns:
        suggestions.append(f"等待下一根已完成日K收盘高于形态高点 {candle['features']['high']:.2f}，再确认承接是否延续")
    if negative_patterns:
        suggestions.append(f"观察下一根已完成日K是否跌破形态低点 {candle['features']['low']:.2f}，以判断压力是否延续")
    if not positive_patterns and not negative_patterns:
        suggestions.append("优先观察趋势条件是否改变，不把单根阴线或阳线直接解释为反转")
    suggestions.append("将提示作为复核清单，不据此单独作出买卖决定")

    attention = "high" if any(item["severity"] == "attention" for item in signals) else "normal"
    confidence = "证据较一致" if len(evidence) >= 3 and not conflicts else "证据混合" if conflicts else "证据有限"
    pattern_keys = candle.get("pattern_keys", [])
    signal_keys = [item["key"] for item in signals]
    signature = "|".join([str(last.get("date") or ""), trend["state"], str(reversal.get("state") or ""), ",".join(pattern_keys), ",".join(signal_keys)])
    return {
        "available": True,
        "as_of": last.get("date"),
        "headline": f"{trend['label']} · {candle.get('label', 'K线待判断')}",
        "attention": attention,
        "confidence": confidence,
        "trend": trend,
        "candle": candle,
        "reversal": reversal,
        "signals": signals,
        "evidence": evidence[:6],
        "conflicts": conflicts[:4],
        "levels": {"support_20": support, "resistance_20": resistance},
        "suggestions": [item for item in suggestions if item],
        "signature": signature,
        "basis": "仅使用已完成的前复权日K、均线、RSI、MACD和成交量；形态与确认阈值为固定规则。",
        "caveat": "这是行情观察解释；价格确认只表示收盘越过候选形态边界，不是收益预测、买卖建议或持久反转证明。",
    }


def analyze_market_snapshot(items: list[dict[str, Any]], breadth: dict[str, Any] | None = None) -> dict[str, Any]:
    changes = [(str(item.get("name") or item.get("symbol") or "指数"), _number(item.get("change_pct"))) for item in items]
    changes = [(name, value) for name, value in changes if value is not None]
    if breadth and breadth.get("available"):
        up = int(breadth.get("up") or 0)
        down = int(breadth.get("down") or 0)
        flat = int(breadth.get("flat") or 0)
        directional = up + down
        ratio = up / directional * 100 if directional else 0.0
        if ratio >= 65:
            state, label = "breadth_up", "沪深个股涨多跌少"
        elif ratio <= 35:
            state, label = "breadth_down", "沪深个股跌多涨少"
        else:
            state, label = "breadth_balanced", "沪深个股涨跌较均衡"
        index_evidence = [f"{name} {value:+.2f}%" for name, value in changes]
        return {
            "available": True,
            "basis": "market_breadth",
            "state": state,
            "label": label,
            "advance_ratio": round(ratio, 2),
            "evidence": [f"上涨 {up} 家", f"下跌 {down} 家", f"平盘 {flat} 家"] + index_evidence,
            "suggestion": "涨跌家数反映当日横截面强弱；当前范围为沪深两市、不含北交所，也不代表后续走势。",
        }
    if not changes:
        return {"available": False, "label": "大盘数据不足", "evidence": [], "suggestion": "等待指数快照恢复"}
    up = sum(value > 0.1 for _, value in changes)
    down = sum(value < -0.1 for _, value in changes)
    average = sum(value for _, value in changes) / len(changes)
    if up >= 2 and down == 0:
        state, label = "broad_up", "主要指数同步偏强"
    elif down >= 2 and up == 0:
        state, label = "broad_down", "主要指数同步偏弱"
    elif up and down:
        state, label = "divergent", "主要指数表现分化"
    else:
        state, label = "flat", "主要指数窄幅整理"
    evidence = [f"{name} {value:+.2f}%" for name, value in changes]
    return {
        "available": True,
        "state": state,
        "basis": "headline_indices",
        "label": label,
        "average_change_pct": round(average, 4),
        "evidence": evidence,
        "suggestion": "该结论只反映主要指数当日快照；判断全市场情绪还需要涨跌家数、涨停跌停和行业广度。",
    }
