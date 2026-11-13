"""All persisted run slots; recovery must never resurrect an external platform."""

PLATFORMS = ("bilibili", "wechat", "qq")
DESKTOP_PLATFORMS = (*PLATFORMS, "local")
RUN_KEYS = ("run", "core-run", *("core-run:" + platform for platform in DESKTOP_PLATFORMS),
            "core-run:manual", "core-run:manual-video")


def active(store):
    return any(store.state(key, {}).get("status") == "active" for key in RUN_KEYS)
