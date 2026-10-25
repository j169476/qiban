import json

import pytest

from tests_core.test_answer_pipeline import CASES, ScriptedReader, evaluation


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["actual", "oracle"])
async def test_full_answer_pipeline_uses_encrypted_postgres_and_live_acl_checks(pg_store, branch):
    from companion_core.observability import measure

    class MeteredReader(ScriptedReader):
        async def generate(self, *args, **kwargs):
            with measure(pg_store, "synthetic-model", kind="model") as observation:
                observation.tokens({"input_tokens": 10, "output_tokens": 5, "usage_events": 1})
                return await super().generate(*args, **kwargs)

    case = next(case for case in CASES if case["id"] == "current-correction")
    row = await evaluation.run_case(pg_store, MeteredReader(), case, branch)
    assert "error" not in row and row["answer_pass"] and row["fact_state_pass"], row
    with pg_store.db() as db:
        bodies = db.execute("SELECT body FROM content_records WHERE kind='memories'").fetchall()
    assert bodies and all("杭州".encode() not in bytes(row[0]) for row in bodies)
    assert row["tokens"]["input"] == row["model_calls"] * 10
    json.dumps(evaluation.summarize([row], real_model=False))
