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


def account_details(login):
    """Only the exact verified self row; never enumerate contacts or messages."""
    with _contacts(login) as (db, _):
        columns = {row[1] for row in db.execute("PRAGMA table_info(contact)")}
        if not {"username", "nick_name", "alias"} <= columns:
            raise ProbeError("contact_schema_unsupported")
        rows = db.execute("SELECT username,nick_name,alias FROM contact "
                          "WHERE username=? COLLATE BINARY LIMIT 2", (login,)).fetchall()
        if len(rows) != 1 or rows[0][0] != login:
            raise ProbeError("source_identity_changed")
        def label(value):
            return value.strip() if isinstance(value, str) and len(value) <= 200 and not any(
                ord(c) < 32 or 0x7F <= ord(c) < 0xA0 for c in value) else ""
        result = {"login": login, "nickname": label(rows[0][1]), "wechat_id": label(rows[0][2])}
    # _contacts verifies the selected process identity again on exit.
    return result


def verify(login, targets, *, include_directory=False):
    with _contacts(login) as (db, directory):
        result = verify_contacts(db, login, targets)
        if include_directory:
            result["directory"] = str(directory)
        return result


def directory_page(db, login, kind, query, page):
    """One read-only directory snapshot, then filter and paginate verified rows."""
    if (kind not in ("private", "group") or not isinstance(query, str) or len(query) > 100
            or type(page) is not int or not 1 <= page <= 10000):
        raise ProbeError("configuration_invalid")
    from .db_source import _contact_kind

    query = query.strip().casefold()
    matches = []
    rows = db.execute("SELECT username,local_type,flag,delete_flag,verify_flag,is_in_chat_room,"
                      "remark,nick_name FROM contact WHERE length(username) BETWEEN 1 AND 256 "
                      "GROUP BY username COLLATE BINARY HAVING count(*)=1 AND local_type=? "
                      "ORDER BY username COLLATE BINARY", (1 if kind == "private" else 2,))
    try:
        for username, *fields, remark, nickname in rows:
            if _contact_kind(login, username, *fields) != kind:
                continue
            if username in {"filehelper", "fmessage", "medianote", "newsapp", "weixin", "floatbottle"}:
                continue
            labels = [v[:200] for v in (remark, nickname) if isinstance(v, str) and v]
            if query and not any(query in v.casefold() for v in [username, *labels]):
                continue
            matches.append({"username": username, "name": next(iter(labels), "")})
    finally:
        rows.close()
    pages = max(1, (len(matches) + 9) // 10)
    page = min(page, pages)
    return {"items": matches[(page-1)*10:page*10], "page": page, "pages": pages,
            "total": len(matches), "page_size": 10}


def browse_page(login, kind, query, page):
    with _contacts(login) as (db, _):
        result = directory_page(db, login, kind, query, page)
    return result


def directory_snapshot(db, login):
    """Read both contact kinds once; preserve all human-searchable names."""
    from .db_source import _contact_kind
    items = []
    account = {'login': login, 'nickname': '', 'wechat_id': ''}
    rows = db.execute("SELECT username,local_type,flag,delete_flag,verify_flag,is_in_chat_room,"
                      "remark,nick_name,alias FROM contact WHERE length(username) BETWEEN 1 AND 256 "
                      "GROUP BY username COLLATE BINARY HAVING count(*)=1 ORDER BY username COLLATE BINARY")
    try:
        for username, *fields, remark, nickname, alias in rows:
            if username == login:
                account.update(nickname=nickname[:200].strip() if isinstance(nickname, str) else '',
                               wechat_id=alias[:200].strip() if isinstance(alias, str) else '')
            kind = _contact_kind(login, username, *fields)
            if kind not in ('private', 'group') or username in {
                    'filehelper', 'fmessage', 'medianote', 'newsapp', 'weixin', 'floatbottle'}:
                continue
            def label(value):
                return value[:200].strip() if isinstance(value, str) else ''
            items.append(dict(id=username, kind=kind, name=label(remark) or label(nickname),
                              nickname=label(nickname), alias=label(alias)))
    finally:
        rows.close()
    return {'items': items, 'account': account}


def browse_snapshot(login):
    with _contacts(login) as (db, _):
        result = directory_snapshot(db, login)
    return result
