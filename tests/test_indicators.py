import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from indicators import (
    backtest_alert,
    enrich_bars,
    evaluate_alert,
    exponential_moving_average,
    moving_average,
    moving_average_convergence_divergence,
    relative_strength_index,
)
from providers import normalize_symbol


class IndicatorTests(unittest.TestCase):
    def test_moving_average(self):
        self.assertEqual(moving_average([1, 2, 3, 4], 3), [None, None, 2.0, 3.0])

    def test_exponential_moving_average_uses_first_value_seed(self):
        values = exponential_moving_average([1, 2, 3], 3)
        self.assertEqual(values, [1.0, 1.5, 2.25])

    def test_wilder_rsi_boundaries_and_flat_series(self):
        rising = relative_strength_index(range(1, 16), 14)
        falling = relative_strength_index(range(15, 0, -1), 14)
        flat = relative_strength_index([7] * 15, 14)
        self.assertIsNone(rising[13])
        self.assertEqual(rising[14], 100.0)
        self.assertEqual(falling[14], 0.0)
        self.assertEqual(flat[14], 50.0)

    def test_macd_uses_double_histogram_convention(self):
        dif, dea, histogram = moving_average_convergence_divergence([1, 2])
        self.assertAlmostEqual(dif[1], 0.0797720798, places=9)
        self.assertAlmostEqual(dea[1], 0.0159544160, places=9)
        self.assertAlmostEqual(histogram[1], 0.1276353276, places=9)

    def test_enrich_bars_adds_rsi_and_macd_fields(self):
        bars = [{"close": value} for value in range(1, 31)]
        enriched = enrich_bars(bars)
        self.assertEqual(enriched[-1]["rsi14"], 100.0)
        self.assertIn("macd_dif", enriched[-1])
        self.assertIn("macd_dea", enriched[-1])
        self.assertIn("macd_hist", enriched[-1])

    def test_price_cross_only_triggers_on_state_change(self):
        rule = {"type": "price_above", "params": {"threshold": 10}}
        first = evaluate_alert(rule, {"price": 10.5}, [])
        repeated = evaluate_alert(rule, {"price": 10.5}, [], previous_active=True)
        inactive = evaluate_alert(rule, {"price": 9.5}, [])
        self.assertTrue(first.triggered)
        self.assertFalse(repeated.triggered)
        self.assertFalse(inactive.active)
        self.assertEqual(inactive.message, "最新价 9.50 未高于 10.00")

    def test_breakout_ignores_current_bar_in_threshold(self):
        bars = [
            {"close": 9, "high": 10, "low": 8, "volume": 100},
            {"close": 10, "high": 11, "low": 9, "volume": 100},
            {"close": 12, "high": 12, "low": 10, "volume": 100},
        ]
        result = evaluate_alert({"type": "breakout", "params": {"lookback": 2, "direction": "high"}}, None, bars)
        self.assertTrue(result.triggered)
        self.assertEqual(result.threshold, 11)

    def test_volume_surge(self):
        bars = [{"close": 1, "high": 1, "low": 1, "volume": value} for value in [100, 100, 100, 220]]
        result = evaluate_alert({"type": "volume_surge", "params": {"window": 3, "multiple": 2}}, None, bars)
        self.assertTrue(result.triggered)

    def test_rsi_threshold_triggers_on_entry_only(self):
        bars = [{"close": value} for value in range(1, 16)]
        rule = {"type": "rsi_threshold", "params": {"period": 14, "direction": "above", "threshold": 70}}
        entered = evaluate_alert(rule, None, bars)
        repeated = evaluate_alert(rule, None, bars, previous_active=True)
        self.assertTrue(entered.active)
        self.assertTrue(entered.triggered)
        self.assertFalse(repeated.triggered)
        self.assertIn("100.00", entered.message)

    def test_macd_cross_detects_latest_golden_cross(self):
        closes = list(range(100, 60, -1)) + [65]
        bars = [{"close": value} for value in closes]
        result = evaluate_alert(
            {"type": "macd_cross", "params": {"short": 12, "long": 26, "signal": 9, "direction": "above"}},
            None,
            bars,
        )
        self.assertTrue(result.triggered)
        self.assertIn("金叉", result.message)

    def test_breakout_volume_requires_both_conditions(self):
        previous = [{"close": 9, "high": 10, "low": 8, "volume": 100} for _ in range(20)]
        rule = {"type": "breakout_volume", "params": {"lookback": 20, "direction": "high", "window": 5, "multiple": 2}}
        matched = evaluate_alert(rule, None, previous + [{"close": 11, "high": 11, "low": 9, "volume": 250}])
        no_volume = evaluate_alert(rule, None, previous + [{"close": 11, "high": 11, "low": 9, "volume": 150}])
        self.assertTrue(matched.triggered)
        self.assertFalse(no_volume.active)
        self.assertIn("同时成立", matched.message)

    def test_symbol_normalization(self):
        self.assertEqual(normalize_symbol("600519"), "600519.SH")
        self.assertEqual(normalize_symbol("000001.sz"), "000001.SZ")
        with self.assertRaises(ValueError):
            normalize_symbol("ABC")

    def test_backtest_uses_only_past_data_and_computes_forward_returns(self):
        bars = [
            {"date": f"2026-01-{day:02d}", "close": close, "high": close, "low": close, "volume": 100}
            for day, close in enumerate([9, 11, 12, 8, 13, 14], 1)
        ]
        result = backtest_alert({"type": "price_above", "params": {"threshold": 10}}, bars, (1,))
        self.assertEqual([item["date"] for item in result["events"]], ["2026-01-02", "2026-01-05"])
        self.assertAlmostEqual(result["events"][0]["forward_returns"]["1"], (12 / 11 - 1) * 100)
        self.assertAlmostEqual(result["events"][1]["forward_returns"]["1"], (14 / 13 - 1) * 100)
        self.assertEqual(result["stats"]["1"]["samples"], 2)

    def test_backtest_keeps_incomplete_forward_horizon_null(self):
        bars = [{"date": str(index), "close": value, "high": value, "low": value, "volume": 100} for index, value in enumerate([9, 11], 1)]
        result = backtest_alert({"type": "price_above", "params": {"threshold": 10}}, bars, (5,))
        self.assertIsNone(result["events"][0]["forward_returns"]["5"])
        self.assertEqual(result["stats"]["5"]["samples"], 0)


if __name__ == "__main__":
    unittest.main()
