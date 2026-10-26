"""Explicit append-only authorization imports with expiring, single-use previews."""
import secrets
import time

from companion_core.reply_authorization import require

PREFIX = "reply-import-preview:"


class ReplyImport:
    def __init__(self, management, *, clock=time.time):
        self.management, self.service, self.store = management, management.service, management.store
        self.clock = clock

    def preview(self, request, *, platform, identifiers, account_identity=None):
        self.service.admin.authority.require(request, administrator=True)
        require(platform in ("qq", "wechat") and isinstance(identifiers, list) and 1 <= len(identifiers) <= 200)
        require(account_identity is None or isinstance(account_identity, str))
        require(all(isinstance(i, str) and 0 < len(i.strip()) <= 256 for i in identifiers))
        identifiers = [i.strip() for i in identifiers]
        targets, _ = self.management.targets()
        items = []
        for identifier in dict.fromkeys(identifiers):
            matches = [t for t in targets.values() if t.scope.platform == platform and t.scope.target_id == identifier
                       and (account_identity is None or t.scope.account_identity == account_identity)]
            if len(matches) != 1:
                items.append({"identifier": identifier, "status": "unverified" if not matches else "ambiguous"})
                continue
            target = matches[0]
            record = self.management.permissions.record(target.scope)
            items.append({"identifier": identifier, "status": "already_authorized" if record["enabled"] else "ready",
                          "id": record["id"], "scope": record["scope"], "expected_version": record["version"],
                          "label": target.label, "account_label": target.account_label})
        token = secrets.token_hex(24)
        expires = self.clock() + 600
        value = {"actor": request.actor, "expires": expires, "items": items, "platform": platform,
                 "confirmed": False, "confirmable": all(i["status"] in {"ready", "already_authorized"} for i in items)}
        with self.store.lock, self.store.db() as db:
            self.store.set_state(PREFIX + token, value, db)
            # Only expiring preview metadata; never remove grants or connector registrations.
            rows = db.execute("SELECT key FROM state WHERE key LIKE ?", (PREFIX + "%",)).fetchall()
            for row in rows:
                if self.store.state(row["key"], {}, db).get("expires", 0) < self.clock():
                    db.execute("DELETE FROM state WHERE key=?", (row["key"],))
        return {"token": token, "expires": expires, "items": items, "confirmable": value["confirmable"],
                "duplicates_removed": len(identifiers) - len(items), "mode": "append", "permissions_changed": False}

    def confirm(self, request, *, token):
        authority = self.service.admin.authority
        authority.require(request, administrator=True)
        require(isinstance(token, str) and len(token) == 48)
        with self.service.state_lock, self.store.lock:
            value = self.store.state(PREFIX + token)
            require(value and value["actor"] == request.actor and value["expires"] > self.clock()
                    and not value["confirmed"] and value["confirmable"], "reply_import_expired")
            targets, _ = self.management.targets()
            for item in value["items"]:
                target = targets.get(item["id"])
                require(target is not None and self.management.permissions.record(target.scope)["scope"] == item["scope"],
                        "reply_target_unverified")
                require(self.management.permissions.record(target.scope)["version"] == item["expected_version"], "edit_conflict")
            changed = sum(i["status"] == "ready" for i in value["items"])
            core = self.service.platform_runs.cores.get(value["platform"])
            paused = bool(changed and core and self.store.state(core.run_key, {}).get("status") == "active")
            if paused:
                core.stop("reply_authorization_changed")
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                authority.require(request, administrator=True)
                for item in value["items"]:
                    saved = self.management.permissions.update(targets[item["id"]].scope, enabled=True,
                        expected_version=item["expected_version"], actor=request.actor, db=db)
                    if item["status"] == "ready":
                        self.service.admin.repository.audit(request.actor, "reply_authorization", item["id"],
                                                            item["expected_version"], saved["version"], db=db)
                self.store.set_state(PREFIX + token, {**value, "confirmed": True}, db)
            return {"mode": "append", "added": changed, "already_authorized": len(value["items"]) - changed,
                    "platform_paused": paused, "role_started": False}
