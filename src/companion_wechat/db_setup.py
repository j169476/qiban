"""First-use directory browsing for one explicitly selected Windows login."""
import os
import re
from contextlib import contextmanager
from pathlib import Path

from .db_crypto import verify_key
from .db_probe import ProbeError, page_one, safe_db_path
from .db_source import contact_page, self_username, verify_contacts
from .db_sqlite import open_readonly
from .db_windows import NativeProcess, extract_keys
from .native.protocol import personal
from .native.windows import Windows


def account_directories(login, config, documents):
    if not personal(login):
        raise ProbeError("configuration_invalid")
    roots = {documents / "xwechat_files"}
    for ini in config.glob("*.ini"):
        if not ini.is_file() or ini.stat().st_size > 4096:
            continue
        try:
            value = ini.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            continue
        if re.match(r"^[a-zA-Z]:[\\/]", value) and not any(ord(c) < 32 for c in value):
            roots.add(Path(value) / "xwechat_files")
    paths = set()
    for root in roots:
        if root.is_dir():
            for path in root.glob(login + "_????"):
                if (re.fullmatch(re.escape(login) + r"_[A-Za-z0-9]{4}", path.name)
                        and path.is_dir() and (path / "db_storage/contact/contact.db").is_file()):
                    paths.add(path.resolve(strict=True))
    if not 1 <= len(paths) <= 8:
        raise ProbeError("database_path_invalid")
    return sorted(paths)


@contextmanager
def _contacts(login):
    windows = Windows()
    pid, born = windows.select(login)
    paths = account_directories(login, Path(os.environ["APPDATA"]) / "Tencent/xwechat/config",
                                Path.home() / "Documents")
    candidates = {f"candidate_{i}": path for i, path in enumerate(paths)}
    files = {i: safe_db_path(path, Path("contact/contact.db")) for i, path in candidates.items()}
    with NativeProcess(pid) as process, extract_keys(
        process, {i: page_one(p) for i, p in files.items()}, verify_key, require_all=False
    ) as keys:
        if len(keys.aliases()) != 1:
            raise ProbeError("source_identity_changed")
        selected = keys.aliases()[0]
        with open_readonly(files[selected], keys.key(selected)) as db:
            if self_username(db, candidates[selected].name) != login:
                raise ProbeError("source_identity_changed")
            yield db, candidates[selected]
    if windows.select(login) != (pid, born):
        raise ProbeError("source_identity_changed")


def browse(login, kind, after):
    with _contacts(login) as (db, _):
        return contact_page(db, login, kind, after, 100)


def verify(login, targets, *, include_directory=False):
    with _contacts(login) as (db, directory):
        result = verify_contacts(db, login, targets)
        if include_directory:
            result["directory"] = str(directory)
        return result
