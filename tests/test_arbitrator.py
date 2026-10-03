from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from pitwall.config.models import JevSettings
from pitwall.state.session import Snapshot
from pitwall.voice.arbitrator import (
    ArbCandidate,
    HeapArbitrator,
    JevArbitrator,
    ShadowArbitrator,
    build_digest,
    build_live_arbitrator,
    mask_names,
    name_labels,
    shadow_report,
)

FIXTURES = Path(__file__).parent / "fixtures" / "arbitration"


def _cands() -> list[ArbCandidate]:
    return [
        ArbCandidate("c1", "gap_ahead", 2, 10.0, 12, 900.0, "Gap ahead 1.2, closing"),
        ArbCandidate("c2", "pit_window_open", 2, 10.0, 12, 900.0, "Window is open"),
        ArbCandidate("c3", "deg_report", 3, 10.0, 12, 900.0, "Rears are going off"),
    ]


def _answer(choice: str, probs: dict[str, float]) -> dict[str, Any]:
    return {
        "model": "typesafe-ai/jev",
        "answers": {"best_call": {"type": "choice", "choice": choice, "probabilities": probs}},
    }


def _jev(
    handler: Callable[[httpx.Request], httpx.Response],
    **settings: object,
) -> tuple[JevArbitrator, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append({"body": json.loads(request.content), "headers": dict(request.headers)})
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    cfg = JevSettings(enabled=True, arbitrate=True, **settings)  # type: ignore[arg-type]
    return JevArbitrator(cfg, api_key="test-key", client=client), sent


def _ok(choice: str, probs: dict[str, float]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(200, json=_answer(choice, probs))


def test_heap_arbitrator_is_priority_then_time() -> None:
    cands = [
        ArbCandidate("a", "a", 3, 1.0, 1, 0.0, "a"),
        ArbCandidate("b", "b", 2, 2.0, 1, 0.0, "b"),
        ArbCandidate("c", "c", 2, 1.5, 1, 0.0, "c"),
    ]
    r = HeapArbitrator().rank(cands, {})
    assert r.order == ("c", "b", "a")
    assert r.arbitrated_by == "heap"


def test_request_is_a_choice_over_call_ids_plus_none() -> None:
    jev, sent = _jev(_ok("c2", {"c1": 0.1, "c2": 0.85, "c3": 0.05}))
    jev.rank(_cands(), {"now": 10.0, "lap": 12})
    body = sent[0]["body"]
    assert sent[0]["headers"]["authorization"] == "Bearer test-key"
    assert body["model"] == "typesafe-ai/jev"
    q = body["questions"]["best_call"]
    assert q["type"] == "choice"
    assert set(q["criteria"]) == {"c1", "c2", "c3", "none"}
    assert "Window is open" in q["criteria"]["c2"]
    state = body["state"]
    assert state["heap_order"] == ["c1", "c2", "c3"]
    assert state["last_pick"] is None and state["last_pick_age_s"] is None
    assert state["doctrine"]


def test_confident_pick_reorders() -> None:
    jev, _ = _jev(_ok("c2", {"c1": 0.1, "c2": 0.85, "c3": 0.05}))
    r = jev.rank(_cands(), {"now": 10.0})
    assert r.order == ("c2", "c1", "c3")
    assert r.arbitrated_by == "jev"
    assert r.pick == "c2"
    assert r.confidence == pytest.approx(0.85)
    assert r.model == "typesafe-ai/jev"
    assert r.latency_ms is not None


def test_timeout_falls_back_to_heap() -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    jev, _ = _jev(boom)
    r = jev.rank(_cands(), {"now": 10.0})
    assert (r.order, r.arbitrated_by) == (("c1", "c2", "c3"), "jev_timeout")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "nope"}),
        httpx.Response(200, json={"no": "answers"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=_answer("c9", {"c9": 0.99})),
    ],
)
def test_errors_and_unknown_choices_fall_back_to_heap(response: httpx.Response) -> None:
    jev, _ = _jev(lambda _r: response)
    r = jev.rank(_cands(), {"now": 10.0})
    assert (r.order, r.arbitrated_by) == (("c1", "c2", "c3"), "jev_error")


def test_none_abstains() -> None:
    jev, _ = _jev(_ok("none", {"none": 0.9, "c1": 0.05, "c2": 0.05}))
    r = jev.rank(_cands(), {"now": 10.0})
    assert (r.order, r.arbitrated_by) == (("c1", "c2", "c3"), "jev_abstain")


@pytest.mark.parametrize(
    "probs",
    [
        {"c1": 0.3, "c2": 0.55, "c3": 0.15},  # below the confidence threshold
        {"c1": 0.0, "c2": 0.59, "c3": 0.41},  # just below 0.6
        {"c1": 0.5, "c2": 0.6, "c3": 0.0},  # above 0.6 but within 0.15 of the heap head
        {"c1": 0.26, "c2": 0.4, "c3": 0.34},  # neither
    ],
)
def test_low_confidence_or_margin_keeps_heap(probs: dict[str, float]) -> None:
    jev, _ = _jev(_ok("c2", probs))
    r = jev.rank(_cands(), {"now": 10.0})
    assert (r.order, r.arbitrated_by) == (("c1", "c2", "c3"), "jev_margin")


def test_heap_head_pick_needs_only_the_threshold() -> None:
    jev, _ = _jev(_ok("c1", {"c1": 0.7, "c2": 0.3}))
    r = jev.rank(_cands(), {"now": 10.0})
    assert (r.order, r.arbitrated_by, r.pick) == (("c1", "c2", "c3"), "jev", "c1")


def test_sticky_window_then_flap_abstains() -> None:
    probs = {"c1": 0.05, "c2": 0.05, "c3": 0.05}
    answers = iter(["c2", "c3", "c3", "c2"])

    def handler(_r: httpx.Request) -> httpx.Response:
        choice = next(answers)
        return httpx.Response(200, json=_answer(choice, {**probs, choice: 0.85}))

    jev, sent = _jev(handler)
    first = jev.rank(_cands(), {"now": 10.0})
    assert first.order[0] == "c2"
    # A different pick 2 s later: sticky keeps c2.
    second = jev.rank(_cands(), {"now": 12.0})
    assert (second.order[0], second.arbitrated_by) == ("c2", "jev")
    assert sent[1]["body"]["state"]["last_pick"] == "c2"
    assert sent[1]["body"]["state"]["last_pick_age_s"] == 2.0
    # Contradicted again inside the window: flapping, abstain to the heap.
    third = jev.rank(_cands(), {"now": 13.0})
    assert (third.order, third.arbitrated_by) == (("c1", "c2", "c3"), "jev_flap")
    # Outside the window a new pick is free to land.
    fourth = jev.rank(_cands(), {"now": 30.0})
    assert (fourth.order[0], fourth.arbitrated_by) == ("c2", "jev")


def test_single_candidate_never_calls_jev() -> None:
    jev, sent = _jev(_ok("c1", {"c1": 1.0}))
    r = jev.rank(_cands()[:1], {"now": 10.0})
    assert r.arbitrated_by == "heap"
    assert sent == []


def test_max_candidates_caps_the_question() -> None:
    jev, sent = _jev(_ok("c1", {"c1": 0.9}), max_candidates=2)
    jev.rank(_cands(), {"now": 10.0})
    assert set(sent[0]["body"]["questions"]["best_call"]["criteria"]) == {"c1", "c2", "none"}


def test_shadow_keeps_heap_and_reports_diffs() -> None:
    jev, _ = _jev(_ok("c2", {"c1": 0.1, "c2": 0.85}))
    shadow = ShadowArbitrator(jev)
    r = shadow.rank(_cands(), {"now": 10.0, "lap": 12, "session_uid": 7})
    assert (r.order, r.arbitrated_by) == (("c1", "c2", "c3"), "heap")
    report = shadow_report(shadow.decisions)
    assert report["decision_points"] == 1
    assert report["reordered"] == 1
    assert report["diffs"][0]["heap_first"] == "gap_ahead"
    assert report["diffs"][0]["jev_first"] == "pit_window_open"


def test_live_arbitrator_needs_enabled_arbitrate_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERCEL_AI_GATEWAY_KEY", raising=False)
    assert build_live_arbitrator(JevSettings()) is None
    assert build_live_arbitrator(JevSettings(enabled=True, arbitrate=True)) is None
    monkeypatch.setenv("VERCEL_AI_GATEWAY_KEY", "k")
    assert build_live_arbitrator(JevSettings(enabled=True)) is None
    live = build_live_arbitrator(JevSettings(enabled=True, arbitrate=True))
    assert isinstance(live, JevArbitrator)


def test_digest_carries_no_driver_names() -> None:
    snap = Snapshot(now=5.0, lap_num=3, rival_ahead_name="VERSTAPPEN", active_plan="A")
    digest = build_digest(snap)
    assert digest["lap"] == 3
    assert digest["plan"]["active"] == "A"
    assert "VERSTAPPEN" not in json.dumps(digest)
    labels = name_labels(snap)
    assert mask_names("Box to cover VERSTAPPEN", labels) == "Box to cover the car ahead"


def _fixture_ids() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json"))


@pytest.mark.parametrize("name", _fixture_ids())
def test_golden_situations(name: str) -> None:
    """Regression pins: one decision point and Jev answer per file, and the order it must give."""
    fx = json.loads((FIXTURES / f"{name}.json").read_text())
    cands = [ArbCandidate.from_json(c) for c in fx["candidates"]]
    jev, _ = _jev(lambda _r: httpx.Response(200, json=fx["response"]))
    rank = jev.rank(cands, fx["digest"])
    rules = {c.call_id: c.rule_id for c in cands}
    assert rank.arbitrated_by == fx["expect"]["arbitrated_by"]
    assert [rules[i] for i in rank.order] == fx["expect"]["order"]
