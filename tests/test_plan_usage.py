from companion_codex.plan_usage import PlanUsage, normalize
from companion_core.errors import AssistantError


def test_multi_bucket_unknown_and_overage_are_not_zero_or_double_counted():
    data = {"rateLimits": {"primary": {"usedPercent": 99}}, "rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 24.5, "windowDurationMins": 300, "resetsAt": 2000},
                  "secondary": {"usedPercent": 110, "windowDurationMins": 10080}},
        "other": {"primary": {"usedPercent": None}}}}
    result = normalize(data)
    assert len(result) == 2
    assert result[0]["windows"][0]["remaining_percent"] == 75.5
    assert result[0]["windows"][1]["remaining_percent"] == 0
    assert result[1]["windows"][0]["remaining_percent"] is None
    assert normalize({"rateLimits": {"primary": {"usedPercent": True}}})[0]["windows"][0]["used_percent"] is None


def test_plan_cache_read_only_failure_keeps_timestamp_and_marks_unavailable():
    class Client:
        calls = 0

        def request(self, method, args):
            assert method == "account/rateLimits/read" and args == {}
            self.calls += 1
            if self.calls > 1:
                raise AssistantError("host_unavailable", "private error must not leak")
            return {"rateLimits": {"primary": {"usedPercent": 30}}}

    now = [100.0]
    client = Client()
    usage = PlanUsage(client, clock=lambda: now[0])
    assert usage.snapshot()["updated_at"] is None
    usage.refresh()
    usage.refresh()
    assert client.calls == 1
    assert usage.snapshot()["status"] == "available"
    now[0] = 225
    usage.refresh()
    snapshot = usage.snapshot()
    assert snapshot["status"] == "unavailable" and snapshot["stale"]
    assert snapshot["updated_at"] == 100
    assert "private error" not in str(snapshot)
    snapshot["buckets"].clear()
    assert usage.snapshot()["buckets"]
