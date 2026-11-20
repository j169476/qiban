"""Non-secret, bounded local connection intent. Discovery is separate."""
import json
import re
from pathlib import Path

from companion_core.reply_authorization import require


def qq_id(value):
    require(type(value) in (str, int) and re.fullmatch(r"[1-9][0-9]{4,19}", str(value)) is not None, "qq_identity_invalid")
    return str(value)


def validate(value):
    require(isinstance(value, dict), "qq_configuration")
    return validate_native(value)


def validate_native(value):
    fields = {"version", "transport", "account", "uid", "private_targets", "groups", "runtime", "data"}
    require(set(value) == fields and type(value["version"]) is int and value["version"] == 2
            and value["transport"] == "native",
            "qq_configuration")
    require(isinstance(value["uid"], str) and re.fullmatch(r"u_.{1,126}", value["uid"]), "qq_identity_invalid")
    result = {**value, "account": qq_id(value["account"])}
    for field in ("private_targets", "groups"):
        require(isinstance(value[field], list), "qq_configuration")
        result[field] = [qq_id(v) for v in value[field]]
        require(len(set(result[field])) == len(result[field]), "qq_configuration")
    require(result["account"] not in result["private_targets"], "qq_configuration")
    for field in ("runtime", "data"):
        require(isinstance(value[field], str) and 0 < len(value[field]) <= 2048 and "\x00" not in value[field]
                and Path(value[field]).is_absolute(), "qq_native_resources_path")
    return result


def native_parameters(options):
    value = validate_native(options)
    return {"account": {"uin": value["account"], "uid": value["uid"]},
            **{key: value[key] for key in ("private_targets", "groups", "runtime", "data")}}


def load(project):
    path = Path(project) / ".dev/qq/module.json"
    if not path.exists():
        return None
    require(path.stat().st_size < 1048576, "qq_configuration")
    return validate(json.loads(path.read_text(encoding="utf-8")))
