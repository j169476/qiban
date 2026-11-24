"""Rank recurring expression cues alongside habits learned from anonymous observations."""
import re

from .contracts import stable_key

HABITS = {
    "acknowledge": ("先接住对方的情绪，再看是否适合给建议。", r"先听|抱抱|辛苦了|能理解|懂你|先.*(情绪|感受)|先共情"),
    "playful": ("气氛轻松时可以顺着玩笑接一句。", r"接梗|开玩笑|笑死|哈哈|接.*玩笑"),
    "clarify": ("对方的需求含糊时，问一个短而具体的问题。", r"你是说|是指|你的意思|先问|具体是|确认.*意思"),
    "listen": ("对方想继续倾诉时，留出说下去的空间。", r"你接着说|我在听|慢慢说|说下去|倾听|倾诉"),
    "serious": ("对方转为认真时，收起玩笑并回应眼前的事。", r"说正经的|认真说|不开玩笑|说真的|收起玩笑"),
}
KEY = "shared-expression-v1"


def observe(store, platform, conversation, event_id, text, db, suggested=None):
    if platform not in {"qq", "wechat", "bilibili"}:
        return
    cues = [name for name, (_, pattern) in HABITS.items() if re.search(pattern, text[:4000])]
    if suggested in HABITS and suggested not in cues:
        cues.append(suggested)
    if not cues:
        return
    state = store.state(KEY, {}, db)
    changed = False
    for cue in cues[:2]:
        # One vote per source event and cue. The digest carries no text,
        # recipient identifiers or model-provided strings into the shared context.
        key = stable_key("observation", platform, conversation, event_id)
        prior = state.get(cue, [])
        if key not in prior and len(prior) < 12:
            state[cue] = [*prior, key]
            changed = True
    if changed:
        store.set_state(KEY, state, db)


def context(store, text=""):
    from .expression_patterns import select
    state = store.state(KEY, {})
    qualified = sorted((k for k in HABITS if len(state.get(k, [])) >= 2),
                       key=lambda k: -len(state[k]))
    return (select(store, text) + [HABITS[k][0] for k in qualified])[:3]
