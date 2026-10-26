"""Non-secret, bounded local connection intent. Discovery is separate."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from companion_core.reply_authorization import require


def qq_id(value):
    require(type(value) in (str, int) and re.fullmatch(r"[1-9][0-9]{4,19}", str(value)) is not None, "qq_identity_invalid")
    return str(value)


def local_url(value, scheme):
    require(isinstance(value, str) and len(value) <= 200, "qq_endpoint_invalid")
    parts = urlsplit(value)
    require(parts.scheme == scheme and parts.hostname == "127.0.0.1" and parts.port is not None
            and 1024 <= parts.port <= 65535 and not (parts.username or parts.password or parts.query or parts.fragment)
            and parts.path in {"", "/", "/event"}, "qq_endpoint_invalid")
    return value.rstrip("/")


def validate(value):
    require(isinstance(value, dict) and set(value) == {"version", "account", "private_targets", "groups", "http", "ws"}
            and value["version"] == 1, "qq_configuration")
    result = {**value, "account": qq_id(value["account"]), "http": local_url(value["http"], "http"),
              "ws": local_url(value["ws"], "ws")}
    for field in ("private_targets", "groups"):
        require(isinstance(value[field], list), "qq_configuration")
        result[field] = [qq_id(v) for v in value[field]]
        require(len(set(result[field])) == len(result[field]), "qq_configuration")
    require(result["account"] not in result["private_targets"], "qq_configuration")
    return result


def load(project):
    path = Path(project) / ".dev/qq/module.json"
    if not path.exists():
        return None
    require(path.stat().st_size < 1048576, "qq_configuration")
    return validate(json.loads(path.read_text(encoding="utf-8")))


def credential_key(project, account):
    from companion_core.contracts import stable_key

    return stable_key("qq-onebot", str(Path(project).resolve()), qq_id(account))


def management_url(project):
    """Read only the local NapCat listener settings, never its authentication token."""
    path = Path(project) / ".dev/qq/runtime/config/webui.json" if project else None
    try:
        if path is None or not path.exists():
            return "http://127.0.0.1:6099/webui"
        if path.stat().st_size > 16000:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if (not isinstance(value, dict) or type(value.get("port")) is not int
                or not 1024 <= value["port"] <= 65535
                or value.get("host", "127.0.0.1") not in ("127.0.0.1", "localhost", "0.0.0.0", "::")):
            return None
        return f"http://127.0.0.1:{value['port']}/webui"
    except (OSError, ValueError):
        return None


def token(project, account):
    import keyring

    value = keyring.get_password("local-companion-qq", credential_key(project, account))
    require(isinstance(value, str) and 16 <= len(value) <= 512, "qq_token_required")
    return value
