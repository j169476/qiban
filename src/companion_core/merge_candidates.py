"""Bounded lexical neighbors and a resumable, low-frequency global audit lane."""
import hashlib
import math
from collections import defaultdict
from itertools import combinations

from .algorithm_policy import policy
from .conversations import identifier
from .selection import normalize
from .storage import search_terms


def pair_key(left, right):
    return "semantic-pair:" + identifier(
        "checked", sorted((g["data"]["id"], g["version"]) for g in (left, right)))


def neighbors(groups, count):
    """Candidate generation only. Shared words never prove semantic equivalence."""
    postings, fingerprints = defaultdict(list), defaultdict(list)
    texts = [normalize(" ".join([g["data"].get("title", ""), g["data"]["content"],
                                 *g["data"].get("aliases", [])])) for g in groups]
    terms = [{t for t in search_terms(text) if len(t) >= 2} for text in texts]
    for i, (text, words) in enumerate(zip(texts, terms, strict=True)):
        if text:
            fingerprints[hashlib.sha256(text.encode()).hexdigest()].append(i)
        for term in words:
            postings[term].append(i)
    scores = {}
    for i, words in enumerate(terms):
        nearby = defaultdict(float)
        for term in words:
            peers = postings[term]
            if not 2 <= len(peers) <= max(4, len(groups) // 4):
                continue  # Common headings are not evidence of a useful comparison.
            # A bounded rotating window avoids an unbounded common-token pair product.
            start = i % len(peers)
            for j in (peers[start:] + peers[:start])[:64]:
                if j != i:
                    nearby[j] += math.log1p(len(groups) / len(peers)) / math.sqrt(max(1, len(words) * len(terms[j])))
        for j, score in sorted(nearby.items(), key=lambda p: (-p[1], p[0]))[:count]:
            pair = tuple(sorted((i, j)))
            scores[pair] = max(scores.get(pair, 0), score)
    # Exact repeated views can have only common vocabulary. A chain of neighbors
    # remains bounded even if thousands of groups contain the same text.
    for indices in fingerprints.values():
        for offset, i in enumerate(indices):
            for j in indices[offset + 1:offset + 1 + count]:
                scores[i, j] = 100.0
    return [pair for pair, _ in sorted(scores.items(), key=lambda p: (-p[1], p[0]))]


def pair_at(rank, size):
    """Unrank lexicographic unordered pairs without materializing the full product."""
    low, high = 0, size - 1
    while low + 1 < high:
        middle = (low + high) // 2
        if middle * (2 * size - middle - 1) // 2 <= rank:
            low = middle
        else:
            high = middle
    prefix = low * (2 * size - low - 1) // 2
    return low, low + 1 + rank - prefix


def plan(groups, checked, settings, state, now):
    """Unknown non-neighbors stay unchecked; the audit cursor eventually revisits them."""
    config = policy(settings, "merge")
    groups = sorted(groups, key=lambda g: g["data"]["id"])
    total = len(groups) * (len(groups) - 1) // 2
    filtered = config["enabled"] and len(groups) > config["exhaustive_below"]
    if not filtered:
        return {"filtered": False, "pairs": combinations(groups, 2), "audit_keys": set(),
                "audit_cursor": 0, "total_pairs": total, "candidate_pairs": total}
    pairs = [(groups[i], groups[j]) for i, j in neighbors(groups, config["neighbors_per_group"])]
    pending = [pair for pair in pairs if pair_key(*pair) not in checked]
    related_keys = {pair_key(*pair) for pair in pairs}
    stamp = state.get("audit_at")
    due = not isinstance(stamp, (int, float)) or now - stamp >= config["global_audit_hours"] * 3600
    cursor = int(state.get("audit_cursor", 0)) % total if total else 0
    audits = []
    scanned = 0
    if due:
        # The cursor advances across checked/related pairs too. If an entire bounded
        # scan finds none, the next invocation can continue without a model call.
        while scanned < min(total, max(100, config["audit_pairs"] * 50)) and len(audits) < config["audit_pairs"]:
            i, j = pair_at(cursor, len(groups))
            cursor = (cursor + 1) % total
            scanned += 1
            pair = groups[i], groups[j]
            key = pair_key(*pair)
            if key not in checked and key not in related_keys:
                audits.append(pair)
    return {"filtered": True, "pairs": [*audits, *pending],
            "audit_keys": {pair_key(*pair) for pair in audits}, "audit_cursor": cursor,
            "audit_at": stamp, "audit_scanned": scanned, "total_pairs": total,
            "candidate_pairs": len(pairs), "pending_candidates": len(pending)}
