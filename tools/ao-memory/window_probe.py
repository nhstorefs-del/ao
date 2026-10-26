"""Visible console-window sampler; reports new windows relative to baseline."""
import ctypes
from ctypes import wintypes
import os
import threading
import time


def visible_console_windows():
    if os.name != 'nt':
        raise RuntimeError('Window measurement requires Windows')
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user.IsWindowVisible.argtypes = (wintypes.HWND,)
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    found = {}
    def visit(hwnd, unused):
        if user.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            handle = kernel.OpenProcess(0x1000, False, pid.value)
            if handle:
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buffer))
                    if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        name = os.path.basename(buffer.value).lower()
                        if name in ('python.exe', 'pythonw.exe', 'cmd.exe', 'node.exe', 'codex.exe', 'conhost.exe', 'openconsole.exe'):
                            found[int(hwnd)] = {'pid': pid.value, 'name': name}
                finally:
                    kernel.CloseHandle(handle)
        return True
    if not user.EnumWindows(callback_type(visit), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return found


class WindowSampler:
    def __enter__(self):
        self.baseline = visible_console_windows()
        self.seen = {}
        self.peak = 0
        self.samples = 0
        self.error = None
        self.stop = threading.Event()
        def sample():
            try:
                while not self.stop.is_set():
                    current = visible_console_windows()
                    new = {k: v for k, v in current.items() if k not in self.baseline}
                    self.seen.update(new)
                    self.peak = max(self.peak, len(new))
                    self.samples += 1
                    self.stop.wait(.1)
            except Exception as exc:
                self.error = exc
        self.thread = threading.Thread(target=sample, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        if self.error:
            raise self.error

    def result(self):
        return {'baseline': self.baseline, 'new_windows': len(self.seen), 'peak': self.peak,
                'samples': self.samples, 'windows': self.seen}
