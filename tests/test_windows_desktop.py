import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import windows_desktop
from windows_desktop import IS_WINDOWS, SingleInstance, TrayController, _private_browser_arguments, _quoted_startup_command


class WindowsDesktopTests(unittest.TestCase):
    def make_controller(self, on_toggle=lambda: False, on_exit=lambda: None, *, portable_mode=False):
        return TrayController(
            address="http://127.0.0.1:8765",
            log_dir=Path("logs"),
            on_toggle_monitor=on_toggle,
            is_monitor_paused=lambda: False,
            on_exit=on_exit,
            portable_mode=portable_mode,
        )

    def test_autostart_command_quotes_executable_and_keeps_browser_closed(self):
        command = _quoted_startup_command(Path(r"C:\看盘 工具\自主看盘台.exe"))
        self.assertEqual(command, '"C:\\看盘 工具\\自主看盘台.exe" --no-open')

    def test_private_browser_arguments_match_edge_and_chrome(self):
        address = "http://127.0.0.1:8765"
        self.assertEqual(
            _private_browser_arguments(Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"), address),
            [r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "--inprivate", "--new-window", address],
        )
        self.assertEqual(
            _private_browser_arguments(Path(r"D:\Apps\chrome.exe"), address),
            [r"D:\Apps\chrome.exe", "--incognito", "--new-window", address],
        )

    @unittest.skipUnless(IS_WINDOWS, "named mutex is Windows-only")
    def test_single_instance_mutex_is_scoped_by_port(self):
        first = SingleInstance(58761)
        duplicate = SingleInstance(58761)
        other_port = SingleInstance(58762)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(duplicate.acquire())
            self.assertTrue(other_port.acquire())
        finally:
            duplicate.release()
            other_port.release()
            first.release()

    def test_tray_pause_command_calls_monitor_and_reports_state(self):
        controller = self.make_controller(on_toggle=lambda: True)
        with patch.object(controller, "show_notification") as notify:
            controller._handle_command(controller.CMD_TOGGLE_MONITOR)
        notify.assert_called_once_with("自主看盘台", "后台预警已暂停")

    def test_tray_autostart_command_uses_current_executable(self):
        controller = self.make_controller()
        with (
            patch.object(windows_desktop, "is_autostart_enabled", return_value=False),
            patch.object(windows_desktop, "set_autostart") as set_startup,
            patch.object(controller, "show_notification"),
        ):
            controller._handle_command(controller.CMD_TOGGLE_AUTOSTART)
        set_startup.assert_called_once_with(True, Path(sys.executable))

    def test_portable_tray_refuses_registry_autostart(self):
        controller = self.make_controller(portable_mode=True)
        with (
            patch.object(windows_desktop, "set_autostart") as set_startup,
            patch.object(controller, "show_notification") as notify,
        ):
            controller._handle_command(controller.CMD_TOGGLE_AUTOSTART)
        set_startup.assert_not_called()
        notify.assert_called_once_with("自主看盘台", "便携模式不会写入 Windows 开机启动项")

    def test_tray_exit_command_requests_graceful_shutdown(self):
        requested = threading.Event()
        controller = self.make_controller(on_exit=requested.set)
        controller._handle_command(controller.CMD_EXIT)
        self.assertTrue(requested.wait(1), "托盘退出没有请求服务安全停止")


if __name__ == "__main__":
    unittest.main()
