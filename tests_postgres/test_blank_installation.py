"""A fresh production-type store does not inherit the author's persona."""


def test_fresh_postgres_has_no_personal_baseline_or_memories(pg_store):
    persona = pg_store.read("persona")["data"]
    assert persona["name"] == ""
    assert persona["core"] == {"description": "", "speaking_style": []}
    assert persona["interests"] == []
    assert persona["foundation_memories"] == []
    assert "visual_identity" not in persona and "relationship_baseline" not in persona
    assert pg_store.records("memories") == ([], [])
    assert pg_store.records("relationships") == ([], [])
