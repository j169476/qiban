CREATE TABLE preset_reply_turns (
    kind TEXT NOT NULL DEFAULT 'conversation_turns' CHECK (kind = 'conversation_turns'),
    id TEXT NOT NULL,
    preset_id TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (kind, id),
    FOREIGN KEY (kind, id) REFERENCES content_records(kind, id) ON DELETE CASCADE
);
CREATE INDEX preset_reply_page ON preset_reply_turns(preset_id, sent_at DESC, id DESC);
