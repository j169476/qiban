"""One authenticated management API for both browser and control chat."""
from companion_core.reply_authorization import (
    PLATFORMS,
    ReplyAuthorizations,
    ReplyScope,
    ReplyTarget,
    require,
)


class ReplyManagement:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.permissions = ReplyAuthorizations(self.store)

    def targets(self):
        result = {}
        platforms = {p: {"available": False} for p in sorted(PLATFORMS)}
        for platform, core in self.service.platform_runs.cores.items():
            if platform not in PLATFORMS:
                continue
            for adapter in core.platforms.all():
                discover = getattr(adapter, "reply_targets", None)
                if discover is None:
                    continue
                try:
                    candidates = tuple(discover())
                    require(len(candidates) <= 1000, "reply_target_invalid")
                    validated = {}
                    for target in candidates:
                        require(isinstance(target, ReplyTarget) and target.scope.platform == platform
                                and target.scope.id not in result and target.scope.id not in validated,
                                "reply_target_invalid")
                        validated[target.scope.id] = target
                except Exception:
                    # A broken connector cannot block revocation or other platforms.
                    # Never expose its raw errors (which may contain source paths).
                    platforms[platform]["discovery_failed"] = True
                    continue
                platforms[platform]["available"] = True
                result.update(validated)
        return result, platforms

    def listing(self, request):
        self.service.admin.authority.require(request, administrator=True)
        targets, platforms = self.targets()
        records = {r["id"]: r for r in self.permissions.records()}
        for key, target in targets.items():
            records[key] = self.permissions.record(target.scope)
        items = []
        for key, record in records.items():
            target = targets.get(key)
            items.append({**record, "verified_target": target is not None,
                "label": target.label if target else "当前连接器未提供此会话",
                "account_label": target.account_label if target else record["scope"]["account_identity"],
                "restriction": target.restriction if target else "只可撤销，重新授权需要连接器核验"})
        return {"items": sorted(items, key=lambda x: (x["scope"]["platform"], x["id"])),
                "platforms": platforms,
                "default_allow": False, "change_pauses_platform": True}

    def update(self, request, *, scope_id, enabled, expected_version):
        authority = self.service.admin.authority
        authority.require(request, administrator=True)
        require(isinstance(scope_id, str) and type(enabled) is bool)
        # Same service lock order as lifecycle control; final native submission
        # and mutation additionally share the store lock through the core API.
        with self.service.state_lock, self.store.lock:
            authority.require(request, administrator=True)
            targets, _platforms = self.targets()
            target = targets.get(scope_id)
            if enabled:
                require(target is not None, "reply_target_unverified")
                scope = target.scope
            else:
                saved = next((r for r in self.permissions.records() if r["id"] == scope_id), None)
                require(target is not None or saved is not None, "reply_target_unverified")
                scope = target.scope if target else ReplyScope(**saved["scope"])
            previous = self.permissions.record(scope)
            require(previous["version"] == expected_version, "edit_conflict")
            changed = previous["version"] is None or previous["enabled"] != enabled
            core = self.service.platform_runs.cores.get(scope.platform)
            paused = False
            if changed and core and self.store.state(core.run_key, {}).get("status") == "active":
                # Cancel native preparations before committing new permissions.
                # If stopping fails, no permission change is reported as success.
                core.stop("reply_authorization_changed")
                paused = True
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                record = self.permissions.update(scope, enabled=enabled, expected_version=expected_version,
                                                 actor=request.actor, db=db)
                if changed:
                    self.service.admin.repository.audit(request.actor, "reply_authorization", scope.id,
                                                         previous["version"], record["version"], db=db)
            return {"authorization": record, "platform_paused": paused, "role_started": False}

    def operation(self, request, name, arguments):
        self.service.admin.authority.require(request, administrator=True)
        require(isinstance(arguments, dict))
        if name.startswith("reply_policy_"):
            return self.policy_operation(request, name, arguments)
        if name == "reply_authorization_list":
            require(not arguments)
            return self.listing(request)
        require(name == "reply_authorization_set" and set(arguments) == {"scope_id", "enabled", "expected_version"})
        return self.update(request, **arguments)

    def policy_operation(self, request, name, arguments):
        from companion_core.reply_policy import BOUNDS, Participation, ReplyPolicies

        policies = ReplyPolicies(self.store)
        targets, platforms = self.targets()
        accounts = {(t.scope.platform, t.scope.account_identity): t for t in targets.values()}
        if name == "reply_policy_list":
            require(not arguments)
            participation = Participation(self.store)
            groups = []
            for target in targets.values():
                scope = target.scope
                if scope.kind != "group":
                    continue
                policy = policies.get(scope.platform, scope.account_identity)
                window = self.store.state(participation.window_key(scope), {})
                run = self.service.platform_runs.status(scope.platform)
                active = (window.get("status") == "active" and window.get("expires", 0) > participation.now()
                          and window.get("generation") == run.get("generation") and run.get("status") == "active"
                          and window.get("policy") == policy["version"]
                          and window.get("permission") == self.permissions.version(scope))
                groups.append({"platform": scope.platform, "label": target.label, "active": active,
                    "settings": policy["settings"], "used": participation.spent(window),
                    "decisions": window.get("decisions", 0), "expires": window.get("expires"),
                    "reply_limit": window.get("reply_limit", policy["settings"]["group_session_replies"])})
            return {"items": [policies.get(platform, account) | {"account_label": target.account_label,
                       "private_usage": Participation(self.store).usage(target.scope)[1]}
                    for (platform, account), target in sorted(accounts.items())], "bounds": BOUNDS,
                    "platforms": platforms, "private_quota_scope": "platform_account_calendar_day", "groups": groups}
        require(name == "reply_policy_set" and set(arguments) == {
            "platform", "account_identity", "changes", "expected_version"})
        platform, account = arguments["platform"], arguments["account_identity"]
        require(isinstance(platform, str) and isinstance(account, str), "reply_policy_invalid")
        with self.service.state_lock, self.store.lock:
            self.service.admin.authority.require(request, administrator=True)
            fresh, _ = self.targets()
            require(any((t.scope.platform, t.scope.account_identity) == (platform, account) for t in fresh.values()),
                    "reply_target_unverified")
            # Validate and commit before pause while holding the same submission
            # lock. Changed policy versions immediately invalidate old tickets.
            before = policies.get(platform, account)
            saved = policies.update(platform, account, arguments["changes"], arguments["expected_version"], request.actor)
            changed = before["version"] != saved["version"]
            core = self.service.platform_runs.cores.get(platform)
            paused = bool(changed and core and self.store.state(core.run_key, {}).get("status") == "active")
            if paused:
                core.stop("reply_policy_changed")
            return {"policy": saved, "platform_paused": paused, "role_started": False}
