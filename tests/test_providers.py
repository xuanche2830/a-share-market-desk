import os
import sys
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from providers import (
    AutoProvider,
    DataSourceError,
    EastmoneyProvider,
    HithinkProvider,
    HithinkApiError,
    PublicProvider,
    TencentProvider,
    company_overview,
    diagnose_hithink,
    get_provider,
    market_overview,
    market_breadth,
    search_symbols,
)
import providers


class ProviderTests(unittest.TestCase):
    @patch("providers._get_text")
    def test_symbol_search_filters_non_a_share_results(self, get_text):
        get_text.return_value = (
            'v_hint="sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A^'
            'hk~03750~\\u5b81\\u5fb7\\u65f6\\u4ee3~ndsd~GP^'
            'sz~300750~\\u5b81\\u5fb7\\u65f6\\u4ee3~ndsd~GP-A^'
            'sh~510300~ETF~etf~JJ"'
        )
        results = search_symbols("宁德", 10)
        self.assertEqual([item["symbol"] for item in results], ["600519.SH", "300750.SZ"])
        self.assertEqual(results[0]["name"], "贵州茅台")

    @patch("providers._get_text", return_value="unexpected")
    def test_symbol_search_rejects_malformed_response(self, _get_text):
        with self.assertRaisesRegex(DataSourceError, "搜索返回格式异常"):
            search_symbols("茅台")

    def test_failed_provider_enters_short_cooldown(self):
        original = deepcopy(providers._HEALTH["eastmoney"])
        calls = 0

        def fail():
            nonlocal calls
            calls += 1
            raise DataSourceError("temporary upstream failure")

        try:
            with self.assertRaisesRegex(DataSourceError, "temporary upstream failure"):
                providers._observed("eastmoney", fail)
            with self.assertRaisesRegex(DataSourceError, "暂时熔断"):
                providers._observed("eastmoney", fail)
            self.assertEqual(calls, 1)
            health = {item["source"]: item for item in providers.provider_health()}
            self.assertEqual(health["eastmoney"]["status"], "cooldown")
        finally:
            providers._HEALTH["eastmoney"].clear()
            providers._HEALTH["eastmoney"].update(original)

    @patch("providers._get_json")
    def test_hithink_parses_current_data_item_contract(self, get_json):
        get_json.return_value = {
            "code": 0,
            "message": "success",
            "request_id": "request-1",
            "data": {
                "timestamp": 1_780_000_000_000,
                "item": [{
                    "thscode": "600519.SH", "ticker": "600519", "last_price": 1500,
                    "open_price": 1490, "high_price": 1510, "low_price": 1480,
                    "prev_price": 1495, "price_change": 5, "price_change_ratio_pct": 0.3344,
                    "volume": 123400, "turnover": 185000000,
                }],
            },
        }
        quote = HithinkProvider("secret-not-for-output").quotes(["600519.SH"])[0]
        self.assertEqual(quote["symbol"], "600519.SH")
        self.assertEqual(quote["price"], 1500.0)
        self.assertEqual(quote["timestamp"], 1_780_000_000_000)
        self.assertEqual(quote["source"], "hithink")

    @patch("providers._get_json")
    def test_hithink_rejects_missing_required_price(self, get_json):
        get_json.return_value = {
            "code": 0,
            "data": {"timestamp": None, "item": [{"thscode": "600519.SH", "ticker": "600519"}]},
        }
        with self.assertRaisesRegex(DataSourceError, "last_price"):
            HithinkProvider("key").quotes(["600519.SH"])

    @patch("providers._get_json")
    def test_hithink_error_keeps_request_id_but_not_api_key(self, get_json):
        get_json.return_value = {"code": 2003, "message": "无权限", "request_id": "request-2", "data": None}
        with self.assertRaises(DataSourceError) as caught:
            HithinkProvider("super-secret-key").quotes(["600519.SH"])
        message = str(caught.exception)
        self.assertIn("request-2", message)
        self.assertNotIn("super-secret-key", message)

    @patch("providers.time.sleep")
    @patch("providers._get_json")
    def test_hithink_retries_only_retryable_business_errors(self, get_json, _sleep):
        get_json.side_effect = [
            {"code": 4001, "message": "限流", "request_id": "retry-1", "data": None},
            {"code": 0, "message": "success", "data": {"timestamp": 1, "item": [{
                "thscode": "600519.SH", "ticker": "600519", "last_price": 10,
            }]}},
        ]
        quote = HithinkProvider("secret").quotes(["600519.SH"])[0]
        self.assertEqual(quote["price"], 10.0)
        self.assertEqual(get_json.call_count, 2)
        _sleep.assert_called_once()

        get_json.reset_mock()
        get_json.side_effect = None
        get_json.return_value = {"code": 2003, "message": "无权限", "request_id": "deny-1", "data": None}
        with self.assertRaises(HithinkApiError) as caught:
            HithinkProvider("secret").quotes(["600519.SH"])
        self.assertEqual(caught.exception.code, 2003)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(get_json.call_count, 1)

    def test_hithink_diagnostic_without_key_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            result = diagnose_hithink("600519.SH")
        self.assertFalse(result["configured"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "not_configured")
        self.assertNotIn("secret", str(result).lower())

    def test_public_provider_falls_back_after_timeout(self):
        provider = PublicProvider()
        provider.tencent.quotes = Mock(side_effect=DataSourceError("timeout"))
        provider.eastmoney.quotes = Mock(return_value=[{"symbol": "600519.SH", "source": "eastmoney"}])
        items = provider.quotes(["600519.SH"])
        self.assertEqual(items[0]["source"], "eastmoney")
        provider.eastmoney.quotes.assert_called_once()

    def test_auto_provider_falls_back_when_hithink_fails(self):
        primary = HithinkProvider("key")
        primary.quotes = Mock(side_effect=DataSourceError("official unavailable"))
        provider = AutoProvider(primary)
        provider.public.quotes = Mock(return_value=[{"symbol": "600519.SH", "source": "tencent"}])
        items = provider.quotes(["600519.SH"])
        self.assertEqual(items[0]["source"], "tencent")

    def test_hithink_preference_builds_official_first_route_from_environment(self):
        with patch.dict(os.environ, {"HITHINK_FINANCE_API_KEY": "environment-only-key"}):
            provider = get_provider("hithink")
        self.assertEqual(provider.name, "hithink")
        self.assertIsInstance(provider.primary, HithinkProvider)
        self.assertEqual(provider.primary.api_key, "environment-only-key")

    @patch("providers._get_json")
    def test_tencent_history_keeps_bar_when_turnover_type_is_invalid(self, get_json):
        get_json.return_value = {
            "data": {"sh600519": {"qfqday": [["2026-09-17", "10", "11", "12", "9", "100", {"bad": True}]]}}
        }
        bar = TencentProvider().history("600519.SH", 30)[0]
        self.assertEqual(bar["volume"], 10000.0)
        self.assertIsNone(bar["turnover"])

    @patch("providers._get_json")
    def test_eastmoney_history_truncates_to_requested_limit(self, get_json):
        rows = [f"2026-09-{day:02d},10,11,12,9,100,1000" for day in range(1, 11)]
        get_json.return_value = {"data": {"klines": rows}}
        bars = EastmoneyProvider().history("600519.SH", 3)
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0]["date"], "2026-09-08")

    @patch("providers._get_json")
    def test_company_overview_parses_scaled_valuation_and_missing_optional_fields(self, get_json):
        get_json.return_value = {"data": {
            "f57": "600519", "f58": "贵州茅台", "f84": 1000, "f85": None,
            "f116": 2000, "f117": "-", "f127": "白酒", "f128": "贵州",
            "f162": 1765, "f167": 625, "f189": "20010827",
        }}
        with patch("providers._observed", side_effect=lambda _source, operation: operation()):
            item = company_overview("600519.SH")[0]
        self.assertEqual(item["pe_dynamic"], 17.65)
        self.assertEqual(item["pb"], 6.25)
        self.assertEqual(item["listing_date"], "2001-08-27")
        self.assertIsNone(item["float_shares"])

    @patch("providers._get_text")
    @patch("providers._get_json", side_effect=DataSourceError("eastmoney unavailable"))
    def test_company_overview_falls_back_to_partial_tencent_fields(self, _get_json, get_text):
        fields = [""] * 60
        fields[1], fields[44], fields[45], fields[46], fields[52] = "贵州茅台", "15715.03", "15000", "6.25", "17.65"
        get_text.return_value = "~".join(fields)
        with patch("providers._observed", side_effect=lambda _source, operation: operation()):
            item = company_overview("600519.SH")[0]
        self.assertTrue(item["partial"])
        self.assertAlmostEqual(item["total_market_cap"], 1_571_503_000_000)
        self.assertEqual(item["source"], "tencent")

    @patch("providers._get_text")
    def test_market_overview_requires_and_parses_three_indices(self, get_text):
        get_text.return_value = (
            'v_s_sh000001="1~上证指数~000001~3911.87~36.27~0.94~123";'
            'v_s_sz399001="1~深证成指~399001~12000~-20~-0.17~456";'
            'v_s_sz399006="1~创业板指~399006~3000~10~0.33~789";'
        )
        with patch("providers._observed", side_effect=lambda _source, operation: operation()):
            items = market_overview()
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["change_pct"], 0.94)
        self.assertEqual(items[2]["symbol"], "399006.SZ")

    @patch("providers._get_json")
    def test_market_breadth_combines_shanghai_and_shenzhen(self, get_json):
        get_json.return_value = {"data": {"diff": [
            {"f12": "000001", "f104": 1000, "f105": 800, "f106": 20},
            {"f12": "399001", "f104": 1400, "f105": 900, "f106": 30},
        ]}}
        with patch("providers._observed", side_effect=lambda _source, operation: operation()):
            item = market_breadth()[0]
        self.assertTrue(item["available"])
        self.assertEqual(item["up"], 2400)
        self.assertEqual(item["down"], 1700)
        self.assertAlmostEqual(item["advance_ratio"], 58.54)
        self.assertIn("不含北交所", item["coverage_note"])

    @patch("providers._get_json")
    def test_market_breadth_rejects_partial_exchange_data(self, get_json):
        get_json.return_value = {"data": {"diff": [
            {"f12": "000001", "f104": 1000, "f105": 800, "f106": 20},
        ]}}
        with patch("providers._observed", side_effect=lambda _source, operation: operation()):
            with self.assertRaisesRegex(DataSourceError, "未同时返回沪深两市"):
                market_breadth()


if __name__ == "__main__":
    unittest.main()
