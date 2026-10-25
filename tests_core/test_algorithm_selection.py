"""Platform-independent routing and exploration quality constraints."""
from collections import Counter

import pytest

from companion_core.algorithm_policy import DEFAULTS, policy
from companion_core.errors import AssistantError
from companion_core.profiles import default_profile
from companion_core.selection import Candidate, choose, contains, interests


def test_clear_interest_replaces_selection_but_ambiguity_and_unknowns_do_not():
    items = [Candidate("art", "动画分镜演示"), Candidate("other", "木工入门")]
    result = choose(items, settings={}, weights={"动画": 1})
    assert result["route"] == "algorithm" and result["selected"] == "art"
    tied = choose([items[0], Candidate("art2", "动画创作")], settings={}, weights={"动画": 1})
    assert tied["route"] == "model" and tied["reason"] == "ambiguous_ranking"
    unknown = choose([Candidate("mystery", "只有经历过才会懂")], settings={}, weights={"动画": 1})
    assert unknown["route"] == "model"
    assert not contains("case study", "as")


def test_latest_correction_wins_without_converting_exposure_to_preference():
    weights, negative = interests(["汽车", "陶艺"], {"汽车": .8}, [
        {"topic": "汽车", "latest_feedback": "not_interested", "supporting_sources": 20}])
    assert "汽车" not in weights and "汽车" in negative
    selected = choose([Candidate("car", "汽车评测"), Candidate("clay", "陶艺作品")],
                      settings={}, weights=weights, negative=negative)
    assert selected["selected"] == "clay" and "car" not in selected["eligible_ids"]
    assert choose([Candidate("car", "汽车评测")], settings={}, negative=negative)["route"] == "skip"


def test_unique_reads_and_bounded_history_do_not_count_repeated_events():
    items = [Candidate("new", "动画制作", "new"), Candidate("old", "动画片段", "familiar")]
    history = [Candidate("watched", "", "familiar", ("动画",))]
    one = choose(items, settings={}, weights={"动画": 1}, history=history)
    repeated = choose(items, settings={}, weights={"动画": 1}, history=history * 20)
    assert one == repeated
    assert one["selected"] == "new"
    assert choose([history[0]], settings={}, history=history)["route"] == "skip"
    recent = [Candidate("repeat", "", "known", ("动画",))]
    recent += [Candidate(str(i), "", "other", ("园艺",)) for i in range(10)]
    recent.append(recent[0])
    settings = {"algorithm_routing": {"selection": {"history_limit": 10}}}
    assert choose([Candidate("repeat", "动画")], settings=settings, history=recent)["route"] == "skip"


def test_exploration_selects_undercovered_direction_and_unknowns_fall_back():
    history = [Candidate(str(i), "", "artist", ("动画",)) for i in range(10)]
    candidates = [Candidate("art", "动画欣赏", "artist", ("动画",)),
                  Candidate("sky", "天文观测", "observer", ("天文",))]
    result = choose(candidates, settings={}, weights={"动画": 1}, history=history, explore=True)
    assert result["selected"] == "sky" and result["reason"] == "diversity_exploration"
    assert choose([Candidate("unknown", "完全不懂的标题")], settings={}, history=history,
                  explore=True)["route"] == "model"


def test_simulated_browsing_preserves_exploration_and_avoids_single_author():
    history, routes = [], Counter()
    for turn in range(60):
        explore = (turn + 1) % 5 == 0
        candidates = [Candidate(f"{turn}-art", "动画作品", "artist", ("动画",)),
                      Candidate(f"{turn}-sky", "天文观测", "observer", ("天文",)),
                      Candidate(f"{turn}-garden", "园艺记录", "gardener", ("园艺",))]
        result = choose(candidates, settings={}, weights={"动画": 1}, history=history,
                        explore=explore, seed=str(turn))
        routes[result["route"]] += 1
        # Frozen fallback for ambiguous routine choices; no actual model call.
        selected = next(c for c in candidates if c.id == result["selected"]) if result["selected"] else candidates[0]
        if explore:
            assert selected.topics != ("动画",)
        history.append(selected)
    topics = Counter(item.topics[0] for item in history)
    authors = Counter(item.author for item in history)
    assert topics["动画"] == 48 and min(topics.values()) >= 5
    assert len(authors) == 3 and max(authors.values()) / len(history) <= .8
    assert routes["algorithm"] >= 12


def test_goal_selection_requires_full_short_phrase_coverage():
    candidates = [Candidate("yes", "Python 列表排序 实例"), Candidate("no", "Python 文件读写")]
    result = choose(candidates, settings={}, query="Python 列表排序")
    assert result["selected"] == "yes"
    assert choose(candidates, settings={}, query="掌握异步取消条件和异常恢复机制")["route"] == "model"
    requested = choose(candidates, settings={}, query="Python 列表排序", negative=["Python"], history=candidates[:1])
    assert requested["selected"] == "yes"  # Explicit study can revisit a former ordinary dislike/read.


def test_core_policy_is_optional_editable_and_validated():
    profile = default_profile()
    old = profile.defaults["companion"]
    old.pop("algorithm_routing")
    profile.validate("companion", old)
    assert policy(old, "selection") == DEFAULTS["selection"]
    old["algorithm_routing"] = {"selection": {"enabled": False}}
    profile.validate("companion", old)
    assert choose([Candidate("a", "动画")], settings=old, weights={"动画": 1})["reason"] == "disabled"
    old["algorithm_routing"]["selection"]["history_limit"] = -1
    with pytest.raises(AssistantError):
        profile.validate("companion", old)
