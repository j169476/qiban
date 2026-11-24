CREATE TABLE role_reminders (
 id text PRIMARY KEY, scope text NOT NULL, platform text NOT NULL, account text NOT NULL,
 due double precision NOT NULL, status text NOT NULL, version text NOT NULL, body text NOT NULL,
 attempt text, wake_after double precision NOT NULL DEFAULT 0
);
CREATE INDEX role_reminders_tick ON role_reminders (platform, account, status, due, wake_after);
CREATE INDEX role_reminders_scope ON role_reminders (scope, status, due);
CREATE INDEX role_reminders_pending ON role_reminders (status, due, id);
