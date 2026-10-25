"""Password-wrapped key recovery artifact, separate from encrypted data backups."""
import hashlib
import secrets

from companion_core.cipher import ContentCipher
from companion_core.errors import AssistantError

MAGIC = b"CCKEY1:"


def _derive(password, salt):
    if not isinstance(password, str) or not 16 <= len(password) <= 256:
        raise AssistantError("recovery_password", "恢复口令需要 16 至 256 个字符，请与日常登录密码分开保管")
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


def wrap(key, password):
    if len(key) != 32:
        raise AssistantError("recovery_key", "资料密钥无效")
    salt = secrets.token_bytes(16)
    encrypted = ContentCipher(_derive(password, salt)).encrypt(key, "recovery", "database-key", "1")
    return MAGIC + salt + encrypted


def unwrap(blob, password):
    if not 40 < len(blob) < 300 or not blob.startswith(MAGIC):
        raise AssistantError("recovery_key", "恢复文件格式无效")
    offset = len(MAGIC)
    key = ContentCipher(_derive(password, blob[offset:offset + 16])).decrypt(blob[offset + 16:], "recovery", "database-key", "1")
    if len(key) != 32:
        raise AssistantError("recovery_key", "恢复文件中的密钥无效")
    return key
