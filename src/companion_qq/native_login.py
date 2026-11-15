"""Explicit QR/password login; never starts a message session or chooses an account."""
from .native_process import NativeProcess


class NativeLogin(NativeProcess):
    WORKER = "login_worker.cjs"
    ERRORS = NativeProcess.ERRORS | {"qq_login_state", "qq_login_failed"}
