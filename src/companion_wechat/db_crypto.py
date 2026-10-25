"""Bounded SQLCipher 4 raw-key page authentication, with no file I/O.

This helper handles exactly the WeChat candidate format under investigation:
4096-byte pages, 80 reserved bytes, AES-256-CBC, SHA-512 HMAC, and little-endian
page numbers. It does not derive an encryption key from a password, interpret
SQLite records, assemble a database, parse WAL or establish a live snapshot.

Format basis: https://www.zetetic.net/sqlcipher/design/ and SQLCipher's
``src/sqlcipher.c`` (DEFAULT_CIPHER_FLAGS, HMAC_SALT_MASK, FAST_PBKDF2_ITER).
Only the caller's requested page is returned. Nothing is logged or persisted.

``close`` wipes this object's bytearrays on a best-effort basis. Python and the
cryptographic provider may create temporary copies which cannot be guaranteed
to be erased; callers must not claim complete key zeroization or memory locking.
"""
from __future__ import annotations

import hashlib
import hmac
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
RESERVED_SIZE = 80
SALT_SIZE = 16
SQLITE_HEADER = b"SQLite format 3\x00"
MAX_PAGE_NUMBER = 0xFFFFFFFE
_PAYLOAD_END = PAGE_SIZE - RESERVED_SIZE
_MAC_START = _PAYLOAD_END + 16
_ERRORS = {"invalid_key", "invalid_salt", "invalid_page", "invalid_page_number",
           "page_auth_failed", "invalid_sqlite_header", "cipher_closed", "crypto_failed"}


class DatabaseCryptoError(ValueError):
    """A fixed public code; never include key, ciphertext, plaintext or paths."""

    def __init__(self, code):
        self.code = code if type(code) is str and code in _ERRORS else "crypto_failed"
        super().__init__(self.code)


def _view(value, length, code):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise DatabaseCryptoError(code)
    try:
        view = memoryview(value).cast("B")
        if len(view) != length:
            raise DatabaseCryptoError(code)
        return view
    except (TypeError, ValueError):
        raise DatabaseCryptoError(code) from None


def _require_page_number(value):
    if type(value) is not int or not 1 <= value <= MAX_PAGE_NUMBER:
        raise DatabaseCryptoError("invalid_page_number")


class SqlCipher4Pages:
    """Memory-only keys and authentication-first decoding of one fixed-size page."""

    __slots__ = ("_key", "_salt", "_hmac_key", "_closed")

    def __init__(self, raw_key, salt):
        key_view = _view(raw_key, 32, "invalid_key")
        salt_view = _view(salt, SALT_SIZE, "invalid_salt")
        self._key = bytearray(key_view)
        self._salt = bytearray(salt_view)
        self._hmac_key = bytearray()
        self._closed = False
        mixed_salt = bytearray(value ^ 0x3A for value in self._salt)
        try:
            self._hmac_key = bytearray(hashlib.pbkdf2_hmac("sha512", self._key, mixed_salt, 2, 32))
        except Exception:
            self.close()
            raise DatabaseCryptoError("crypto_failed") from None
        finally:
            mixed_salt[:] = b"\x00" * len(mixed_salt)

    def __repr__(self):
        return f"SqlCipher4Pages(<redacted>, closed={self._closed})"

    def _require_open(self):
        if self._closed:
            raise DatabaseCryptoError("cipher_closed")

    def close(self):
        for value in (self._key, self._hmac_key, self._salt):
            value[:] = b"\x00" * len(value)
        self._closed = True

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, *_exc):
        self.close()

    def decrypt_page(self, ciphertext, page_number):
        """Authenticate before decrypting; return a page with zeroed reserve area.

        For page 1, verify its clear salt against the supplied database salt and
        restore the SQLite magic. The reserved-byte count in its header remains
        80; changing it would require rewriting SQLite's page layout.
        """
        self._require_open()
        _require_page_number(page_number)
        # Freeze this one bounded page so a caller-owned mutable buffer cannot
        # change between HMAC validation and AES decryption.
        page = bytes(_view(ciphertext, PAGE_SIZE, "invalid_page"))
        start = SALT_SIZE if page_number == 1 else 0
        if page_number == 1 and not hmac.compare_digest(page[:SALT_SIZE], self._salt):
            raise DatabaseCryptoError("page_auth_failed")
        try:
            mac = hmac.new(self._hmac_key, digestmod=hashlib.sha512)
            mac.update(page[start:_MAC_START])
            mac.update(struct.pack("<I", page_number))
            if not hmac.compare_digest(mac.digest(), page[_MAC_START:]):
                raise DatabaseCryptoError("page_auth_failed")
            decryptor = Cipher(algorithms.AES(self._key), modes.CBC(page[_PAYLOAD_END:_MAC_START])).decryptor()
            payload = decryptor.update(page[start:_PAYLOAD_END]) + decryptor.finalize()
        except DatabaseCryptoError:
            raise
        except Exception:
            raise DatabaseCryptoError("crypto_failed") from None
        output = (SQLITE_HEADER if page_number == 1 else b"") + payload + bytes(RESERVED_SIZE)
        if page_number == 1 and (output[16:18] != b"\x10\x00" or output[18] not in (1, 2)
                                 or output[19] not in (1, 2) or output[20] != RESERVED_SIZE
                                 or output[21:24] != b"\x40\x20\x20"):
            raise DatabaseCryptoError("invalid_sqlite_header")
        return output


def verify_key(raw_key, page1):
    """Check one page, without writing anything or returning plaintext.

    Wrong keys, changed ciphertext/MAC, and authenticated but unsupported page
    headers return False. Invalid input lengths/types and provider failures use
    fixed DatabaseCryptoError codes, so callers can distinguish bad plumbing.
    """
    page = _view(page1, PAGE_SIZE, "invalid_page")
    with SqlCipher4Pages(raw_key, page[:SALT_SIZE]) as cipher:
        try:
            cipher.decrypt_page(page, 1)
        except DatabaseCryptoError as exc:
            if exc.code in {"page_auth_failed", "invalid_sqlite_header"}:
                return False
            raise
    return True
