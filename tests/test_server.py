import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import server
from providers import DataSourceError


class ServerReliabilityTests(unittest.TestCase):
    def test_port_fallback_range_is_bounded_and_deterministic(self):
        self.assertEqual(server.candidate_ports(8765, False), [8765])
        self.assertEqual(server.candidate_ports(8765, True, attempts=3), [8765, 8766, 8767])
        self.assertEqual(server.candidate_ports(65534, True), [65534, 65535])
        with self.assertRaises(ValueError):
            server.candidate_ports(0, True)

    def test_desktop_status_does_not_expose_machine_absolute_path(self):
        status = server.desktop_status()
        self.assertEqual(status["log_file"], "data/logs/app.log")
        self.assertEqual(status["data_location"], "application_folder")
        self.assertFalse(Path(status["log_file"]).is_absolute())

    @unittest.skipUnless(server.IS_WINDOWS, "exclusive port binding is Windows-only")
    def test_local_server_refuses_port_shared_by_another_process(self):
        occupied = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = occupied.server_address[1]
        try:
            with self.assertRaises(OSError):
                server.LocalThreadingHTTPServer(("127.0.0.1", port), server.Handler)
        finally:
            occupied.server_close()

    def test_legacy_state_import_migrates_schema_and_watchlist_metadata(self):
        imported = server.sanitize_state_document({
            "settings": {"refresh_seconds": 5, "source": "unknown"},
            "watchlist": [{"symbol": "600519", "name": "贵州茅台"}],
            "alerts": [], "records": [],
        })
        self.assertEqual(imported["schema_version"], server.STATE_VERSION)
        self.assertEqual(imported["settings"]["refresh_seconds"], 10)
        self.assertEqual(imported["settings"]["source"], "auto")
        self.assertEqual(imported["watchlist"][0]["symbol"], "600519.SH")
        self.assertEqual(imported["watchlist"][0]["group"], "默认")
        self.assertEqual(imported["watchlist"][0]["position"], 0)

    def test_import_rejects_future_schema_and_invalid_symbol(self):
        with self.assertRaisesRegex(ValueError, "高于当前支持版本"):
            server.sanitize_state_document({"schema_version": server.STATE_VERSION + 1})
        with self.assertRaises(ValueError):
            server.sanitize_state_document({"watchlist": [{"symbol": "not-a-stock"}]})

    def test_backup_creation_rotation_and_path_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "state.json"
            backup_dir = root / "backups"
            with patch.object(server, "DATA_DIR", root), patch.object(server, "STATE_FILE", state_file), patch.object(server, "BACKUP_DIR", backup_dir), patch.object(server, "MAX_BACKUPS", 3):
                server.save_state(server._fresh_default())
                for index in range(5):
                    state = server.load_state()
                    state["settings"]["refresh_seconds"] = 10 + index
                    server.save_state(state, f"test-{index}")
                backups = server.list_backups()
                self.assertEqual(len(backups), 3)
                self.assertTrue(server.backup_path(backups[0]["name"]).is_file())
                with self.assertRaises(ValueError):
                    server.backup_path("../state.json")

    def test_import_preserves_valid_alert_and_limits_records(self):
        records = [{
            "id": str(index), "symbol": "300750.SZ", "name": "宁德时代",
            "type": "price_above", "message": "测试", "triggered_at": "2026-09-19T10:00:00+08:00",
        } for index in range(240)]
        imported = server.sanitize_state_document({
            "watchlist": [{"symbol": "300750.SZ"}],
            "alerts": [{"symbol": "300750.SZ", "type": "rsi_threshold", "params": {"direction": "below", "threshold": 30}}],
            "records": records,
        })
        self.assertEqual(imported["alerts"][0]["params"]["period"], 14)
        self.assertEqual(imported["alerts"][0]["stats"]["checks"], 0)
        self.assertEqual(imported["audit"], [])
        self.assertEqual(len(imported["records"]), 200)

    def test_background_monitor_can_pause_and_resume_immediately(self):
        called = threading.Event()

        def evaluate():
            called.set()
            return {"triggered": [], "records": [], "alerts": [], "skipped": False, "idle": True}

        monitor = server.AlertMonitor()
        monitor.set_paused(True)
        with patch.object(server, "evaluate_all", side_effect=evaluate), patch.object(server, "monitor_interval", return_value=60):
            monitor.start()
            self.assertFalse(called.wait(0.1), "暂停状态仍执行了预警检查")
            monitor.set_paused(False)
            self.assertTrue(called.wait(1), "恢复后没有立即执行预警检查")
            monitor.stop()

    def test_background_monitor_dispatches_triggered_records(self):
        delivered = threading.Event()
        received = []

        def evaluate():
            return {
                "triggered": [{"id": "record-1", "name": "贵州茅台", "message": "测试触发"}],
                "records": [], "alerts": [], "skipped": False, "idle": False,
            }

        def notify(records):
            received.extend(records)
            delivered.set()

        monitor = server.AlertMonitor(on_triggered=notify)
        with patch.object(server, "evaluate_all", side_effect=evaluate), patch.object(server, "monitor_interval", return_value=60):
            monitor.start()
            self.assertTrue(delivered.wait(1), "触发记录没有交给桌面通知通道")
            monitor.stop()
        self.assertEqual(received[0]["id"], "record-1")

    def test_background_monitor_runs_without_browser_request(self):
        called = threading.Event()

        def evaluate():
            called.set()
            return {"triggered": [], "records": [], "alerts": [], "skipped": False, "idle": True}

        monitor = server.AlertMonitor()
        with patch.object(server, "evaluate_all", side_effect=evaluate), patch.object(server, "monitor_interval", return_value=60):
            monitor.start()
            self.assertTrue(called.wait(1), "后台线程没有自主执行预警检查")
            monitor.stop()
        self.assertFalse(server.monitor_status()["running"])

    def test_evaluation_keeps_rule_added_during_provider_request(self):
        initial_rule = {
            "id": "existing", "symbol": "600519.SH", "type": "price_above",
            "params": {"threshold": 10}, "enabled": True, "last_active": False,
            "once_per_day": True, "cooldown_minutes": 60, "trading_hours_only": False,
        }
        added_rule = {
            "id": "added", "symbol": "300750.SZ", "type": "price_above",
            "params": {"threshold": 1000}, "enabled": True, "last_active": False,
            "once_per_day": True, "cooldown_minutes": 60,
        }

        class Provider:
            name = "test"

            def quotes(self, _symbols):
                server.update_state(lambda state: state["alerts"].append(dict(added_rule)))
                return [{"symbol": "600519.SH", "name": "贵州茅台", "price": 11, "source": "test", "timestamp": 1}]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "state.json"
            with patch.object(server, "DATA_DIR", root), patch.object(server, "STATE_FILE", state_file):
                state = server._fresh_default()
                state["alerts"] = [initial_rule]
                state["records"] = []
                server.save_state(state)
                with patch.object(server, "get_provider", return_value=Provider()):
                    result = server.evaluate_all()
                latest = server.load_state()

        self.assertEqual({rule["id"] for rule in latest["alerts"]}, {"existing", "added"})
        self.assertEqual(len(result["triggered"]), 1)
        self.assertNotIn("last_checked_at", next(rule for rule in latest["alerts"] if rule["id"] == "added"))

    def test_cache_fallback_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "CACHE_DIR", Path(directory)):
            live = [{"symbol": "600519.SH", "price": 10.0, "source": "tencent"}]
            first = server.fetch_with_cache("quotes", "600519.SH", lambda: live)
            cached = server.fetch_with_cache(
                "quotes", "600519.SH", lambda: (_ for _ in ()).throw(DataSourceError("upstream failed")),
            )
        self.assertEqual(first[1], "live")
        self.assertEqual(cached[0], live)
        self.assertEqual(cached[1], "cached")
        self.assertIn("upstream failed", cached[2])
        self.assertIsNotNone(cached[3])

    def test_cache_does_not_invent_data_when_no_success_exists(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "CACHE_DIR", Path(directory)):
            with self.assertRaises(DataSourceError):
                server.fetch_with_cache(
                    "history", "missing", lambda: (_ for _ in ()).throw(DataSourceError("offline")),
                )

    def test_market_session_reports_clock_basis(self):
        shanghai = timezone(timedelta(hours=8))
        session = server.market_session(datetime(2026, 9, 17, 10, 0, tzinfo=shanghai))
        self.assertEqual(session["status"], "trading")
        self.assertTrue(session["calendar_covered"])
        self.assertIn("上交所官方休市安排", session["basis"])

    def test_market_session_honors_official_holiday_calendar(self):
        shanghai = timezone(timedelta(hours=8))
        session = server.market_session(datetime(2026, 9, 25, 10, 0, tzinfo=shanghai))
        self.assertEqual(session["status"], "closed")
        self.assertEqual(session["label"], "休市（中秋节）")

    def test_market_session_marks_uncovered_year(self):
        shanghai = timezone(timedelta(hours=8))
        session = server.market_session(datetime(2027, 1, 4, 10, 0, tzinfo=shanghai))
        self.assertFalse(session["calendar_covered"])
        self.assertIn("缺少本地官方休市表", session["basis"])

    def test_quiet_hours_support_overnight_range(self):
        shanghai = timezone(timedelta(hours=8))
        settings = {"quiet_hours_enabled": True, "quiet_start": "15:01", "quiet_end": "09:29"}
        self.assertTrue(server.is_quiet_time(settings, datetime(2026, 9, 19, 23, 0, tzinfo=shanghai)))
        self.assertTrue(server.is_quiet_time(settings, datetime(2026, 9, 19, 8, 30, tzinfo=shanghai)))
        self.assertFalse(server.is_quiet_time(settings, datetime(2026, 9, 19, 10, 0, tzinfo=shanghai)))

    def test_validate_new_alert_types(self):
        rsi = server.validate_alert({"symbol": "600519", "type": "rsi_threshold", "params": {"direction": "below", "threshold": 30}})
        macd = server.validate_alert({"symbol": "600519", "type": "macd_cross", "params": {"direction": "above"}})
        combined = server.validate_alert({"symbol": "600519", "type": "breakout_volume", "params": {"lookback": 20, "window": 5, "multiple": 2}})
        self.assertEqual(rsi["params"]["period"], 14)
        self.assertEqual(macd["params"]["signal"], 9)
        self.assertEqual(combined["params"]["lookback"], 20)

    def test_record_filter_and_csv_export(self):
        records = [
            {"symbol": "600519.SH", "name": "贵州茅台", "type": "rsi_threshold", "message": "RSI 命中", "triggered_at": "2026-09-18T10:00:00+08:00", "source": "tencent"},
            {"symbol": "300750.SZ", "name": "宁德时代", "type": "macd_cross", "message": "=unsafe", "triggered_at": "2026-09-19T10:00:00+08:00", "source": "hithink", "notification_suppressed": True},
        ]
        filtered = server.filter_records(records, {"symbol": ["300750.SZ"], "date": ["2026-09-19"]})
        csv_text = server.records_csv(filtered).decode("utf-8-sig")
        self.assertEqual(len(filtered), 1)
        self.assertIn("MACD 金叉/死叉", csv_text)
        self.assertIn("'=unsafe", csv_text)
        self.assertIn("已静默", csv_text)

    def test_trading_hours_only_rule_is_skipped_without_consuming_state(self):
        shanghai = timezone(timedelta(hours=8))
        after_close = datetime(2026, 9, 18, 16, 0, tzinfo=shanghai)
        rule = {
            "id": "only-open", "symbol": "600519.SH", "type": "price_above",
            "params": {"threshold": 1}, "enabled": True, "last_active": False,
            "once_per_day": False, "cooldown_minutes": 1, "trading_hours_only": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, "DATA_DIR", root), patch.object(server, "STATE_FILE", root / "state.json"):
                state = server._fresh_default()
                state["alerts"] = [rule]
                server.save_state(state)
                with patch.object(server, "get_provider", side_effect=AssertionError("skip cycle must not initialize provider")):
                    result = server.evaluate_all(after_close)
                latest = server.load_state()["alerts"][0]
        self.assertTrue(result["skipped"])
        self.assertFalse(latest["last_active"])
        self.assertIn("仅在交易时段", latest["last_message"])
        self.assertEqual(latest["stats"]["skipped"], 1)

    def test_audit_filter_and_limit(self):
        state = server._fresh_default()
        state["audit"] = [
            {"id": str(index), "rule_id": "r1", "symbol": "600519.SH", "status": "not_met" if index % 2 else "triggered"}
            for index in range(12)
        ]
        result = server.filtered_audit(state, {"status": ["triggered"], "limit": ["3"]})
        self.assertEqual(len(result), 3)
        self.assertTrue(all(item["status"] == "triggered" for item in result))

    def test_source_failure_is_audited_and_counted(self):
        class Provider:
            name = "broken"
            def quotes(self, _symbols):
                raise DataSourceError("offline")

        rule = {
            "id": "r1", "symbol": "600519.SH", "type": "price_above", "params": {"threshold": 1},
            "enabled": True, "once_per_day": False, "cooldown_minutes": 1, "trading_hours_only": False,
            "last_active": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, "DATA_DIR", root), patch.object(server, "STATE_FILE", root / "state.json"), patch.object(server, "get_provider", return_value=Provider()):
                state = server._fresh_default()
                state["alerts"] = [rule]
                server.save_state(state)
                with self.assertRaises(DataSourceError):
                    server.evaluate_all(datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=8))))
                latest = server.load_state()
        self.assertEqual(latest["alerts"][0]["stats"]["errors"], 1)
        self.assertEqual(latest["audit"][0]["status"], "source_error")

    def test_quiet_hours_record_trigger_but_suppress_notification(self):
        shanghai = timezone(timedelta(hours=8))
        late = datetime(2026, 9, 18, 23, 0, tzinfo=shanghai)
        rule = {
            "id": "all-hours", "symbol": "600519.SH", "type": "price_above",
            "params": {"threshold": 1}, "enabled": True, "last_active": False,
            "once_per_day": False, "cooldown_minutes": 1, "trading_hours_only": False,
        }

        class Provider:
            name = "test"
            def quotes(self, _symbols):
                return [{"symbol": "600519.SH", "name": "贵州茅台", "price": 10, "source": "test", "timestamp": 1}]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, "DATA_DIR", root), patch.object(server, "STATE_FILE", root / "state.json"), patch.object(server, "get_provider", return_value=Provider()):
                state = server._fresh_default()
                state["alerts"] = [rule]
                server.save_state(state)
                result = server.evaluate_all(late)
        self.assertEqual(len(result["triggered"]), 1)
        self.assertTrue(result["triggered"][0]["notification_suppressed"])


if __name__ == "__main__":
    unittest.main()
