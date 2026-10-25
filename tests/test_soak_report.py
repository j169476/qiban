from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from scripts.soak_observer import summarize


def observations():
    start = datetime(2026, 10, 21, tzinfo=timezone.utc)
    run = {
        "generation": "test", "started_at": start.isoformat(),
        "deadline_at": (start + timedelta(days=1)).isoformat(), "read_only": True, "status": "active",
    }
    meta = {"run": run, "duration_seconds": 86400, "policy_version": "original"}
    samples = [{
        "at": (start + timedelta(minutes=minute)).isoformat(), "run": deepcopy(run),
        "ages_seconds": [5, 5], "actions": [], "model_counts": {"day": int(minute > 60)},
        "video_counts": [], "read_counts": {"material": int(minute > 60)}, "read_record_issues": 0,
        "policy_version": "original", "fault": None,
    } for minute in range(1441)]
    samples[-1]["run"].update(status="paused", reason="duration_complete")
    return meta, samples


def test_soak_report_requires_elapsed_day_real_read_and_stable_policy():
    meta, samples = observations()
    assert summarize(meta, samples, ended=True)["status"] == "passed"
    assert summarize(meta, samples[:100])["status"] == "running"
    assert summarize(meta, samples[:100], ended=True)["status"] == "interrupted_or_needs_review"


@pytest.mark.parametrize("failure", ["missing_samples", "no_reads", "policy_changed", "action", "stale_pulse"])
def test_soak_report_never_passes_incomplete_or_modified_acceptance(failure):
    meta, samples = observations()
    if failure == "missing_samples":
        del samples[500:550]
    elif failure == "no_reads":
        for row in samples:
            row["read_counts"] = {}
    elif failure == "policy_changed":
        samples[500]["policy_version"] = "edited"
    elif failure == "action":
        samples[500]["actions"] = [{"kind": "like", "status": "failed", "count": 1}]
    else:
        samples[500]["ages_seconds"] = [100, 3]
    assert summarize(meta, samples, ended=True)["status"] == "interrupted_or_needs_review"
