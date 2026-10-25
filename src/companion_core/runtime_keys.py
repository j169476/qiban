"""All persisted run slots; recovery must never resurrect an external platform."""

PLATFORMS = ("bilibili", "wechat", "qq")
RUN_KEYS = ("run", "core-run", *("core-run:" + platform for platform in PLATFORMS))


def active(store):
    return any(store.state(key, {}).get("status") == "active" for key in RUN_KEYS)
