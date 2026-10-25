"""Corpus-aware lexical ranking; Codex supplies semantic query expansion when available."""

import math
import re

FUNCTION_WORDS = frozenset("a an the of to for in on at by as is are was were be been it its this that these those "
                           "and or but if so than then not no how why what when where can could should would "
                           "不能 不要 不是 没有 是否 可以 需要 什么 怎么 如何 这个 那个".split())


def concept_weight(term):
    # Questions and negation remain in the supplied evidence. They are weak
    # retrieval signals: a shared '不能' or a crossing bigram such as '天的'
    # should not outrank a specific concept. Full phrases still vote normally.
    return .2 if term in FUNCTION_WORDS or len(term) == 2 and any(c in "的了呢吗" for c in term) else 1.0


def lexical_length(text):
    """Comparable lexical length for Han phrases and alphabetic words.

    Character length penalizes an English source several times more than a Han
    source expressing the same fact. Treat an ASCII word as two characters (a
    typical Han keyword); whitespace is not content. This is ranking only, not
    excerpt offsets or the actual model context budget.
    """
    return max(1, len(re.sub(r"\s+", "", re.sub(r"[a-z0-9_]+", "xx", text))))


def scores(records, query, queries=(), *, passages=None):
    from .storage import search_terms, searchable_text

    # Bound each expression independently so a long question cannot crowd out a later concept.
    terms = sorted({term for q in [query, *queries[:5]] for term in search_terms(q)})
    if not terms or not records:
        return {}
    bodies = [searchable_text(r["data"]) for r in records]
    windows = []
    for record, body in zip(records, bodies, strict=True):
        spans = (passages or {}).get((record["kind"], record["data"]["id"]), [])
        if spans:
            metadata = searchable_text({**record["data"], "content": ""})
            windows.append([metadata + " " + record["data"]["content"][start:end].casefold() for start, end in spans])
        else:
            windows.append([body])
    # English words must match tokens: 'as' should not match every 'case' or 'task'.
    words = [set(re.findall(r"[a-z0-9_]+", body)) for body in bodies]
    window_lengths = [[lexical_length(span) for span in spans] for spans in windows]
    lengths = [max(spans) for spans in window_lengths]
    # A library dominated by tiny status messages must not make an ordinary
    # sentence look abnormally long while chunked records escape that penalty.
    # Use the same short-note floor for both sides of length normalization.
    average = max(64, sum(max(64, length) for length in lengths) / len(lengths))
    # Distinct concepts vote once, including when Codex repeats or reorders an expression.
    values = [0.0] * len(records)
    passage_values = [[0.0] * len(spans) for spans in windows]
    passage_words = [[set(re.findall(r"[a-z0-9_]+", span)) for span in spans] if len(spans) > 1 else [words[i]]
                     for i, spans in enumerate(windows)]
    for term in terms:
        english = re.fullmatch(r"[a-z0-9_]+", term)
        matched = [i for i, (body, word_set) in enumerate(zip(bodies, words, strict=True))
                   if (term in word_set if english else term in body)]
        weight = concept_weight(term) * math.log(1 + (len(records) - len(matched) + .5) / (len(matched) + .5))
        for i in matched:
            values[i] += weight
            if len(windows[i]) > 1:
                for j, span in enumerate(windows[i]):
                    if term in passage_words[i][j] if english else term in span:
                        passage_values[i][j] += weight
    keys = [(r["kind"], r["data"]["id"]) for r in records]
    def normalization(length):
        return 2.2 / (1 + 1.2 * (.25 + .75 * max(64, length) / average))

    result = {}
    for i, key in enumerate(keys):
        if len(windows[i]) > 1:
            # A source window's extra characters retain context and qualifiers;
            # they are not evidence against its short local fact. Chunking already
            # prevents unrelated sections from pooling votes. Do not punish a
            # 1200-character context window against a corpus of tiny chat notes,
            # and normalize the actual winning window rather than its longest peer.
            result[key] = max(value * normalization(min(length, average))
                              for value, length in zip(passage_values[i], window_lengths[i], strict=True))
        else:
            result[key] = values[i] * normalization(lengths[i])
    return result
