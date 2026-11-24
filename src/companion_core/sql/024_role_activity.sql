CREATE TABLE role_activity (
    matter text PRIMARY KEY REFERENCES role_matters(id) ON DELETE CASCADE,
    updated_at double precision NOT NULL
);
CREATE INDEX role_activity_recent ON role_activity(updated_at DESC,matter);
