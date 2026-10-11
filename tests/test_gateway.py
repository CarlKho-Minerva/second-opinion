from __future__ import annotations

import json

import httpx
import pytest

from second_opinion import config
from second_opinion.gateway import (Action, Gateway, SameFamilyError, parse_verdict, reviewer_order, review_prompt)
from second_opinion.providers import ModelClient, ProviderError
from second_opinion.records import RecordStore
from second_opinion.router import Router, decide

from .conftest import (APPROVE, CHEAP_IRREVERSIBLE, CHEAP_REVERSIBLE, COSTLY, PASS, REJECT, FakeModels, jev_answers,
                       make_action, make_gateway, run)


# ---------------------------------------------------------------- router classes
@pytest.mark.parametrize("reversible,outward,costly,floor,expected", [
    (True, False, False, None, "pass"),
    (True, True, False, None, "review"),      # reaches someone else
    (False, False, False, None, "review"),    # cannot be undone
    (True, False, True, None, "review"),      # a mistake is costly
    (True, False, False, "review", "review"), # code-owned floor beats the model
])
def test_router_policy_table(reversible, outward, costly, floor, expected):
    assert decide(reversible, outward, costly, floor) == expected


def test_router_classes_from_jev():
    r = run(Router(FakeModels(jev=PASS)).route("notes.append", {"note": "x"}))
    assert (r.route, r.reversible, r.stakes, r.source) == ("pass", True, "cheap", "jev")
    r = run(Router(FakeModels(jev=CHEAP_REVERSIBLE)).route("event.rsvp", {"event": "x"}))
    assert (r.route, r.reversible, r.outward, r.stakes) == ("review", True, True, "cheap")
    r = run(Router(FakeModels(jev=COSTLY)).route("email.send", {"to": "x"}))
    assert (r.route, r.reversible, r.stakes) == ("review", False, "costly")


def test_router_floor_and_unknown_tool_are_never_passed():
    assert run(Router(FakeModels(jev=PASS)).route("payment.pay", {"amount_usd": 1})).route == "review"
    assert run(Router(FakeModels(jev=PASS)).route("made.up.tool", {})).route == "review"


def test_router_falls_back_to_cheap_model_and_says_so():
    models = FakeModels({"google": ['{"reversible": true, "outward": false, "cost_if_wrong": "none", "check": "cheap"}']},
                        jev=ProviderError("jev: timed out"))
    r = run(Router(models).route("notes.append", {"note": "x"}))
    assert r.source == "fallback_llm" and r.route == "pass"
    assert "Jev unavailable" in r.warning


def test_router_unreachable_assumes_the_worst():
    models = FakeModels({"google": [ProviderError("down")]}, jev=ProviderError("down"))
    r = run(Router(models).route("notes.append", {"note": "x"}))
    assert (r.source, r.route, r.reversible, r.stakes) == ("fail_closed", "review", False, "costly")


def test_router_malformed_answers_fall_through():
    models = FakeModels({"google": ["I think it is fine"]}, jev={"reversible": {"oops": 1}})
    r = run(Router(models).route("notes.append", {"note": "x"}))
    assert r.source == "fail_closed" and "malformed" in r.warning


# ---------------------------------------------------------------- reviewer is a different family
@pytest.mark.parametrize("proposer", config.FAMILIES)
def test_reviewer_order_never_contains_the_proposer(proposer):
    order = reviewer_order(proposer, "cross")
    assert proposer not in order and len(order) == len(config.FAMILIES) - 1


@pytest.mark.parametrize("proposer", config.FAMILIES)
def test_second_opinion_comes_from_another_family(proposer, store, spy):
    models = FakeModels({f: [APPROVE] for f in config.FAMILIES})
    rec = run(make_gateway(models, store, spy).submit(make_action(proposer)))
    assert rec["opinions"][0]["family"] != proposer
    assert all(family != proposer for family, _, _ in models.asked)


def test_same_family_review_is_refused(store, spy):
    gw = make_gateway(FakeModels({"google": [APPROVE]}), store, spy)
    rec = {"attempts": [], "warnings": [], "opinions": [], "cost_usd": 0.0}

    async def say(_):
        return None

    with pytest.raises(SameFamilyError):
        run(gw._opinion(make_action("google"), "second_opinion", ["google"], rec, say))


def test_only_one_family_configured_fails_closed(store, spy):
    gw = make_gateway(FakeModels({"google": [APPROVE]}), store, spy, families=("google",))
    rec = run(gw.submit(make_action("google")))
    assert rec["outcome"] == "blocked_fail_closed" and spy.ran == []


def test_cross_reviewer_never_sees_the_proposers_reasoning(store, spy):
    models = FakeModels({f: [APPROVE] for f in config.FAMILIES})
    run(make_gateway(models, store, spy, mode="cross").submit(make_action("google")))
    assert all("SECRET-RATIONALE" not in user for _, _, user in models.asked)
    models = FakeModels({f: [APPROVE] for f in config.FAMILIES})
    run(make_gateway(models, store, spy, mode="self").submit(make_action("google")))
    assert models.asked[0][0] == "google" and "SECRET-RATIONALE" in models.asked[0][2]
    assert "SECRET-RATIONALE" not in review_prompt(make_action(), include_rationale=False)


# ---------------------------------------------------------------- agreement, disagreement, cards
def test_harmless_action_passes_without_any_review(store, spy):
    models = FakeModels({}, jev=PASS)
    rec = run(make_gateway(models, store, spy).submit(make_action(tool="notes.append")))
    assert rec["outcome"] == "executed_passthrough" and rec["opinions"] == [] and spy.ran == ["notes.append"]


def test_agreement_runs_the_action(store, spy):
    models = FakeModels({f: [APPROVE] for f in config.FAMILIES})
    rec = run(make_gateway(models, store, spy).submit(make_action()))
    assert rec["outcome"] == "executed_agreed" and rec["executed"] and spy.ran == ["email.send"]


def test_disagreement_on_a_costly_action_raises_a_card_and_blocks(store, spy):
    models = FakeModels({f: [REJECT] for f in config.FAMILIES}, jev=COSTLY)
    gw = make_gateway(models, store, spy)
    rec = run(gw.submit(make_action("google")))
    assert rec["outcome"] == "pending_human" and rec["executed"] is False
    assert spy.ran == []                                   # blocked: nothing ran
    assert len(models.asked) == 1                          # no tiebreak on costly cases
    card = rec["card"]
    assert card["opinions"][0]["who"] == "proposed it" and card["opinions"][1]["verdict"] == "reject"
    assert card["evidence"] and card["args"] == {"to": "a@b.example", "body": "hello"}
    assert rec["id"] in gw.pending


def test_human_reject_keeps_it_blocked_and_approve_runs_it(store, spy):
    models = FakeModels({f: [REJECT] for f in config.FAMILIES}, jev=COSTLY)
    gw = make_gateway(models, store, spy)
    first = run(gw.submit(make_action()))
    ruled = run(gw.resolve(first["id"], approve=False))
    assert ruled["outcome"] == "blocked_human_rejected" and spy.ran == []
    with pytest.raises(KeyError):                           # a card can be ruled on once
        run(gw.resolve(first["id"], approve=True))
    second = run(gw.submit(make_action()))
    ruled = run(gw.resolve(second["id"], approve=True))
    assert ruled["outcome"] == "executed_human_approved" and spy.ran == ["email.send"]


def test_cheap_disagreement_goes_to_a_third_family(store, spy):
    # proposer google -> reviewer anthropic rejects -> openai breaks the tie
    models = FakeModels({"anthropic": [REJECT], "openai": [APPROVE]}, jev=CHEAP_REVERSIBLE)
    rec = run(make_gateway(models, store, spy).submit(make_action("google", tool="event.rsvp")))
    assert [o["family"] for o in rec["opinions"]] == ["anthropic", "openai"]
    assert rec["outcome"] == "executed_tiebreak" and spy.ran == ["event.rsvp"]
    models = FakeModels({"anthropic": [REJECT], "openai": [REJECT]}, jev=CHEAP_REVERSIBLE)
    spy.ran.clear()
    rec = run(make_gateway(models, store, spy).submit(make_action("google", tool="event.rsvp")))
    assert rec["outcome"] == "blocked_tiebreak" and spy.ran == []


def test_cheap_disagreement_with_no_third_model_goes_to_a_person(store, spy):
    models = FakeModels({"anthropic": [REJECT], "openai": [ProviderError("down")]}, jev=CHEAP_REVERSIBLE)
    rec = run(make_gateway(models, store, spy).submit(make_action("google", tool="event.rsvp")))
    assert rec["outcome"] == "pending_human" and spy.ran == []


# ---------------------------------------------------------------- a record for every action
def test_a_decision_record_is_written_for_every_action(tmp_path, spy):
    store = RecordStore(tmp_path / "d.jsonl")
    scenarios = [
        (FakeModels({}, jev=PASS), "cross"),                                                  # pass
        (FakeModels({f: [APPROVE] for f in config.FAMILIES}), "cross"),                       # agreed
        (FakeModels({f: [REJECT] for f in config.FAMILIES}), "cross"),                        # card
        (FakeModels({f: [REJECT] for f in config.FAMILIES}, jev=CHEAP_IRREVERSIBLE), "cross"),  # blocked by tiebreak
        (FakeModels({f: [ProviderError("down")] for f in config.FAMILIES}), "cross"),         # fail closed
        (FakeModels({}, jev=ProviderError("down")), "cross"),                                 # router and all else down
        (FakeModels({f: [APPROVE] for f in config.FAMILIES}), "self"),
        (FakeModels({}), "none"),
    ]
    outcomes = []
    for models, mode in scenarios:
        outcomes.append(run(make_gateway(models, store, spy, mode=mode).submit(make_action()))["outcome"])
    lines = [json.loads(x) for x in (tmp_path / "d.jsonl").read_text().splitlines()]
    assert len(lines) == len(scenarios) == store.written
    assert [x["outcome"] for x in lines] == outcomes
    assert len({x["id"] for x in lines}) == len(lines)
    for x in lines:
        assert {"id", "ts", "action", "proposer", "evidence", "router", "opinions", "outcome", "executed",
                "added_latency_ms", "cost_usd", "simulated", "warnings"} <= set(x)
        assert x["simulated"] is True


def test_human_ruling_appends_a_second_line_and_keeps_the_first(tmp_path, spy):
    store = RecordStore(tmp_path / "d.jsonl")
    gw = make_gateway(FakeModels({f: [REJECT] for f in config.FAMILIES}), store, spy)
    first = run(gw.submit(make_action()))
    run(gw.resolve(first["id"], approve=False))
    lines = [json.loads(x) for x in (tmp_path / "d.jsonl").read_text().splitlines()]
    assert [x["outcome"] for x in lines] == ["pending_human", "blocked_human_rejected"]
    assert lines[0]["id"] == lines[1]["id"] and lines[1]["human"]["decision"] == "reject"


def test_no_record_means_no_action(spy):
    class BrokenStore(RecordStore):
        def write(self, record):
            raise OSError("disk full")

    gw = make_gateway(FakeModels({f: [APPROVE] for f in config.FAMILIES}), BrokenStore(None), spy)
    with pytest.raises(OSError):
        run(gw.submit(make_action()))
    assert spy.ran == []


# ---------------------------------------------------------------- failure paths
def test_provider_timeout_fails_closed_for_an_irreversible_action(store, spy):
    """A real ModelClient whose HTTP layer times out on every reviewer. The router answers."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.typesafe.ai":
            return httpx.Response(200, json={"model": "jev-test", "answers": COSTLY,
                                             "usage": {"input_tokens": 300, "output_tokens": 20}})
        raise httpx.ReadTimeout("timed out", request=request)

    env = {"TYPESAFE_API_KEY": "k-typesafe-00000000", "GEMINI_API_KEY": "k-gemini-00000000",
           "ANTHROPIC_API_KEY": "k-anthropic-00000000", "AZURE_OPENAI_API_KEY": "k-azure-00000000",
           "AZURE_OPENAI_BASE_URL": "https://example.invalid/openai/v1"}
    models = ModelClient(env=env, timeout=0.5, down=set())
    models._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=0.5)
    rec = run(Gateway(models, store, mode="cross", execute=spy).submit(make_action("google")))
    assert rec["router"]["source"] == "jev" and rec["router"]["reversible"] is False
    assert rec["outcome"] == "blocked_fail_closed" and rec["executed"] is False and spy.ran == []
    assert {a["family"] for a in rec["attempts"]} == {"anthropic", "openai"}       # both others were tried
    assert all("timed out" in a["error"] for a in rec["attempts"])
    assert "k-anthropic" not in json.dumps(rec)                                     # no key in any record


def test_reviewers_down_reversible_action_proceeds_with_a_logged_warning(store, spy):
    models = FakeModels({f: [ProviderError("timed out")] for f in config.FAMILIES}, jev=CHEAP_REVERSIBLE)
    rec = run(make_gateway(models, store, spy).submit(make_action("google", tool="event.rsvp")))
    assert rec["outcome"] == "executed_unreviewed" and spy.ran == ["event.rsvp"]
    assert any("ran unreviewed" in w for w in rec["warnings"])


def test_first_reviewer_down_falls_over_to_the_third_family(store, spy):
    models = FakeModels({"anthropic": [ProviderError("down")], "openai": [APPROVE]})
    rec = run(make_gateway(models, store, spy).submit(make_action("google")))
    assert rec["opinions"][0]["family"] == "openai" and rec["outcome"] == "executed_agreed"
    assert any("anthropic unavailable" in w for w in rec["warnings"])


def test_router_unreachable_irreversible_path_still_reviews_and_blocks_on_no_reviewer(store, spy):
    models = FakeModels({f: [ProviderError("down")] for f in config.FAMILIES}, jev=ProviderError("down"))
    rec = run(make_gateway(models, store, spy).submit(make_action(tool="notes.append")))
    assert rec["router"]["source"] == "fail_closed" and rec["outcome"] == "blocked_fail_closed" and spy.ran == []


def test_malformed_model_output_is_retried_then_passed_to_the_next_family(store, spy):
    models = FakeModels({"anthropic": ["Sure! Looks good to me.", "```json\n{\"verdict\": \"maybe\"}\n```"],
                         "openai": ['```json\n{"verdict": "Approve", "reason": "ok"}\n```']})
    rec = run(make_gateway(models, store, spy).submit(make_action("google")))
    assert [a["family"] for a in rec["attempts"]] == ["anthropic", "anthropic"]
    assert rec["opinions"][0]["family"] == "openai" and rec["outcome"] == "executed_agreed"


def test_all_output_malformed_fails_closed(store, spy):
    models = FakeModels({f: ["not json at all"] for f in config.FAMILIES})
    rec = run(make_gateway(models, store, spy).submit(make_action("google")))
    assert rec["outcome"] == "blocked_fail_closed" and spy.ran == []


@pytest.mark.parametrize("text", ["", "approve", '{"verdict": "yes"}', '{"reason": "fine"}', "[1, 2]", '{"verdict": null}'])
def test_parse_verdict_rejects_anything_unclear(text):
    with pytest.raises(ValueError):
        parse_verdict(text)


def test_parse_verdict_reads_fenced_json():
    assert parse_verdict('```json\n{"verdict": "REJECT", "reason": "wrong  event"}\n```') == ("reject", "wrong event")


def test_an_unexpected_bug_blocks_the_action_and_is_recorded(store, spy):
    class Boom(FakeModels):
        async def complete(self, *a, **k):
            raise RuntimeError("surprise")

    rec = run(make_gateway(Boom({}), store, spy).submit(make_action()))
    assert rec["outcome"] == "blocked_error" and spy.ran == [] and store.written == 1


def test_error_text_is_scrubbed_of_keys():
    models = ModelClient(env={"GEMINI_API_KEY": "super-secret-key-123456"}, timeout=1, down=set())
    assert "super-secret-key-123456" not in models._scrub("HTTP 400 key=super-secret-key-123456 was bad")
    run(models.aclose())


def test_action_roundtrip_and_tools_are_simulated():
    from second_opinion import tools
    a = Action.from_dict({"id": "x", "tool": "email.send", "proposer_family": "google"})
    assert a.args == {} and a.evidence == []
    out = tools.execute("email.send", {"to": "a@b.example"})
    assert out["simulated"] is True and "SIMULATED" in out["result"]
