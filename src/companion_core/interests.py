"""Evidence-based interest aggregation with correction and source deduplication."""

def build_profile(signals, guide):
    topics = {}
    for item in signals:
        doc = item["data"]
        for observation in doc["topics"]:
            name = observation["topic"].strip().casefold()
            if not name:
                continue
            topic = topics.setdefault(name, {
                "topic": observation["topic"], "latest_feedback": observation["sentiment"],
                "latest_reason": doc["content"][:500], "sources": {}, "evidence_ids": [],
            })
            # Repeated mentions/discussion of one video are one sample, not independent votes.
            if doc["content_id"]:
                topic["sources"].setdefault(doc["content_id"], observation["sentiment"])
            if len(topic["evidence_ids"]) < 6:
                topic["evidence_ids"].append(doc["id"])
    profile = []
    for topic in list(topics.values())[:50]:
        supporting = sum(value == "interested" for value in topic["sources"].values())
        topic["supporting_sources"] = supporting
        topic["distinct_sources"] = len(topic.pop("sources"))
        topic["status"] = (
            "暂时避开，等待新的明确反馈" if topic["latest_feedback"] == "not_interested"
            else "多条独立内容支持" if supporting >= 3
            else "初步线索，不能当作固定偏好"
        )
        profile.append(topic)
    return {
        "guide_uid": guide, "topics": profile,
        "signal_count": len(signals),
        "note": "大号推荐是引导线索；最近的纠正优先。同一内容的重复讨论只算一个样本。",
    }
