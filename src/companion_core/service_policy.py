"""Optional service settings shared by platform profiles, without migrating user values."""

PROPERTIES = {
    "daily_local_replies": {"type": "integer", "minimum": 0, "maximum": 10000},
    "auto_backup": {"type": "boolean"},
    "backup_interval_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
}
