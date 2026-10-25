"""Ephemeral signed control metadata; never message text or database keys.

The role still uses private stdio. This optional controller bridge lets an
already waiting UI process observe the final two-second grant without another
tool round trip. Ed25519 private keys exist only in the two processes' memory.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class ControlSignal:
    def __init__(self, parent):
        self.root = Path(parent).resolve() / secrets.token_hex(16)
        self.root.mkdir(parents=True, exist_ok=False)
        self._key = Ed25519PrivateKey.generate()
        self._ui = None
        self._issued, self._consumed, self._files = set(), set(), set()

    def public(self):
        key = self._key.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"root": str(self.root), "public_key": base64.b64encode(key).decode()}

    def register(self, public_key):
        if self._ui is not None or not isinstance(public_key, str) or len(public_key) > 200:
            raise ValueError("ui_registration")
        key = serialization.load_der_public_key(base64.b64decode(public_key, validate=True))
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("ui_registration")
        self._ui = key

    def _path(self, action, suffix):
        if not isinstance(action, str) or not re.fullmatch(r"[a-f0-9]{32}", action):
            raise ValueError("signal_action")
        path = self.root / (action + "." + suffix + ".json")
        self._files.add(path)
        return path

    def receive(self, action, view_token, kind):
        if self._ui is None or kind not in {"ready", "submitted"} or (action, kind) in self._consumed:
            return False
        path = self._path(action, kind)
        try:
            raw = path.read_bytes()
            if len(raw) > 2048:
                return False
            envelope = json.loads(raw)
            if set(envelope) != {"payload", "signature"}:
                return False
            body = base64.b64decode(envelope["payload"], validate=True)
            self._ui.verify(base64.b64decode(envelope["signature"], validate=True), body)
            value = json.loads(body)
            if value != {"v": 1, "type": kind, "id": action, "view_token": view_token}:
                return False
        except Exception:
            return False
        self._consumed.add((action, kind))
        return True

    def grant(self, action, view_token, expires_unix_ms):
        now = int(time.time() * 1000)
        if (self._ui is None or action in self._issued or (action, "ready") not in self._consumed
                or type(expires_unix_ms) is not int or not now < expires_unix_ms <= now + 2000):
            raise ValueError("signal_grant")
        value = {"v": 1, "type": "grant", "id": action, "view_token": view_token,
                 "expires_unix_ms": expires_unix_ms}
        body = json.dumps(value, separators=(",", ":")).encode()
        envelope = {"payload": base64.b64encode(body).decode(),
                    "signature": base64.b64encode(self._key.sign(body)).decode()}
        path = self._path(action, "grant")
        self._issued.add(action)
        with path.open("x", encoding="utf-8") as output:
            json.dump(envelope, output, separators=(",", ":"))

    def close(self):
        self._key = self._ui = None
        for path in self._files:
            path.unlink(missing_ok=True)
        # An unexpected file is never recursively removed.
        try:
            self.root.rmdir()
        except OSError:
            pass
