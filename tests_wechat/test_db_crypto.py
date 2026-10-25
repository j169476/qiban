"""Synthetic encrypted pages only; no real WeChat files or keys are opened."""
import hashlib
import hmac
import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from companion_wechat import db_crypto
from companion_wechat.db_crypto import DatabaseCryptoError, SqlCipher4Pages, verify_key

KEY = bytes(range(32))
SALT = bytes(range(16, 32))
IV = bytes(range(160, 176))


def plain_page(number=1):
    value = bytearray(b"SYNTHETIC PAGE " * 300)[:4096]
    value[-80:] = bytes(80)
    if number == 1:
        value[:16] = b"SQLite format 3\x00"
        value[16:24] = b"\x10\x00\x02\x02\x50\x40\x20\x20"
        value[28:32] = struct.pack(">I", 2)
    return bytes(value)


def encrypted_page(number=1, *, plain=None, key=KEY, salt=SALT, iv=IV, pgno_endian="<"):
    """Independent fixture encoder using the published on-disk layout."""
    plain = plain_page(number) if plain is None else plain
    prefix = salt if number == 1 else b""
    start = 16 if number == 1 else 0
    aes = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = aes.update(plain[start:4016]) + aes.finalize()
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(x ^ 0x3A for x in salt), 2, 32)
    signed = encrypted + iv + struct.pack(pgno_endian + "I", number)
    return prefix + encrypted + iv + hmac.digest(mac_key, signed, "sha512")


@pytest.mark.parametrize("number", [1, 2, 65537, 0xFFFFFFFE])
def test_page_authentication_and_decryption_follow_page_number_offsets(number):
    encrypted = encrypted_page(number)
    assert len(encrypted) == 4096
    with SqlCipher4Pages(KEY, SALT) as cipher:
        assert cipher.decrypt_page(encrypted, number) == plain_page(number)


def test_verify_key_returns_boolean_without_plaintext():
    assert verify_key(KEY, encrypted_page()) is True
    assert verify_key(bytes(reversed(KEY)), encrypted_page()) is False


@pytest.mark.parametrize("offset", [0, 15, 16, 4000, 4015, 4016, 4031, 4032, 4095])
def test_changed_salt_ciphertext_iv_or_mac_fails_closed(offset):
    page = bytearray(encrypted_page())
    page[offset] ^= 1
    assert verify_key(KEY, page) is False
    with SqlCipher4Pages(KEY, SALT) as cipher, pytest.raises(DatabaseCryptoError) as exc:
        cipher.decrypt_page(page, 1)
    assert exc.value.code == "page_auth_failed"


def test_authentication_occurs_before_aes_is_constructed(monkeypatch):
    damaged = bytearray(encrypted_page())
    damaged[20] ^= 1

    def forbidden_cipher(*args, **kwargs):
        pytest.fail("AES must not see unauthenticated bytes")

    monkeypatch.setattr(db_crypto, "Cipher", forbidden_cipher)
    assert verify_key(KEY, damaged) is False


def test_page_reordering_and_wrong_hmac_endianness_are_detected():
    with SqlCipher4Pages(KEY, SALT) as cipher:
        for page in (encrypted_page(2), encrypted_page(3, pgno_endian=">")):
            with pytest.raises(DatabaseCryptoError, match="page_auth_failed"):
                cipher.decrypt_page(page, 3)


@pytest.mark.parametrize("offset,value", [(16, 32), (18, 3), (19, 0), (20, 0), (21, 63)])
def test_correct_key_with_unsupported_authenticated_header_is_not_accepted(offset, value):
    plain = bytearray(plain_page())
    plain[offset] = value
    page = encrypted_page(plain=plain)
    assert verify_key(KEY, page) is False
    with SqlCipher4Pages(KEY, SALT) as cipher, pytest.raises(DatabaseCryptoError, match="invalid_sqlite_header"):
        cipher.decrypt_page(page, 1)


@pytest.mark.parametrize("value", [b"", b"x" * 31, b"x" * 33, "SECRET key", None])
def test_invalid_keys_use_fixed_errors(value):
    with pytest.raises(DatabaseCryptoError) as exc:
        verify_key(value, encrypted_page())
    assert str(exc.value) == "invalid_key"
    assert "SECRET" not in repr(exc.value)


@pytest.mark.parametrize("value", [b"", b"x" * 15, b"x" * 17, "SECRET salt"])
def test_invalid_salt_uses_fixed_error(value):
    with pytest.raises(DatabaseCryptoError, match="invalid_salt"):
        SqlCipher4Pages(KEY, value)


@pytest.mark.parametrize("value", [b"", b"x" * 4095, b"x" * 4097, "SECRET page", None],
                         ids=["empty", "short", "long", "string", "none"])
def test_only_one_exact_sized_page_can_be_requested(value):
    with pytest.raises(DatabaseCryptoError, match="invalid_page"):
        verify_key(KEY, value)


@pytest.mark.parametrize("number", [0, -1, 0xFFFFFFFF, True, 1.0, "SECRET"])
def test_invalid_page_numbers_are_rejected(number):
    with SqlCipher4Pages(KEY, SALT) as cipher, pytest.raises(DatabaseCryptoError, match="invalid_page_number"):
        cipher.decrypt_page(encrypted_page(), number)


def test_caller_owned_key_and_page_buffers_are_not_modified():
    key, page = bytearray(KEY), bytearray(encrypted_page())
    original = bytes(page)
    assert verify_key(key, memoryview(page))
    assert key == KEY and page == original


def test_noncontiguous_buffers_are_rejected_without_echoing_data():
    with pytest.raises(DatabaseCryptoError, match="invalid_key"):
        SqlCipher4Pages(memoryview(bytes(64))[::2], SALT)


def test_mutation_of_caller_buffer_after_authentication_cannot_change_plaintext(monkeypatch):
    page = bytearray(encrypted_page())
    real_new = hmac.new

    class MutatingMac:
        def __init__(self, *args, **kwargs):
            self.mac = real_new(*args, **kwargs)

        def update(self, data):
            self.mac.update(data)

        def digest(self):
            value = self.mac.digest()
            page[20] ^= 1
            return value

    monkeypatch.setattr(db_crypto.hmac, "new", MutatingMac)
    with SqlCipher4Pages(KEY, SALT) as cipher:
        assert cipher.decrypt_page(page, 1) == plain_page()


def test_context_manager_closes_on_exception_and_wipes_owned_mutable_buffers():
    cipher = SqlCipher4Pages(KEY, SALT)
    buffers = [cipher._key, cipher._hmac_key, cipher._salt]
    with pytest.raises(RuntimeError), cipher:
        raise RuntimeError("synthetic")
    assert all(not any(value) for value in buffers)
    cipher.close()
    assert repr(cipher) == "SqlCipher4Pages(<redacted>, closed=True)"
    with pytest.raises(DatabaseCryptoError, match="cipher_closed"):
        cipher.decrypt_page(encrypted_page(), 1)
    with pytest.raises(DatabaseCryptoError, match="cipher_closed"):
        cipher.__enter__()


def test_crypto_provider_exception_does_not_expose_details(monkeypatch):
    page = encrypted_page()

    def fail(*args, **kwargs):
        raise RuntimeError("SECRET key or plaintext")

    monkeypatch.setattr(db_crypto, "Cipher", fail)
    with pytest.raises(DatabaseCryptoError) as exc:
        verify_key(KEY, page)
    assert str(exc.value) == "crypto_failed"
    assert "SECRET" not in repr(exc.value)


def test_error_constructor_does_not_accept_untrusted_public_message():
    assert str(DatabaseCryptoError("SECRET cipher text")) == "crypto_failed"
    assert str(DatabaseCryptoError(["SECRET cipher text"])) == "crypto_failed"
