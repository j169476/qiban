"""Windows-only installation helpers; passwords travel through an owner-only pipe."""
from __future__ import annotations

import ctypes
import os
import subprocess
import uuid
from ctypes import wintypes as w

kernel = ctypes.WinDLL('kernel32', use_last_error=True)
security = ctypes.WinDLL('advapi32', use_last_error=True)


def bind(lib, name, result, *arguments):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, arguments
    return fn


close = bind(kernel, 'CloseHandle', w.BOOL, w.HANDLE)
free = bind(kernel, 'LocalFree', w.HANDLE, w.HANDLE)
get_process = bind(kernel, 'GetCurrentProcess', w.HANDLE)
open_token = bind(security, 'OpenProcessToken', w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
get_token = bind(security, 'GetTokenInformation', w.BOOL, w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD))
sid_string = bind(security, 'ConvertSidToStringSidW', w.BOOL, ctypes.c_void_p, ctypes.POINTER(w.LPWSTR))
sddl = bind(security, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', w.BOOL, w.LPCWSTR, w.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD))


def user_sid():
    token, size = w.HANDLE(), w.DWORD()
    if not open_token(get_process(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        get_token(token, 1, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not get_token(token, 1, buf, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        text = w.LPWSTR()
        if not sid_string(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            free(ctypes.cast(text, w.HANDLE))
    finally:
        close(token)


def protect_directory(path):
    subprocess.run([os.path.join(os.environ['SystemRoot'], 'System32', 'icacls.exe'), str(path),
                    '/inheritance:r', '/grant:r', '*'+user_sid()+':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW)


class Attributes(ctypes.Structure):
    _fields_ = [('size', w.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', w.BOOL)]


create_pipe = bind(kernel, 'CreateNamedPipeW', w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD,
                   w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(Attributes))
connect_pipe = bind(kernel, 'ConnectNamedPipe', w.BOOL, w.HANDLE, ctypes.c_void_p)
write_pipe = bind(kernel, 'WriteFile', w.BOOL, w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p)
flush_pipe = bind(kernel, 'FlushFileBuffers', w.BOOL, w.HANDLE)


class PasswordPipe:
    def __init__(self):
        self.name = r'\\.\pipe\qiban-init-' + uuid.uuid4().hex
        descriptor = ctypes.c_void_p()
        if not sddl('D:P(A;;GA;;;SY)(A;;GA;;;'+user_sid()+')', 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
            # Outbound, byte mode, reject remote clients. Handle never inherited.
            self.handle = create_pipe(self.name, 0x2 | 0x80000, 0x8, 1, 4096, 4096, 0, ctypes.byref(attributes))
            if self.handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            free(descriptor)

    def send(self, password):
        if not connect_pipe(self.handle, None) and ctypes.get_last_error() != 535:
            raise ctypes.WinError(ctypes.get_last_error())
        raw = (password+'\n').encode('ascii')
        count = w.DWORD()
        if not write_pipe(self.handle, raw, len(raw), ctypes.byref(count), None) or count.value != len(raw):
            raise ctypes.WinError(ctypes.get_last_error())
        if not flush_pipe(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            close(self.handle)
            self.handle = None
