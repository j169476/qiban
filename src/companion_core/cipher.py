"""Authenticated encryption for stored content; keys are supplied by the host."""
import json
import secrets

from .errors import AssistantError


class ContentCipher:
    def __init__(self, key):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not isinstance(key, bytes) or len(key) != 32:
            raise AssistantError("storage_key", "存储加密密钥无效")
        self._cipher = AESGCM(key)

    @staticmethod
    def associated_data(kind, item_id, version):
        return json.dumps(["companion-content-v1", kind, item_id, version], ensure_ascii=False).encode()

    def encrypt(self, raw, kind, item_id, version):
        nonce = secrets.token_bytes(12)
        return b"CC1" + nonce + self._cipher.encrypt(nonce, raw, self.associated_data(kind, item_id, version))

    def decrypt(self, encrypted, kind, item_id, version):
        from cryptography.exceptions import InvalidTag

        try:
            if encrypted[:3] != b"CC1" or len(encrypted) < 31:
                raise ValueError()
            return self._cipher.decrypt(encrypted[3:15], encrypted[15:],
                                        self.associated_data(kind, item_id, version))
        except (ValueError, InvalidTag):
            raise AssistantError("storage_integrity", "加密资料或其归属标识校验失败") from None
