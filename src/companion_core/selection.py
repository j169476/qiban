"""Explainable candidate ranking. Ambiguous content still goes to the model."""
import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from .algorithm_policy import policy


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def contains(text, phrase):
    text, phrase = normalize(text), normalize(phrase)
    if not phrase:
        return False
    if re.fullmatch(r"[a-z0-9_ ]+", phrase):
        return re.search(r"(?<![a-z0-9_])" + re.escape(phrase) + r"(?![a-z0-9_])", text) is not None
    return phrase in text


@dataclass(frozen=True)
class Candidate:
    id: str
    text: str
    author: str = ""
    topics: tuple = ()


def interests(persona=(), growth=None, feedback=()):
    weights = {normalize(topic): 1.0 for topic in persona if topic.strip()}
    for topic, weight in (growth or {}).items():
        if weight > 0:
            weights[normalize(topic)] = max(weights.get(normalize(topic), 0), min(1.0, weight))
    negative = set()
    for item in feedback:
        topic = normalize(item["topic"])
        if item["latest_feedback"] == "not_interested":
            negative.add(topic)
            weights.pop(topic, None)
        elif item["latest_feedback"] == "interested":
            weights[topic] = max(weights.get(topic, 0), min(1.0, 0.4 + 0.2 * item.get("supporting_sources", 0)))
    return weights, negative


def choose(candidates, *, settings, weights=None, negative=(), history=(), explore=False, seed="", query=""):
    """Return a route, ranked evidence and an optional selection, without model or I/O.

    History contains actual read items, not impressions or clicks. Labels inferred
    from titles are selection hints only, never evidence that content was read.
    """
    config = policy(settings, "selection")
    base = {"route": "model", "reason": "disabled", "selected": "", "ranked": [],
            "eligible_ids": [c.id for c in candidates], "exploration": explore}
    if not config["enabled"]:
        return base
    weights = {normalize(k): v for k, v in (weights or {}).items()}
    # An explicit learning request supersedes ordinary interest feedback. Platform
    # exclusions and the goal's own visited set remain the caller's hard filters.
    negatives = {normalize(word) for word in negative if word.strip()} if not query.strip() else set()
    # A new source with the same ID is still one read, not an extra preference vote.
    distinct = {}
    for item in history:
        distinct.pop(item.id, None)
        distinct[item.id] = item
    recent = list(distinct.values())[-config["history_limit"]:]
    seen = {r.id for r in recent}
    authors, topics = Counter(), Counter()
    classified = 0
    for item in recent:
        if item.author:
            authors[item.author] += 1
        labels = {normalize(t) for t in item.topics if t.strip()}
        if labels:
            classified += 1
            for topic in labels:
                topics[topic] += 1 / len(labels)
    topic_share = {k: v / classified for k, v in topics.items()} if classified else {}
    author_share = {k: v / len(recent) for k, v in authors.items()} if recent else {}
    vocabulary = set(weights) | set(topics)
    query = normalize(query)
    # Restrict deterministic goal selection to explicit short search phrases.
    goal_terms = query.split() if query and len(query) <= 80 else []
    rows = []
    for candidate in {c.id: c for c in candidates}.values():
        text = candidate.text + " " + " ".join(candidate.topics)
        if (not query and candidate.id in seen) or any(contains(text, term) for term in negatives):
            continue
        matched = sorted(t for t in weights if contains(text, t))
        labels = {normalize(t) for t in candidate.topics if t.strip()}
        labels |= {t for t in vocabulary if contains(text, t)}
        goal_match = bool(goal_terms) and all(contains(text, term) for term in goal_terms)
        affinity = sum(weights[t] for t in matched)
        if query:
            affinity = float(goal_match)
        concentration = max((topic_share.get(t, 0) for t in labels), default=0)
        author = author_share.get(candidate.author, 0)
        novelty = 1 - concentration if labels else 0
        score = (config["interest_weight"] * (min(affinity, 2) if not explore else min(affinity, 1) * .2)
                 + config["novelty_weight"] * novelty * (3 if explore else 1)
                 - config["topic_penalty"] * concentration - config["author_penalty"] * author)
        rows.append({"id": candidate.id, "score": round(score, 6), "matched": matched, "topics": sorted(labels),
                     "goal_match": goal_match, "topic_share": concentration, "author_share": author,
                     "tie": hashlib.sha256(f"{seed}:{candidate.id}".encode()).hexdigest()})
    rows.sort(key=lambda r: (r["score"], r["tie"]), reverse=True)
    base.update(ranked=[{k: v for k, v in r.items() if k != "tie"} for r in rows],
                eligible_ids=[r["id"] for r in rows])
    if not rows:
        return {**base, "route": "skip", "reason": "no_eligible_candidates"}
    if query:
        # Ambiguous goal text is not simplified to the character bigrams used by recall.
        confident = [r for r in rows if r["goal_match"]]
    elif explore:
        # Unknown titles need semantic interpretation. Known under-covered directions
        # can be selected locally, including topics outside the current interest set.
        confident = [r for r in rows if r["topics"] and r["topic_share"] < settings.get("topic_max_share", .6)]
    else:
        confident = [r for r in rows if r["matched"]]
    if not confident:
        return {**base, "reason": "semantic_uncertainty"}
    best = confident[0]
    if not explore:
        others = [r for r in rows if r["id"] != best["id"]]
        if others and best["score"] - others[0]["score"] < config["min_margin"]:
            return {**base, "reason": "ambiguous_ranking"}
    return {**base, "route": "algorithm", "reason": "diversity_exploration" if explore else "clear_match",
            "selected": best["id"]}
