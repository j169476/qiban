"""Data contracts, shared by file validation, CLI and MCP."""

from copy import deepcopy
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator

from companion_core.algorithm_policy import SCHEMA as ALGORITHM_SCHEMA
from companion_core.context_budget import PROPERTIES as CONTEXT_BUDGET_PROPERTIES
from companion_core.service_policy import PROPERTIES as SERVICE_POLICY
from companion_core.temporal_facts import FACT as PERSONAL_FACT

from .errors import AssistantError

ACTIONS = ("comment", "reply", "dm_reply", "dynamic", "like", "favorite", "follow", "coin", "comment_like")
CONFIG = ("manifest", "persona", "preferences", "policy", "companion")
MEMORY_COLLECTIONS = ("memories", "knowledge", "relationships", "diaries", "skills", "interest_signals")
COLLECTIONS = (*MEMORY_COLLECTIONS, "conversation_turns", "emotion_events",
               "conversation_summaries", "semantic_groups", "browse_observations",
               "source_snapshots", "knowledge_reviews", "method_trials",
               "learning_goals", "learning_checks", "learning_nodes", "learning_reviews", "comment_pages",
               "publication_images", "native_jobs", "video_progress")
STR = {"type": "string", "maxLength": 16000}
UID = {"type": "string", "pattern": r"^[0-9]{1,20}$"}
ID = {"type": "string", "pattern": r"^[A-Za-z0-9_-]{1,80}$"}
STRINGS = {"type": "array", "items": STR, "maxItems": 100}


def obj(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


def document(properties, required=None):
    props = {"schema_version": {"const": 1}, **properties}
    return obj(props, ["schema_version"] + (list(properties) if required is None else required))


INTEREST_TOPICS = {
    "type": "array", "maxItems": 6, "minItems": 1,
    "items": obj({
        "topic": {"type": "string", "minLength": 1, "maxLength": 100},
        "sentiment": {"enum": ["interested", "not_interested", "uncertain"]},
    }),
}
SOURCE = obj(
    {
        "type": {"enum": ["user_instruction", "conversation", "video", "interaction", "reflection", "web"]},
        "recorded_at": {"type": "string", "format": "date-time"},
        "reference": STR,
    },
    ["type", "recorded_at", "reference"],
)
COVERAGE = obj(
    {
        "kind": {"enum": ["metadata_only", "subtitles", "transcription", "frames", "web"]},
        "bvid": {"type": "string", "pattern": "^(?:BV[0-9A-Za-z]{10})?$"},
        "start_seconds": {"type": "number", "minimum": 0},
        "end_seconds": {"type": "number", "minimum": 0},
        "duration_seconds": {"type": "number", "minimum": 0},
        "complete": {"type": "boolean"},
    }
)
EMOTION_CHANGE = obj({
    "label": {"type": "string", "minLength": 1, "maxLength": 60},
    "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
    "valence_delta": {"type": "number", "minimum": -0.2, "maximum": 0.2},
    "arousal_delta": {"type": "number", "minimum": -0.2, "maximum": 0.2},
    "evidence_ids": {
        "type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200},
        "minItems": 1, "maxItems": 10,
    },
})
EMOTION_STATE = obj({
    "label": {"type": "string", "maxLength": 60},
    "reason": {"type": "string", "maxLength": 1000},
    "valence": {"type": "number", "minimum": -1, "maximum": 1},
    "arousal": {"type": "number", "minimum": 0, "maximum": 1},
    "updated_at": {"type": "string", "format": "date-time"},
    "account_uid": UID,
    "event_id": ID,
    "day": {"type": "string", "format": "date"},
    "change_today": {"type": "number", "minimum": 0},
})
VERSION = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
TURN_REF = obj({"id": ID, "version": VERSION})
MEMORY_REF = obj({"kind": {"enum": list(MEMORY_COLLECTIONS)}, "id": ID, "version": VERSION})
QUOTE = obj({"source_id": ID, "quote": {"type": "string", "minLength": 1, "maxLength": 1200}})
REVIEW_RESULT = obj({
    "summary": {"type": "string", "maxLength": 3000},
    "assessment": {"enum": ["supported_by_material", "needs_evidence", "misstated"]},
    "claims": {"type": "array", "minItems": 1, "maxItems": 8, "items": obj({
        "claim": {"type": "string", "maxLength": 2000},
        "kind": {"enum": ["fact", "opinion", "inference", "method"]},
        "assessment": {"enum": ["supported_by_material", "needs_evidence", "misstated"]},
        "conditions": {"type": "string", "maxLength": 1500},
        "evidence": {"type": "array", "items": QUOTE, "maxItems": 6},
    })},
    "comparisons": {"type": "array", "maxItems": 3, "items": obj({
        "other_id": ID,
        "relation": {"enum": ["equivalent", "different_conditions", "time_update", "contradiction", "unrelated"]},
        "resolution": {"enum": ["unresolved", "prefer_target", "prefer_other", "both_conditional"]},
        "reason": {"type": "string", "maxLength": 2000},
        "evidence": {"type": "array", "items": QUOTE, "maxItems": 6},
    })},
    "follow_up": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 6},
})
GOAL_CRITERIA = {"type": "array", "minItems": 1, "maxItems": 6,
                 "items": {"type": "string", "minLength": 1, "maxLength": 1000}}
QUESTION = obj({
    "id": ID, "criterion": {"type": "integer", "minimum": 0, "maximum": 5},
    "kind": {"enum": ["recall", "transfer", "counterexample"]},
    "question": {"type": "string", "minLength": 1, "maxLength": 2000},
    "expected": {"type": "string", "minLength": 1, "maxLength": 3000},
    "rubric": {"type": "string", "minLength": 1, "maxLength": 1500},
    "evidence": {"type": "array", "minItems": 1, "maxItems": 4, "items": QUOTE},
})
ANSWER = obj({"id": ID, "answer": {"type": "string", "maxLength": 4000}})
GRADE = obj({
    "id": ID, "passed": {"type": "boolean"}, "reason": {"type": "string", "maxLength": 2000},
    "gap": {"type": "string", "maxLength": 1000},
})
ATTACHMENTS = {"type": "array", "maxItems": 12, "items": obj({
    "kind": {"enum": ["image", "video_card", "link_card"]},
    "url": {"type": "string", "maxLength": 2000}, "title": {"type": "string", "maxLength": 500},
    "bvid": {"type": "string", "pattern": "^(?:BV[0-9A-Za-z]{10})?$"},
})}
COMMENT = obj({
    "rpid": UID, "uid": UID, "display_name": {"type": "string", "maxLength": 120},
    "root": {"type": "string", "pattern": "^[0-9]{0,20}$"},
    "parent": {"type": "string", "pattern": "^[0-9]{0,20}$"},
    "text": STR, "received": {"type": ["string", "null"], "format": "date-time"},
    "attachments": ATTACHMENTS,
})
COMMENT["properties"]["text_truncated"] = {"type": "boolean"}
SCHEMAS = {
    "manifest": document(
        {
            "project_id": ID,
            "account_uid": {"type": "string", "pattern": r"^[0-9]{0,20}$"},
            "credential_ref": ID,
        }
    ),
    "persona": document(
        {
            "id": ID,
            "name": STR,
            "core": obj({"description": STR, "speaking_style": STRINGS}),
            "interests": STRINGS,
            "locked_fields": STRINGS,
        }
    ),
    "preferences": document(
        {
            "topics": STRINGS,
            "blocked_topics": STRINGS,
            "blocked_uids": {"type": "array", "items": UID, "maxItems": 1000},
        }
    ),
    "growth": document(
        {
            "interests": {
                "type": "object",
                "maxProperties": 100,
                "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "mood": STR,
            "reason": STR,
            "traits": STRINGS,
            "speech_habits": STRINGS,
            "opinions": STRINGS,
            "last_reflection_at": {"type": "string", "format": "date-time"},
            "last_reflection_id": ID,
            "emotion": EMOTION_STATE,
        },
        ["interests", "mood", "reason"],
    ),
    "companion": document(
        {
            "select_by_interest": {"type": "boolean"},
            "guide_uid": {"type": "string", "pattern": "^[0-9]{0,20}$"},
            "exploration_every": {"type": "integer", "minimum": 0, "maximum": 20},
            "feed_source": {"enum": ["following", "recommended"]},
            "candidate_limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "context_char_limit": {"type": "integer", "minimum": 4000, "maximum": 100000},
            "context_record_limit": {"type": "integer", "minimum": 6, "maximum": 60},
            "catalog_limit": {"type": "integer", "minimum": 10, "maximum": 200},
            "auto_relationships": {"type": "boolean"},
            "affinity_daily_change": {"type": "integer", "minimum": 0, "maximum": 30},
            "extract_skills": {"type": "boolean"},
            "daily_reflection": {"type": "boolean"},
            "reflection_hour": {"type": "integer", "minimum": 0, "maximum": 23},
            "reflection_min_memories": {"type": "integer", "minimum": 1, "maximum": 50},
            "visuals_enabled": {"type": "boolean"},
            "visuals_max_images": {"type": "integer", "minimum": 1, "maximum": 4},
            "visual_scene_detection": {"type": "boolean"},
            "visual_scene_threshold": {"type": "number", "minimum": 0.05, "maximum": 0.9},
            "visual_frame_dedup": {"type": "boolean"},
            "subtitle_char_limit": {"type": "integer", "minimum": 2000, "maximum": 100000},
            "conversation_context_turns": {"type": "integer", "minimum": 4, "maximum": 100},
            "conversation_context_chars": {"type": "integer", "minimum": 1000, "maximum": 100000},
            "conversation_recall_enabled": {"type": "boolean"},
            "emotion_enabled": {"type": "boolean"},
            "emotion_step_limit": {"type": "number", "minimum": 0, "maximum": 0.2},
            "emotion_daily_change": {"type": "number", "minimum": 0, "maximum": 4},
            "emotion_half_life_minutes": {"type": "integer", "minimum": 15, "maximum": 1440},
            "memory_maintenance": {"type": "boolean"},
            "memory_maintenance_min_records": {"type": "integer", "minimum": 1, "maximum": 200},
            "conversation_summary_after": {"type": "integer", "minimum": 12, "maximum": 200},
            "conversation_keep_recent": {"type": "integer", "minimum": 4, "maximum": 50},
            "search_expansion": {"type": "boolean"},
            "search_daily_limit": {"type": "integer", "minimum": 0, "maximum": 30},
            "search_cooldown_minutes": {"type": "integer", "minimum": 15, "maximum": 1440},
            "diversity_window_days": {"type": "integer", "minimum": 1, "maximum": 30},
            "diversity_min_samples": {"type": "integer", "minimum": 3, "maximum": 50},
            "topic_max_share": {"type": "number", "minimum": 0.2, "maximum": 1},
            "author_max_share": {"type": "number", "minimum": 0.2, "maximum": 1},
            "knowledge_review": {"type": "boolean"},
            "review_daily_limit": {"type": "integer", "minimum": 0, "maximum": 10},
            "review_interval_days": {"type": "integer", "minimum": 1, "maximum": 90},
            "learning_round_limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "comment_context_pages": {"type": "integer", "minimum": 1, "maximum": 5},
            "comment_context_chars": {"type": "integer", "minimum": 1000, "maximum": 40000},
            "failure_notifications": {"enum": ["off", "admin_once"]},
            "management_thread_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{0,100}$"},
            "credential_auto_refresh": {"type": "boolean"},
            "credential_check_hours": {"type": "integer", "minimum": 1, "maximum": 24},
            "media_fallback": {"enum": ["auto", "off"]},
            "media_max_seconds": {"type": "integer", "minimum": 10, "maximum": 3600},
            "media_max_megabytes": {"type": "integer", "minimum": 8, "maximum": 512},
            "asr_enabled": {"type": "boolean"},
            "asr_model_path": {"type": "string", "maxLength": 1000},
            "asr_timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 600},
        },
        ["select_by_interest", "guide_uid", "exploration_every", "feed_source", "candidate_limit",
         "context_char_limit", "context_record_limit", "catalog_limit", "auto_relationships",
         "affinity_daily_change", "extract_skills", "daily_reflection", "reflection_hour",
         "reflection_min_memories", "visuals_enabled", "visuals_max_images", "subtitle_char_limit"],
    ),
    "policy": document(
        {
            "mode": {"const": "autonomous"},
            "timezone": STR,
            "active_hours": obj(
                {
                    k: {"type": "string", "pattern": r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"}
                    for k in ("start", "end")
                }
            ),
            "cycle_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
            "items_per_cycle": {"type": "integer", "minimum": 1, "maximum": 10},
            "daily_videos": {"type": "integer", "minimum": 0, "maximum": 100},
            "daily_model_cycles": {"type": "integer", "minimum": 0, "maximum": 288},
            "worker_timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 600},
            "min_action_interval_seconds": {"type": "integer", "minimum": 0, "maximum": 86400},
            "max_reply_turns_per_uid_day": {"type": "integer", "minimum": 0, "maximum": 20},
            "favorite_folder_id": {"type": "integer", "minimum": 0},
            "backup_keep": {"type": "integer", "minimum": 1, "maximum": 90},
            "actions": obj(
                {
                    a: obj(
                        {
                            "enabled": {"type": "boolean"},
                            "daily_limit": {"type": "integer", "minimum": 0, "maximum": 1000},
                        }
                    )
                    for a in ACTIONS
                }, [a for a in ACTIONS if a != "comment_like"]
            ),
        }
    ),
    "memories": document(
        {
            "id": ID,
            "kind": {"enum": ["user_preference", "experience", "reflection"]},
            "content": STR,
            "source": SOURCE,
            "confidence": {"enum": ["explicit", "observed", "inferred"]},
            "tags": STRINGS,
            "locked": {"type": "boolean"},
        }
    ),
    "knowledge": document(
        {
            "id": ID,
            "content": STR,
            "source": SOURCE,
            "coverage": COVERAGE,
            "tags": STRINGS,
            "locked": {"type": "boolean"},
            "disagrees_with": STRINGS,
        }
    ),
    "relationships": document(
        {
            "id": UID,
            "display_name": STR,
            "affinity": {"type": "integer", "minimum": 0, "maximum": 100},
            "interactions": {"type": "integer", "minimum": 0},
            "events": STRINGS,
            "locked": {"type": "boolean"},
            "impression": STR,
            "facts": STRINGS,
            "last_interaction_at": {"type": "string", "format": "date-time"},
            "affinity_day": {"type": "string", "format": "date"},
            "affinity_change_today": {"type": "integer", "minimum": 0},
        },
        ["id", "display_name", "affinity", "interactions", "events", "locked"],
    ),
    "diaries": document(
        {
            "id": ID, "content": STR, "source": SOURCE, "tags": STRINGS, "locked": {"type": "boolean"},
            "memory_refs": {
                "type": "array", "maxItems": 60,
                "items": obj({"id": ID, "version": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}),
            },
        },
        ["id", "content", "source", "tags", "locked"],
    ),
    "skills": document(
        {
            "id": ID,
            "title": STR,
            "content": STR,
            "when_to_use": STR,
            "limitations": STR,
            "knowledge_ids": {"type": "array", "items": ID, "minItems": 1, "maxItems": 10},
            "source": SOURCE,
            "tags": STRINGS,
            "locked": {"type": "boolean"},
        }
    ),
    "interest_signals": document(
        {
            "id": ID,
            "content": STR,
            "source": SOURCE,
            "sender_uid": UID,
            "bvid": {"type": "string", "pattern": r"^(?:BV[0-9A-Za-z]{10})?$"},
            "topics": INTEREST_TOPICS,
            "locked": {"type": "boolean"},
        }
    ),
    "conversation_turns": document({
        "id": ID, "thread_id": ID, "account_uid": UID,
        "channel": {"enum": ["codex", "comment", "dm"]},
        "thread_key": {"type": "string", "minLength": 1, "maxLength": 200},
        "speaker": {"enum": ["user", "assistant"]},
        "sender_uid": {"type": "string", "pattern": r"^[0-9]{0,20}$"},
        "content": STR,
        "status": {"enum": ["received", "sent", "failed", "uncertain", "cancelled"]},
        "message_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "reply_to": {"type": "string", "maxLength": 200},
        "part": {"type": "integer", "minimum": 0},
        "sequence": {"type": "integer", "minimum": 0},
        "occurred_at": {"type": ["string", "null"], "format": "date-time"},
        "source": SOURCE, "locked": {"type": "boolean"},
    }),
    "emotion_events": document({
        "id": ID, "account_uid": UID, "content": STR,
        "proposal": EMOTION_CHANGE, "before": EMOTION_STATE, "after": EMOTION_STATE,
        "source": SOURCE, "locked": {"type": "boolean"},
    }),
    "conversation_summaries": document({
        "id": ID, "account_uid": UID, "thread_id": ID, "content": STR,
        "turn_refs": {"type": "array", "items": TURN_REF, "minItems": 1, "maxItems": 50},
        "turn_spans": {"type": "array", "minItems": 1, "maxItems": 50, "items": obj({
            **TURN_REF["properties"], "start": {"type": "integer", "minimum": 0},
            "end": {"type": "integer", "minimum": 0}, "total": {"type": "integer", "minimum": 0},
        })},
        "previous": {"anyOf": [{"type": "null"}, TURN_REF]},
        "source": SOURCE, "locked": {"type": "boolean"},
    }, ["id", "account_uid", "thread_id", "content", "turn_refs", "previous", "source", "locked"]),
    "semantic_groups": document({
        "id": ID, "content": STR, "title": {"type": "string", "maxLength": 200},
        "aliases": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 20},
        "members": {"type": "array", "items": MEMORY_REF, "minItems": 1, "maxItems": 200},
        "parent_refs": {"type": "array", "items": TURN_REF, "maxItems": 2},
        "source": SOURCE, "locked": {"type": "boolean"},
    }),
    "browse_observations": document({
        "id": ID, "account_uid": UID, "bvid": {"type": "string", "pattern": "^BV[0-9A-Za-z]{10}$"},
        "owner_uid": {"type": "string", "pattern": "^[0-9]{0,20}$"},
        "title": {"type": "string", "maxLength": 500}, "origin": {"enum": ["following", "recommended", "search", "guide"]},
        "topics": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 8},
        "candidate_count": {"type": "integer", "minimum": 0},
        "selected_count": {"type": "integer", "minimum": 0},
        "read_count": {"type": "integer", "minimum": 0},
        "read_at": {"type": ["string", "null"], "format": "date-time"},
        "day": {"type": "string", "format": "date"},
        "source": SOURCE, "locked": {"type": "boolean"},
    }),
    "source_snapshots": document({
        "id": ID, "account_uid": UID, "bvid": {"type": "string", "pattern": "^(?:BV[0-9A-Za-z]{10})?$"},
        "url": STR, "title": STR, "owner_uid": {"type": "string", "pattern": "^[0-9]{0,20}$"},
        "coverage": COVERAGE,
        "segments": {"type": "array", "maxItems": 10000, "items": obj({
            "start": {"type": "number", "minimum": 0}, "end": {"type": "number", "minimum": 0},
            "text": STR,
        })},
        "visual_note": STR,
        "source": SOURCE, "locked": {"type": "boolean"},
    }),
    "knowledge_reviews": document({
        "id": ID, "account_uid": UID, "target": TURN_REF,
        "related": {"type": "array", "items": TURN_REF, "maxItems": 3},
        "sources": {"type": "array", "items": TURN_REF, "maxItems": 4},
        "method_trials": {"type": "array", "items": TURN_REF, "maxItems": 5},
        "result": REVIEW_RESULT,
        "source": SOURCE, "locked": {"type": "boolean"},
    }),
    "method_trials": document({
        "id": ID, "account_uid": UID, "skill": TURN_REF,
        "mode": {"enum": ["simulation", "user_report"]},
        "case": STR, "expected": STR, "observed": STR,
        "outcome": {"enum": ["passed", "failed", "inconclusive"]},
        "limitations": STR,
        "evidence": TURN_REF, "evidence_quote": STR,
        "source": SOURCE, "locked": {"type": "boolean"},
    }),
    "learning_goals": document({
        "id": ID, "account_uid": UID, "request": TURN_REF, "request_quote": STR,
        "objective": {"type": "string", "minLength": 1, "maxLength": 3000},
        "criteria": GOAL_CRITERIA,
        "status": {"enum": ["active", "paused", "completed", "cancelled"]},
        "phase": {"enum": ["plan", "search", "read", "quiz", "answer", "grade", "done"]},
        "reason": STR, "gaps": {"type": "array", "items": STR, "maxItems": 8},
        "round": {"type": "integer", "minimum": 1, "maximum": 21},
        "round_limit": {"type": "integer", "minimum": 1, "maximum": 20},
        "knowledge_refs": {"type": "array", "items": TURN_REF, "maxItems": 20},
        "visited_bvids": {"type": "array", "items": STR, "maxItems": 40},
        "queries": {"type": "array", "items": STR, "maxItems": 40},
        "query": {"type": "string", "maxLength": 100},
        "selected_bvid": {"type": "string", "pattern": "^(?:BV[0-9A-Za-z]{10})?$"},
        "check_ids": {"type": "array", "items": ID, "maxItems": 20},
        "updated_at": {"type": "string", "format": "date-time"},
        "source": SOURCE, "locked": {"type": "boolean"},
    }),
    "learning_checks": document({
        "id": ID, "account_uid": UID, "goal_id": ID, "definition": VERSION,
        "round": {"type": "integer", "minimum": 1, "maximum": 20},
        "knowledge_refs": {"type": "array", "items": TURN_REF, "minItems": 1, "maxItems": 20},
        "source_refs": {"type": "array", "items": TURN_REF, "minItems": 1, "maxItems": 20},
        "questions": {"type": "array", "items": QUESTION, "minItems": 2, "maxItems": 8},
        "answers": {"type": "array", "items": ANSWER, "maxItems": 8},
        "grades": {"type": "array", "items": GRADE, "maxItems": 8},
        "status": {"enum": ["questions_ready", "answered", "graded"]},
        "passed": {"type": ["boolean", "null"]},
        "limitations": STR, "source": SOURCE, "locked": {"type": "boolean"},
    }),
    "comment_pages": document({
        "id": ID, "account_uid": UID, "oid": UID, "root": UID,
        "crawl_id": ID,
        "page": {"type": "integer", "minimum": 1, "maximum": 10000},
        "total": {"type": ["integer", "null"], "minimum": 0},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 20},
        "root_comment": {"anyOf": [{"type": "null"}, COMMENT]},
        "replies": {"type": "array", "maxItems": 20, "items": COMMENT},
        "source": SOURCE, "locked": {"type": "boolean"},
    }),
}
# Optional addition preserves existing, user-editable conversation files.
SCHEMAS["conversation_turns"]["properties"]["attachments"] = ATTACHMENTS
SCHEMAS["comment_pages"]["properties"]["resource_type"] = {"enum": [1, 11, 17]}
SCHEMAS["knowledge"]["properties"]["source_ref"] = TURN_REF
SCHEMAS["knowledge"]["properties"]["source_quotes"] = {
    "type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 500}}
SCHEMAS["source_snapshots"]["properties"]["capture"] = obj({
    "method": {"const": "codex_native_web"}, "tool_reference": {"type": "string", "maxLength": 1000},
    "scope": {"const": "excerpt"},
})
SCHEMAS["source_snapshots"]["properties"]["visual_samples"] = {
    "type": "array", "maxItems": 4, "items": obj({"kind": {"type": "string", "maxLength": 100},
        "timestamps_seconds": {"type": "array", "maxItems": 100, "items": {"type": "number", "minimum": 0}}})}
SCHEMAS["companion"]["properties"]["spaced_reviews_enabled"] = {"type": "boolean"}
SCHEMAS["companion"]["properties"]["algorithm_routing"] = ALGORITHM_SCHEMA
SCHEMAS["companion"]["properties"].update(CONTEXT_BUDGET_PROPERTIES)
SCHEMAS["persona"]["properties"].update({
    "relationship_baseline": obj({
        "owner_uid": UID, "owner_name": {"type": "string", "minLength": 1, "maxLength": 100},
        "addresses": {"type": "array", "minItems": 1, "maxItems": 10,
                      "items": {"type": "string", "minLength": 1, "maxLength": 100}},
        "level": {"type": "string", "minLength": 1, "maxLength": 100},
        "description": {"type": "string", "maxLength": 2000}, "source": SOURCE,
    }),
    "foundation_memories": {"type": "array", "maxItems": 10, "uniqueItems": True, "items": ID},
    "visual_identity": obj({
        "description": {"type": "string", "minLength": 1, "maxLength": 4000},
        "references": {"type": "array", "minItems": 1, "maxItems": 2, "items": obj({
            "filename": {"type": "string", "pattern": r"^[A-Za-z0-9_-]+\.(?:png|jpg|webp)$"},
            "sha256": VERSION, "label": {"type": "string", "maxLength": 200},
        })},
    }),
})
SCHEMAS["companion"]["properties"].update({
    "inbox_poll_seconds": {"type": "integer", "minimum": 15, "maximum": 3600},
    "inbox_timeout_seconds": {"type": "integer", "minimum": 20, "maximum": 600},
    "native_tools_enabled": {"type": "boolean"},
    "native_web_daily_limit": {"type": "integer", "minimum": 0, "maximum": 20},
    "native_image_daily_limit": {"type": "integer", "minimum": 0, "maximum": 10},
    "video_gap_enabled": {"type": "boolean"},
    "video_gap_daily_limit": {"type": "integer", "minimum": 0, "maximum": 30},
    "video_gap_max_windows": {"type": "integer", "minimum": 1, "maximum": 30},
})
SCHEMAS["native_jobs"] = document({
    "id": ID, "account_uid": UID, "owner": ID, "kind": {"enum": ["web", "image"]},
    "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
    "draft": {"type": "string", "maxLength": 1000},
    "goal": {"anyOf": [{"type": "null"}, TURN_REF]},
    "status": {"enum": ["pending", "running", "completed", "failed", "cancelled"]},
    "config_versions": obj({k: VERSION for k in CONFIG}),
    "generation": {"type": "string", "maxLength": 80}, "attempts": {"type": "integer", "minimum": 0},
    "result": {"type": "object"}, "error": {"type": "string", "maxLength": 200},
    "source": SOURCE, "locked": {"type": "boolean"},
})
SCHEMAS["native_jobs"]["properties"]["use_persona"] = {"type": "boolean"}
SCHEMAS["native_jobs"]["properties"]["identity_dependencies"] = {
    "type": "array", "maxItems": 100, "items": {"type": "object"},
}
VIDEO_WINDOW = obj({
    "start_seconds": {"type": "number", "minimum": 0},
    "end_seconds": {"type": "number", "minimum": 0},
    "mode": {"enum": ["text", "visual"]}, "reason": {"type": "string", "maxLength": 1000},
})
SCHEMAS["video_progress"] = document({
    "id": ID, "account_uid": UID, "owner": ID,
    "bvid": {"type": "string", "pattern": "^BV[A-Za-z0-9]{10}$"},
    "page_index": {"type": "integer", "minimum": 0}, "cid": {"type": "string", "maxLength": 30},
    "duration_seconds": {"type": "number", "minimum": 0}, "title": STR, "owner_uid": {"type": "string"},
    "windows": {"type": "array", "maxItems": 100, "items": obj({
        "source": TURN_REF, "start_seconds": {"type": "number", "minimum": 0},
        "end_seconds": {"type": "number", "minimum": 0}, "mode": {"enum": ["text", "visual"]},
    })},
    "requests": {"type": "array", "maxItems": 60, "items": obj({
        **VIDEO_WINDOW["properties"], "id": ID,
        "goal": {"anyOf": [{"type": "null"}, TURN_REF]},
        "status": {"enum": ["pending", "reading", "completed", "blocked", "cancelled"]},
        "error": {"type": "string", "maxLength": 200},
    })},
    "windows_used": {"type": "integer", "minimum": 0},
    "source": SOURCE, "locked": {"type": "boolean"},
})

SCHEMAS["publication_images"] = document({
    "id": ID, "account_uid": UID, "sha256": VERSION, "filename": {"type": "string", "maxLength": 100},
    "width": {"type": "integer", "minimum": 1, "maximum": 10000},
    "height": {"type": "integer", "minimum": 1, "maximum": 10000},
    "origin": {"enum": ["codex_imagegen", "user_provided"]},
    "description": {"type": "string", "maxLength": 2000}, "source": SOURCE, "locked": {"type": "boolean"},
})

ASSESSMENT_REF = obj({"kind": {"enum": ["learning_checks", "learning_reviews"]}, "id": ID, "version": VERSION})
SCHEMAS["learning_nodes"] = document({
    "id": ID, "account_uid": UID, "goal_id": ID, "definition": VERSION,
    "criterion": {"type": "integer", "minimum": 0, "maximum": 5}, "title": STR,
    "parent_id": {"type": ["string", "null"], "maxLength": 80},
    "prerequisites": {"type": "array", "items": ID, "uniqueItems": True, "maxItems": 5},
    "mastery": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
    "streak": {"type": "integer", "minimum": 0, "maximum": 1000},
    "next_review_at": {"type": ["string", "null"], "format": "date-time"},
    "assessment": {"anyOf": [{"type": "null"}, ASSESSMENT_REF]},
    "mistakes": {"type": "array", "maxItems": 30, "items": obj({
        "assessment": ASSESSMENT_REF, "question_id": ID, "question": STR,
        "answer": STR, "gap": STR, "resolved": {"type": "boolean"},
    })},
    "source": SOURCE, "locked": {"type": "boolean"},
})
SCHEMAS["learning_reviews"] = document({
    "id": ID, "account_uid": UID, "goal_id": ID, "base_check": TURN_REF,
    "node_refs": {"type": "array", "items": TURN_REF, "maxItems": 6},
    "answers": {"type": "array", "items": ANSWER, "maxItems": 8},
    "grades": {"type": "array", "items": GRADE, "maxItems": 8},
    "status": {"enum": ["questions_ready", "answered", "graded"]},
    "limitations": STR, "source": SOURCE, "locked": {"type": "boolean"},
})


SCHEMAS["policy"]["properties"].update(SERVICE_POLICY)



SCHEMAS["memories"]["properties"]["fact"] = PERSONAL_FACT


def validate(kind, data):
    schema = SCHEMAS.get(kind)
    if schema is None:
        raise AssistantError("schema_unknown", kind)
    errors = sorted(
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(data),
        key=lambda e: str(e.path),
    )
    if errors:
        error = errors[0]
        # Avoid echoing possible secrets entered in a wrong field.
        raise AssistantError(
            "invalid_data", f"{kind}: {'.'.join(map(str, error.path)) or '/'} 不符合 {error.validator} 约束"
        )
    if kind == "policy":
        try:
            ZoneInfo(data["timezone"])
        except (ValueError, ZoneInfoNotFoundError):
            raise AssistantError("invalid_timezone", "无法识别 policy.timezone") from None
        if data["active_hours"]["start"] == data["active_hours"]["end"]:
            raise AssistantError("invalid_hours", "开始和结束时刻不能相同")
    if kind in ("knowledge", "source_snapshots"):
        coverage = data["coverage"]
        if coverage["end_seconds"] < coverage["start_seconds"]:
            raise AssistantError("invalid_coverage", "内容覆盖结束时间早于开始时间")
        if not coverage["bvid"] and coverage["kind"] != "web":
            raise AssistantError("invalid_coverage", "视频材料必须携带 BV 号")
        if coverage["kind"] == "web" and (coverage["bvid"] or coverage["complete"]):
            raise AssistantError("invalid_coverage", "网页导入只能声明实际摘录，不声明整页完整覆盖")
        if kind == "knowledge" and coverage["kind"] == "metadata_only":
            raise AssistantError("insufficient_content", "只有标题简介不能写成已学习的知识")
    if kind == "companion" and data["reflection_min_memories"] > data["context_record_limit"]:
        raise AssistantError("invalid_reflection_limit", "反思所需条目数不能超过每轮上下文条目上限")
    if kind == "companion" and data.get("conversation_keep_recent", 12) >= data.get("conversation_summary_after", 40):
        raise AssistantError("invalid_summary_limit", "压缩触发条数必须大于保留近期原文条数")
    return deepcopy(data)
