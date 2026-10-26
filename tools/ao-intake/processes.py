"""PID identity includes creation time; unknown identity is never considered dead."""
import os
import ctypes
from pathlib import Path

def identity(pid):
    if not pid:
        return None
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            if ctypes.get_last_error() == 5:
                return 'UNKNOWN'
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)):
                return 'UNKNOWN'
            if code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return 'UNKNOWN'
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        if stat[0] == 'Z':
            return None
        return stat[19]
    except FileNotFoundError:
        return None
    except OSError:
        return 'UNKNOWN'

def alive(pid, created):
    actual = identity(pid)
    if actual is None:
        return False
    if actual == 'UNKNOWN' or created is None:
        return True  # fail closed: do not restart an unqualified PID
    return str(actual) == str(created)
