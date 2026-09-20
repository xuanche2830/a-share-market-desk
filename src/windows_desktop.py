from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable


LOGGER = logging.getLogger("marketdesk.desktop")
IS_WINDOWS = sys.platform == "win32"
AUTOSTART_NAME = "自主看盘台"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _quoted_startup_command(executable: Path) -> str:
    return f'"{executable}" --no-open'


def is_autostart_enabled(executable: Path | None = None) -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    target = str((executable or Path(sys.executable)).resolve()).casefold()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
        return target in str(value).casefold()
    except (FileNotFoundError, OSError):
        return False


def set_autostart(enabled: bool, executable: Path | None = None) -> None:
    if not IS_WINDOWS:
        raise OSError("开机启动仅支持 Windows")
    import winreg

    target = (executable or Path(sys.executable)).resolve()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, _quoted_startup_command(target))
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


class SingleInstance:
    """A per-port named mutex so one dashboard instance owns each address."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, port: int) -> None:
        self.name = f"Local\\ZiZhuKanPanTai_{int(port)}"
        self.handle: int | None = None

    def acquire(self) -> bool:
        if not IS_WINDOWS:
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self.handle = int(handle)
        return True

    def release(self) -> None:
        if not self.handle or not IS_WINDOWS:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(self.handle)
        self.handle = None


def show_message(title: str, message: str, *, error: bool = False) -> None:
    if not IS_WINDOWS:
        return
    flags = 0x00000010 if error else 0x00000040
    message_box = ctypes.windll.user32.MessageBoxW
    message_box.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    message_box.restype = ctypes.c_int
    message_box(None, message, title, flags | 0x00000000)


def _private_browser_arguments(executable: Path, address: str) -> list[str]:
    flag = "--inprivate" if executable.name.casefold() in {"msedge.exe", "msedge"} else "--incognito"
    return [str(executable), flag, "--new-window", address]


def open_dashboard_address(address: str, *, private: bool = False) -> bool:
    """Open the local dashboard, preferring a private window in portable mode."""
    if private and IS_WINDOWS:
        candidates: list[Path] = []
        for name in ("msedge", "chrome"):
            resolved = shutil.which(name)
            if resolved:
                candidates.append(Path(resolved))
        for variable, relative in (
            ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
            ("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
            ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
            ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
        ):
            base = os.environ.get(variable)
            if base:
                candidates.append(Path(base) / relative)
        seen: set[str] = set()
        for executable in candidates:
            key = str(executable).casefold()
            if key in seen or not executable.is_file():
                continue
            seen.add(key)
            try:
                flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
                subprocess.Popen(
                    _private_browser_arguments(executable, address),
                    close_fds=True, creationflags=flags,
                )
                return True
            except OSError:
                continue
    return bool(webbrowser.open(address))


if IS_WINDOWS:
    from ctypes import wintypes

    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_COMMAND = 0x0111
    WM_CONTEXTMENU = 0x007B
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_USER = 0x0400
    NIN_BALLOONUSERCLICK = WM_USER + 5

    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIF_INFO = 0x00000010
    NIIF_INFO = 0x00000001
    NIIF_WARNING = 0x00000002

    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    TPM_LEFTALIGN = 0x0000
    TPM_BOTTOMALIGN = 0x0020
    TPM_RIGHTBUTTON = 0x0002
    IDI_APPLICATION = 32512

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    def _configure_winapi() -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.LoadIconW.restype = wintypes.HICON
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.PostQuitMessage.restype = None
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = LRESULT
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, ctypes.POINTER(wintypes.RECT),
        ]
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class TrayController:
    CALLBACK_MESSAGE = 0x0400 + 20
    CMD_OPEN = 1001
    CMD_TOGGLE_MONITOR = 1002
    CMD_TOGGLE_AUTOSTART = 1003
    CMD_OPEN_LOGS = 1004
    CMD_EXIT = 1005

    def __init__(
        self,
        address: str,
        log_dir: Path,
        on_toggle_monitor: Callable[[], bool],
        is_monitor_paused: Callable[[], bool],
        on_exit: Callable[[], None],
        *,
        portable_mode: bool = False,
    ) -> None:
        self.address = address
        self.log_dir = log_dir
        self.on_toggle_monitor = on_toggle_monitor
        self.is_monitor_paused = is_monitor_paused
        self.on_exit = on_exit
        self.portable_mode = portable_mode
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.failed: str | None = None
        self.hwnd: int | None = None
        self._class_name = f"ZiZhuKanPanTaiTray_{os.getpid()}"
        self._wnd_proc = None
        self._hinstance = None
        self._hicon = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive() and self.hwnd)

    def start(self) -> bool:
        if not IS_WINDOWS:
            self.failed = "系统不是 Windows"
            return False
        if self.thread and self.thread.is_alive():
            return True
        self.thread = threading.Thread(target=self._run, name="windows-tray", daemon=True)
        self.thread.start()
        self.ready.wait(3)
        if self.failed:
            LOGGER.error("系统托盘启动失败：%s", self.failed)
        return self.running

    def stop(self) -> None:
        if not IS_WINDOWS or not self.hwnd:
            return
        ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def open_dashboard(self) -> None:
        open_dashboard_address(self.address, private=self.portable_mode)

    def show_notification(self, title: str, message: str, *, warning: bool = False) -> bool:
        if not self.running:
            return False
        data = self._notification_data(NIF_INFO)
        data.szInfoTitle = title[:63]
        data.szInfo = message[:255]
        data.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
        ok = bool(ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)))
        if not ok:
            LOGGER.warning("Windows 通知发送失败：%s", title)
        return ok

    def _notification_data(self, flags: int) -> "NOTIFYICONDATAW":
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = self.CALLBACK_MESSAGE
        data.hIcon = self._hicon
        return data

    def _run(self) -> None:
        try:
            _configure_winapi()
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            shell32 = ctypes.windll.shell32
            self._hinstance = kernel32.GetModuleHandleW(None)
            icon_resource = ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR)
            self._hicon = user32.LoadIconW(None, icon_resource)
            self._wnd_proc = WNDPROC(self._window_proc)
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = self._wnd_proc
            window_class.hInstance = self._hinstance
            window_class.hIcon = self._hicon
            window_class.lpszClassName = self._class_name
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError()
            self.hwnd = user32.CreateWindowExW(
                0, self._class_name, "自主看盘台", 0,
                0, 0, 0, 0, None, None, self._hinstance, None,
            )
            if not self.hwnd:
                raise ctypes.WinError()
            data = self._notification_data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
            data.szTip = "自主看盘台 · 后台预警运行中"
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
                raise ctypes.WinError()
            self.ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.failed = str(exc)
            self.ready.set()
            LOGGER.exception("系统托盘线程异常")
        finally:
            if self.hwnd:
                data = self._notification_data(0)
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
                self.hwnd = None
            if self._hinstance:
                ctypes.windll.user32.UnregisterClassW(self._class_name, self._hinstance)

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            if message == self.CALLBACK_MESSAGE:
                if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu(hwnd)
                    return 0
                if lparam in (WM_LBUTTONDBLCLK, NIN_BALLOONUSERCLICK):
                    self.open_dashboard()
                    return 0
            elif message == WM_COMMAND:
                self._handle_command(int(wparam) & 0xFFFF)
                return 0
            elif message == WM_CLOSE:
                ctypes.windll.user32.DestroyWindow(hwnd)
                return 0
            elif message == WM_DESTROY:
                ctypes.windll.user32.PostQuitMessage(0)
                return 0
        except Exception:
            LOGGER.exception("处理托盘操作失败")
        return ctypes.windll.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        try:
            paused = self.is_monitor_paused()
            user32.AppendMenuW(menu, MF_STRING, self.CMD_OPEN, "打开看盘台")
            user32.AppendMenuW(menu, MF_STRING, self.CMD_TOGGLE_MONITOR, "恢复预警" if paused else "暂停预警")
            if not self.portable_mode:
                startup = is_autostart_enabled(Path(sys.executable))
                user32.AppendMenuW(menu, MF_STRING, self.CMD_TOGGLE_AUTOSTART, "关闭开机启动" if startup else "启用开机启动")
            user32.AppendMenuW(menu, MF_STRING, self.CMD_OPEN_LOGS, "打开日志目录")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, self.CMD_EXIT, "退出")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            user32.TrackPopupMenu(
                menu, TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON,
                point.x, point.y, 0, hwnd, None,
            )
        finally:
            user32.DestroyMenu(menu)

    def _handle_command(self, command: int) -> None:
        if command == self.CMD_OPEN:
            self.open_dashboard()
        elif command == self.CMD_TOGGLE_MONITOR:
            paused = self.on_toggle_monitor()
            self.show_notification("自主看盘台", "后台预警已暂停" if paused else "后台预警已恢复")
        elif command == self.CMD_TOGGLE_AUTOSTART:
            if self.portable_mode:
                self.show_notification("自主看盘台", "便携模式不会写入 Windows 开机启动项")
                return
            enabled = not is_autostart_enabled(Path(sys.executable))
            set_autostart(enabled, Path(sys.executable))
            self.show_notification("自主看盘台", "已启用开机启动" if enabled else "已关闭开机启动")
        elif command == self.CMD_OPEN_LOGS:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self.log_dir))
        elif command == self.CMD_EXIT:
            threading.Thread(target=self.on_exit, name="tray-shutdown", daemon=True).start()
