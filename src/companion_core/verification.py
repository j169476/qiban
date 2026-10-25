"""One-use control proofs, independent of inferred identity and model decisions."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from .access import AccessGrant
from .errors import AssistantError


def _hash(challenge, token):
    return hashlib.sha256((challenge + ":" + token).encode()).hexdigest()


class VerificationService:
    def __init__(self, repository, authority):
        self.repository, self.authority = repository, authority

    def _private(self, request):
        self.authority.require(request)
        if request.audience != "private" or not request.actor or request.recipients != {request.actor}:
            raise AssistantError("verification_scope", "账号控制权验证需要本人已认证的私密会话")

    def begin(self, request, other_actor, resources, *, purposes=("reply",)):
        from .provenance import reserved

        self._private(request)
        if (other_actor.key == request.actor or not 1 <= len(resources) <= 100
                or not purposes or not set(purposes) <= {"reply", "maintenance"}):
            raise AssistantError("verification_scope", "验证必须指定另一个账号和有限资料范围")
        for resource in resources:
            policy = self.repository.policy(resource)
            if (reserved(resource) or not policy or request.actor not in policy.owners or not policy.valid
                    or policy.classification not in ("personal", "restricted")):
                raise AssistantError("verification_scope", "只能授权本人已确认归属的资料")
        identifier, left, right = (secrets.token_urlsafe(24) for _ in range(3))
        with self.repository.store.db() as db:
            db.execute("INSERT INTO governance_challenges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (identifier, request.actor, other_actor.key, _hash(identifier, left), _hash(identifier, right),
                        json.dumps(sorted(set(resources))), json.dumps(sorted(set(purposes))),
                        self.repository.clock() + 600, self.repository.epoch(), 0, 0, 0, 0))
            self.repository.audit(request.actor, "verification_begin", identifier, db=db)
        # A transport displays each code only to the requesting local user, never
        # in a model prompt, log or public message. Each answer must arrive on the
        # actual authenticated account it proves, not an actor ID in arguments.
        return {"id": identifier, "left_code": left, "right_code": right, "expires_in": 600}

    def prove(self, request, identifier, code):
        self._private(request)
        outcome, failure = None, None
        with self.repository.store.lock, self.repository.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM governance_challenges WHERE id=?", (identifier,)).fetchone()
            if (not row or row["consumed"] or row["epoch"] != self.repository.epoch()
                    or row["expires_at"] <= self.repository.clock() or row["attempts"] >= 5
                    or request.actor not in (row["left_actor"], row["right_actor"])):
                failure = "验证已失效或不属于当前账号"
            else:
                side = "left" if request.actor == row["left_actor"] else "right"
                if not isinstance(code, str) or len(code) > 256 or not hmac.compare_digest(row[side + "_hash"], _hash(identifier, code)):
                    db.execute("UPDATE governance_challenges SET attempts=attempts+1 WHERE id=?", (identifier,))
                    failure = "验证代码不正确或已失效"
                else:
                    db.execute("UPDATE governance_challenges SET " + side + "_verified=1 WHERE id=?", (identifier,))
                    other = "right" if side == "left" else "left"
                    if row[other + "_verified"]:
                        resources = frozenset(json.loads(row["resources"]))
                        policies = [self.repository.policy(resource) for resource in resources]
                        if any(not p or not p.valid or row["left_actor"] not in p.owners
                               or p.classification in ("secret", "unclassified") for p in policies):
                            failure = "验证期间资料范围已改变"
                        else:
                            grant = AccessGrant(secrets.token_hex(16), row["right_actor"], row["left_actor"], resources,
                                                frozenset(json.loads(row["purposes"])), "account_control", identifier,
                                                self.repository.clock() + 86400 * 30, self.repository.epoch())
                            self.repository.put_grant(grant, request.actor, db=db)
                            outcome = {"verified": True, "grant_id": grant.id, "resources": sorted(resources)}
                        db.execute("UPDATE governance_challenges SET consumed=1 WHERE id=?", (identifier,))
                    else:
                        outcome = {"verified": False, "waiting_for_other_account": True}
        # Raise after committing the attempt count. Failed guesses must not roll
        # their own rate-limit update back with the transaction.
        if failure:
            raise AssistantError("verification_denied", failure)
        return outcome
