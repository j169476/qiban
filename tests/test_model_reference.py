"""Verify published medians against the eight measured completion times in milliseconds."""
import json
import re
from pathlib import Path
from statistics import median


def test_support_table_uses_eight_completed_hybrid_replies_per_model():
    tests = Path(__file__).resolve().parent
    samples = json.loads((tests / "fixtures/model_reply_times.json").read_text(encoding="utf-8"))
    table = (tests.parent / "docs/模型选择参考.md").read_text(encoding="utf-8")
    entries = re.findall(r"^\| (GPT-[\w.-]+) \| ([^|]+?) \|", table, re.MULTILINE)
    assert all(len(times) == 8 and all(ms > 0 for ms in times) for times in samples.values())
    assert len(entries) == len(samples)
    assert {model.lower(): value for model, value in entries} == {
        model: f"{median(times) / 1000:.2f} 秒" for model, times in samples.items()
    }
