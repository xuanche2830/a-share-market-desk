import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from analysis import analyze_market_snapshot, analyze_security, assess_reversal, detect_candlestick_patterns
from indicators import enrich_bars, evaluate_alert


def bar(day, opened, high, low, closed, volume=1000):
    return {"date": f"2026-01-{day:02d}", "open": opened, "high": high, "low": low, "close": closed, "volume": volume}


class TechnicalAnalysisTests(unittest.TestCase):
    def test_doji_is_identified_from_body_ratio(self):
        result = detect_candlestick_patterns([bar(1, 10.0, 11.0, 9.0, 10.1)])
        self.assertEqual(result["direction"], "doji")
        self.assertIn("doji", result["pattern_keys"])

    def test_bullish_engulfing_requires_body_coverage(self):
        result = detect_candlestick_patterns([
            bar(1, 11.0, 11.2, 8.8, 9.0),
            bar(2, 8.5, 12.0, 8.0, 11.5),
        ])
        self.assertIn("bullish_engulfing", result["pattern_keys"])

    def test_reversal_candidate_requires_a_later_completed_close_to_confirm(self):
        pending = assess_reversal([
            bar(1, 11.0, 11.2, 8.8, 9.0),
            bar(2, 8.5, 12.0, 8.0, 11.5),
        ])
        confirmed = assess_reversal([
            bar(1, 11.0, 11.2, 8.8, 9.0),
            bar(2, 8.5, 12.0, 8.0, 11.5),
            bar(3, 11.6, 12.8, 11.4, 12.4),
        ])
        self.assertEqual(pending["state"], "pending_up")
        self.assertEqual(confirmed["state"], "confirmed_up")
        self.assertEqual(confirmed["confirmation_date"], "2026-01-03")
        self.assertIn("不等于趋势已经反转", confirmed["caveat"])

    def test_reversal_candidate_is_marked_invalid_after_opposite_close(self):
        result = assess_reversal([
            bar(1, 11.0, 11.2, 8.8, 9.0),
            bar(2, 8.5, 12.0, 8.0, 11.5),
            bar(3, 9.0, 9.2, 7.4, 7.8),
        ])
        self.assertEqual(result["state"], "invalidated_up")
        self.assertEqual(result["invalidation_date"], "2026-01-03")

    def test_rising_series_is_classified_as_uptrend_with_explanation(self):
        bars = enrich_bars([
            bar(index, float(index), float(index) + 0.8, float(index) - 0.8, float(index) + 0.5)
            for index in range(1, 81)
        ])
        result = analyze_security(bars)
        self.assertTrue(result["available"])
        self.assertEqual(result["trend"]["state"], "up")
        self.assertIn("MA5 > MA20 > MA60", result["evidence"])
        self.assertTrue(result["suggestions"])

    def test_trend_and_pattern_rules_trigger_only_on_entry(self):
        rising = [
            bar(index, float(index), float(index) + 0.8, float(index) - 0.8, float(index) + 0.5)
            for index in range(1, 81)
        ]
        trend = evaluate_alert({"type": "trend_state", "params": {"state": "up"}}, None, rising)
        repeated = evaluate_alert({"type": "trend_state", "params": {"state": "up"}}, None, rising, previous_active=True)
        pattern_bars = [bar(1, 11, 11.2, 8.8, 9), bar(2, 8.5, 12, 8, 11.5)]
        pattern = evaluate_alert({"type": "candlestick_pattern", "params": {"pattern": "bullish_engulfing"}}, None, pattern_bars)
        self.assertTrue(trend.triggered)
        self.assertFalse(repeated.triggered)
        self.assertTrue(pattern.triggered)

    def test_market_snapshot_is_explicitly_limited_to_headline_indices(self):
        result = analyze_market_snapshot([
            {"name": "上证指数", "change_pct": 0.8},
            {"name": "深证成指", "change_pct": 1.1},
            {"name": "创业板指", "change_pct": 0.5},
        ])
        self.assertEqual(result["state"], "broad_up")
        self.assertIn("涨跌家数", result["suggestion"])

    def test_market_snapshot_prefers_real_breadth_when_available(self):
        result = analyze_market_snapshot(
            [{"name": "上证指数", "change_pct": 0.8}],
            {"available": True, "up": 3200, "down": 1200, "flat": 50},
        )
        self.assertEqual(result["basis"], "market_breadth")
        self.assertEqual(result["state"], "breadth_up")
        self.assertIn("上涨 3200 家", result["evidence"])
        self.assertIn("不含北交所", result["suggestion"])


if __name__ == "__main__":
    unittest.main()
