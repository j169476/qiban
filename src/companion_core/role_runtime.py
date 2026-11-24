"""Role entry points. Platform pollers submit input and the timer selects durable work."""

from .errors import AssistantError


class RoleRuntime:
    def __init__(self, core):
        self.core = core
        self.cores = None

    async def react(self, trigger, *, event=None, adapter=None, cores=None):
        core = self.core
        run = core.guard()
        if trigger == "message":
            return await core.respond(event, run)
        if run["read_only"] or run.get("reply_test"):
            return
        if trigger == "reminder":
            from .reminder_runner import tick

            return await tick(core, adapter)
        if trigger == "matter":
            from .matter_runner import tick

            return await tick(core, adapter, cores)
        if trigger == "public_timer":
            return await adapter.public_wake(core)
        raise AssistantError("role_trigger", "未知的角色事件")

    async def wake(self, adapter, cores):
        """Pick the earliest ready item across typed stores. Old reminder IDs and receipts stay authoritative."""
        now = self.core.clock().timestamp()
        key = (adapter.platform, adapter.account, now)
        with self.core.store.db() as db:
            row = db.execute(
                "SELECT kind FROM (SELECT 'matter' kind,wake due,id FROM role_matters "
                "WHERE platform=? AND account=? AND wake<=? AND (status IN ('ready','processing') OR "
                "(status='waiting' AND wake>0)) UNION ALL SELECT 'reminder',GREATEST(due,wake_after),id "
                "FROM role_reminders WHERE platform=? AND account=? AND GREATEST(due,wake_after)<=? "
                "AND status IN ('pending','processing')) q ORDER BY due,id LIMIT 1",
                (*key, *key),
            ).fetchone()
        if row:
            await self.react(row[0], adapter=adapter, cores=cores)
