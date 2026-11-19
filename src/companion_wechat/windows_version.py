"""Read a Windows executable's public version resource."""
import ctypes
from ctypes import wintypes


def file_version(path):
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    unused = wintypes.DWORD()
    size = api.GetFileVersionInfoSizeW(path, ctypes.byref(unused))
    if not 0 < size <= 1024 * 1024:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(path, 0, size, buffer):
        return ""
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if (not api.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length))
            or not pointer.value or length.value < 52):
        return ""
    values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if values[0] != 0xFEEF04BD:
        return ""
    hi, lo = values[2], values[3]
    return f"{hi >> 16}.{hi & 65535}.{lo >> 16}.{lo & 65535}"


def version_status():
    from .native.versions import PROFILES
    from .providers.uia import running_client_versions

    versions = running_client_versions()
    supported = list(dict.fromkeys(profile["version"] for profile in PROFILES))
    return {"running_versions": versions,
            "supported_versions": supported,
            "unrecognized_versions": [version for version in versions if version not in supported]}


def unsupported_message(status=None):
    status = version_status() if status is None else status
    versions = "、".join(status["running_versions"])
    prefix = f"检测到电脑版微信 {versions}。" if versions else ""
    return prefix + "当前读取组件无法核验此版本，个人号自动回复暂不可用。"
