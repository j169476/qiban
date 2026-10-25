"""Rebuildable source spans and explicit excerpts; editable YAML stays canonical."""
import hashlib
import json
import re
import sqlite3
from copy import deepcopy

from .index_cache import session

PASSAGE_CHARS = 1200
OVERLAP_CHARS = 160
LONG_RECORD_CHARS = 1600
REVISION = "paragraph-spans-v1-1200-160"


def split(content):
    """Unicode character offsets, with overlap to retain local conditions/negation."""
    spans, start = [], 0
    while start < len(content):
        end = min(len(content), start + PASSAGE_CHARS)
        if end < len(content):
            lower = start + PASSAGE_CHARS // 2
            endings = [match.end() for match in re.finditer(r"\n|[。！？]|[.!?;](?:\s|$)", content[lower:end])]
            if endings:
                end = lower + endings[-1]
            elif content[end - 1:end].isascii() and content[end:end + 1].isalnum():
                boundary = max(content.rfind(" ", lower, end), content.rfind("\t", lower, end))
                if boundary >= lower:
                    end = boundary + 1
        spans.append([start, end])
        if end == len(content):
            break
        start = max(start + 1, end - OVERLAP_CHARS)
    return spans


def valid(spans, total):
    if not isinstance(spans, list) or not spans or len(spans) > 100:
        return False
    previous = None
    for span in spans:
        if (not isinstance(span, list) or len(span) != 2
                or any(type(value) is not int for value in span)
                or not 0 <= span[0] < span[1] <= total
                or span[1] - span[0] > PASSAGE_CHARS):
            return False
        if previous is None:
            if span[0] != 0:
                return False
        elif not previous[0] < span[0] < previous[1] < span[1]:
            return False
        previous = span
    return spans[-1][1] == total


def source_spans(store, record):
    content = record["data"].get("content", "")
    if len(content) <= LONG_RECORD_CHARS:
        return []
    kind, item_id, version = record["kind"], record["data"]["id"], record["version"]
    with session(store) as db:
        if db is not None:
            try:
                row = db.execute(
                    "SELECT spans,spans_hash FROM passages WHERE kind=? AND id=? AND version=? AND revision=?",
                    (kind, item_id, version, REVISION),
                ).fetchone()
                if row and hashlib.sha256(row[0].encode()).hexdigest() == row[1]:
                    spans = json.loads(row[0])
                    if valid(spans, len(content)):
                        store.index_stats["passage_hits"] = store.index_stats.get("passage_hits", 0) + 1
                        return spans
            except (sqlite3.Error, TypeError, ValueError):
                store.index_stats["cache_errors"] += 1
        spans = split(content)
        store.index_stats["passage_builds"] = store.index_stats.get("passage_builds", 0) + 1
        if db is not None:
            try:
                raw = json.dumps(spans)
                db.execute("INSERT OR REPLACE INTO passages VALUES (?,?,?,?,?,?)",
                           (kind, item_id, version, REVISION, raw, hashlib.sha256(raw.encode()).hexdigest()))
            except sqlite3.Error:
                store.index_stats["cache_errors"] += 1
        return spans


def index(store, records):
    return {(r["kind"], r["data"]["id"]): spans for r in records if (spans := source_spans(store, r))}


def terms(query, queries=()):
    from .storage import search_terms

    return sorted({term for expression in (query, *queries[:5]) for term in search_terms(expression)})


def matches(body, term):
    return bool(re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", body)) \
        if re.fullmatch(r"[a-z0-9_]+", term) else term in body


def ordered_spans(record, spans, query_terms):
    content = record["data"].get("content", "")
    if not spans:
        return [[0, len(content)]] if content else []
    scored = [(sum(matches(content[start:end].casefold(), term) for term in query_terms), start, end)
              for start, end in spans]
    if not query_terms or not any(score for score, _, _ in scored):
        return spans[:1]
    return [[start, end] for score, start, end in sorted(scored, key=lambda item: (-item[0], item[1])) if score]


def clipped_range(content, span, allowance, query_terms):
    start, end = span
    if end - start <= allowance:
        return [start, end]
    original = content[start:end]
    body = original.casefold()
    anchors = [(len(term), body.find(term)) for term in query_terms if matches(body, term)]
    anchor = max(anchors, key=lambda item: (item[0], -item[1]))[1] if anchors else 0
    if len(body) != len(original):
        # Case folding can expand characters (e.g. ß -> ss). Stored coordinates
        # always refer to the original string, never the normalized search text.
        positions = [index for index, character in enumerate(original) for _ in character.casefold()]
        anchor = positions[anchor]
    left = max(start, min(start + anchor - allowance // 3, end - allowance))
    return [left, left + allowance]


def metadata(record, ranges):
    total = len(record["data"].get("content", ""))
    included = sum(end - start for start, end in ranges)
    return {"kind": record["kind"], "id": record["data"]["id"], "version": record["version"],
            "field": "content", "offset_unit": "unicode_characters", "ranges": ranges,
            "total_chars": total, "included_chars": included, "omitted_chars": total - included,
            "complete": included == total}


def excerpt(record, spans, query_terms, budget):
    """A schema-compatible record view, never a replacement for a canonical read."""
    content = record["data"].get("content")
    if not isinstance(content, str) or not content:
        return None
    candidates = []
    for span in ordered_spans(record, spans, query_terms):
        if any(span[0] < end and start < span[1] for start, end in candidates):
            continue
        candidates.append(span)
        if len(candidates) == 3:
            break
    empty = deepcopy(record)
    empty["data"]["content"] = ""
    empty["content_truncated"] = True
    empty["excerpt"] = metadata(record, [[0, 0] for _ in candidates])
    overhead = len(json.dumps(empty, ensure_ascii=False)) + 40 * max(0, len(candidates) - 1)
    share = max(1, (budget - overhead) // max(1, len(candidates)))
    selected, result = [], None
    for span in candidates:
        # Reserve room for other matched intervals, e.g. an earlier statement
        # and its later correction. The first interval cannot consume all space.
        low, high, chosen = 1, min(share, span[1] - span[0]), None
        while low <= high:
            allowance = (low + high) // 2
            ranges = sorted([*selected, clipped_range(content, span, allowance, query_terms)])
            view = deepcopy(record)
            view["data"]["content"] = "\n[…未提供的原文…]\n".join(content[start:end] for start, end in ranges)
            view["content_truncated"] = sum(end - start for start, end in ranges) < len(content)
            view["excerpt"] = metadata(record, ranges)
            if len(json.dumps(view, ensure_ascii=False)) <= budget:
                chosen, low = (view, ranges), allowance + 1
            else:
                high = allowance - 1
        if chosen is None:
            continue
        result, selected = chosen
    return result


def catalog_excerpt(record, spans, query_terms, length=240, *, compact=False):
    ordered = ordered_spans(record, spans, query_terms)
    if not ordered:
        return None
    content = record["data"]["content"]
    interval = clipped_range(content, ordered[0], length, query_terms)
    provenance = ({"field": "content", "ranges": [interval], "total_chars": len(content),
                   "complete": interval == [0, len(content)]} if compact else metadata(record, [interval]))
    return {"summary": content[interval[0]:interval[1]], "excerpt": provenance}
