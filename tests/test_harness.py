import threading
import time
from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from harness.cache import (
    MemoryCache,
    MongoCache,
    SingleFlight,
    cache_key,
    normalize_query,
)
from harness.client import LLMClient, LLMError, ProviderError
from harness.fake import ScriptedProvider, good_results
from harness.guard import (
    MemoryBudget,
    MemoryRateLimiter,
    MongoBudget,
    MongoRateLimiter,
    cost_usd,
)
from harness.metrics import Metrics
from harness.service import Generator
from harness.validation import parse_page_html, parse_search_results


def make_client(provider, **kw):
    kw.setdefault("sleep", lambda s: None)
    return LLMClient(provider, **kw)


# --- client: retries, limits, classification ---------------------------------


def test_client_returns_validated_value_on_first_try():
    provider = ScriptedProvider([good_results("q")])
    out = make_client(provider).complete(
        "p", model="m", max_tokens=10, validate=parse_search_results
    )
    assert len(out.value) == 5 and out.attempts == 1 and provider.calls == 1


def test_client_retries_transient_errors_then_succeeds():
    sleeps = []
    provider = ScriptedProvider(
        [ProviderError("rate_limited"), ProviderError("timeout"), good_results("q")]
    )
    metrics = Metrics()
    out = make_client(provider, sleep=sleeps.append, metrics=metrics).complete(
        "p", model="m", max_tokens=10, validate=parse_search_results
    )
    assert out.attempts == 3
    assert len(sleeps) == 2
    assert metrics.snapshot()["retries"] == 2


def test_client_does_not_retry_auth_errors():
    provider = ScriptedProvider([ProviderError("auth"), good_results("q")])
    with pytest.raises(LLMError) as e:
        make_client(provider).complete("p", model="m", max_tokens=10)
    assert e.value.kind == "auth" and e.value.attempts == 1 and provider.calls == 1


def test_client_retries_invalid_output_and_stops_at_max_attempts():
    provider = ScriptedProvider(["not json"] * 5)
    with pytest.raises(LLMError) as e:
        make_client(provider, max_attempts=3).complete(
            "p", model="m", max_tokens=10, validate=parse_search_results
        )
    assert e.value.kind == "invalid_output"
    assert provider.calls == 3


def test_client_deadline_covers_retries():
    now = [0.0]
    provider = ScriptedProvider([ProviderError("unavailable")] * 10)

    def sleep(s):
        now[0] += 5.0  # each backoff "takes" 5 s

    client = LLMClient(
        provider, max_attempts=10, deadline_s=12, sleep=sleep, clock=lambda: now[0]
    )
    with pytest.raises(LLMError):
        client.complete("p", model="m", max_tokens=10)
    assert provider.calls <= 3  # the deadline, not max_attempts, ended the loop


def test_backoff_never_sleeps_past_deadline():
    now = [0.0]
    slept = []
    client = LLMClient(
        ScriptedProvider([ProviderError("timeout")] * 3),
        max_attempts=3,
        deadline_s=1.0,
        backoff_base_s=100,
        sleep=slept.append,
        clock=lambda: now[0],
    )
    with pytest.raises(LLMError):
        client.complete("p", model="m", max_tokens=10)
    assert all(s <= 1.0 for s in slept)


def _sdk_error(cls):
    """Build an OpenAI SDK exception without an HTTP response object."""
    exc = cls.__new__(cls)
    Exception.__init__(exc, cls.__name__)
    return exc


def test_openai_provider_maps_sdk_errors_to_kinds():
    import openai
    from harness.client import OpenAIProvider

    cases = {
        openai.APITimeoutError: "timeout",
        openai.APIConnectionError: "unavailable",
        openai.InternalServerError: "unavailable",
        openai.RateLimitError: "rate_limited",
        openai.AuthenticationError: "auth",
        openai.BadRequestError: "bad_request",
    }
    for cls, kind in cases.items():
        exc = _sdk_error(cls)

        class RaisingSDK:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        raise exc

        with pytest.raises(ProviderError) as e:
            OpenAIProvider(RaisingSDK).complete("p", model="m", max_tokens=1, timeout=1)
        assert e.value.kind == kind, cls


# --- validation -----------------------------------------------------------------


def test_search_validation_strips_fences():
    assert len(parse_search_results("```json\n" + good_results("q") + "\n```")) == 5


def test_search_validation_keeps_only_complete_items_when_truncated():
    text = good_results("q")
    cut = text[: text.index("result 3")]  # items 1-2 complete, item 3 cut off
    titles = [r["title"] for r in parse_search_results(cut, min_results=1)]
    assert titles == ["q result 1", "q result 2"]


def test_search_validation_rejects_too_few_results():
    with pytest.raises(ValueError):
        parse_search_results(good_results("q", n=2))


def test_search_validation_rejects_wrong_schema_and_bad_urls():
    with pytest.raises(ValueError):
        parse_search_results('[{"name": "x", "link": "y"}]')
    mixed = (
        '[{"title":"a","snippet":"b","url":"javascript:alert(1)"},'
        '{"title":"c","snippet":"d","url":"https://ok.com"}]'
    )
    assert parse_search_results(mixed, min_results=1) == [
        {"title": "c", "snippet": "d", "url": "https://ok.com"}
    ]


def test_search_validation_caps_result_count():
    assert len(parse_search_results(good_results("q", n=9))) == 5


def test_search_validation_repairs_trailing_commas():
    text = (
        '[{"title":"a","snippet":"b","url":"https://x.com",},'
        '{"title":"c","snippet":"d","url":"https://y.com"},]'
    )
    assert [r["title"] for r in parse_search_results(text, min_results=2)] == ["a", "c"]


def test_repair_leaves_commas_and_brackets_inside_strings_alone():
    text = '[{"title":"wait, ]","snippet":"x, }","url":"https://x.com"}]'
    assert parse_search_results(text, min_results=1)[0]["title"] == "wait, ]"


def test_search_validation_repairs_invalid_escapes_and_control_chars():
    text = '[{"title":"it\\\'s","snippet":"line1\nline2","url":"https://x.com"}]'
    result = parse_search_results(text, min_results=1)[0]
    assert result["title"] == "it\\'s"
    assert result["snippet"] == "line1\nline2"


def test_search_validation_replaces_lone_surrogates():
    text = '[{"title":"bad \\ud800 char","snippet":"s","url":"https://x.com"}]'
    title = parse_search_results(text, min_results=1)[0]["title"]
    title.encode("utf-8")  # must not raise
    assert title.startswith("bad ")


def test_page_validation():
    assert parse_page_html("```html\n<h1>T</h1><p>x</p>\n```") == "<h1>T</h1><p>x</p>"
    with pytest.raises(ValueError):
        parse_page_html("<h1>T</h1><p>cut off mid-sen")
    with pytest.raises(ValueError):
        parse_page_html("just prose")


# --- cache keys and stores --------------------------------------------------------


def test_cache_key_covers_everything_that_changes_output():
    base = dict(model="gpt-4", prompt_version="v1", params={"max_tokens": 500})
    k = cache_key("search", "cats", **base)
    assert k == cache_key("search", normalize_query("  CATS "), **base)
    assert k != cache_key("search", "cats", **{**base, "model": "gpt-4o"})
    assert k != cache_key("search", "cats", **{**base, "prompt_version": "v2"})
    assert k != cache_key("search", "cats", **{**base, "params": {"max_tokens": 900}})
    assert k != cache_key("page", "cats", **base)


def test_memory_cache_ttl():
    now = [0.0]
    cache = MemoryCache(clock=lambda: now[0])
    cache.set("k", "v", ttl_s=10)
    assert cache.get("k") == "v"
    now[0] = 10
    assert cache.get("k") is None


def test_mongo_cache_ignores_expired_entries():
    col = mongomock.MongoClient().db.llm_cache
    cache = MongoCache(col)
    cache.set("fresh", [1], ttl_s=60)
    cache.set("forever", [2])
    col.insert_one(
        {
            "_id": "old",
            "value": [3],
            "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
    )
    assert cache.get("fresh") == [1]
    assert cache.get("forever") == [2]
    assert cache.get("old") is None


# --- coalescing ---------------------------------------------------------------------


def test_single_flight_runs_once_for_concurrent_same_key():
    flight, calls, barrier = SingleFlight(), [], threading.Barrier(10)

    def slow():
        calls.append(1)
        time.sleep(0.05)
        return "value"

    results = []

    def worker():
        barrier.wait()
        results.append(flight.do("k", slow))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert sorted(shared for _, shared in results) == [False] + [True] * 9


def test_single_flight_shares_errors_and_then_allows_retry():
    flight = SingleFlight()
    with pytest.raises(RuntimeError):
        flight.do("k", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert flight.do("k", lambda: 1) == (1, False)


# --- generator: cache correctness end to end ----------------------------------------


def make_generator(outcomes, **kw):
    provider = ScriptedProvider(outcomes)
    gen = Generator(make_client(provider), MemoryCache(), **kw)
    return gen, provider


def test_repeated_search_hits_cache():
    gen, provider = make_generator([good_results("q")])
    assert gen.search("Cats")[1]["cache"] == "miss"
    assert gen.search("  cats ")[1]["cache"] == "hit"
    assert provider.calls == 1


def test_failures_are_not_cached():
    gen, provider = make_generator([ProviderError("auth"), good_results("q")])
    with pytest.raises(LLMError):
        gen.search("cats")
    results, meta = gen.search("cats")
    assert meta["cache"] == "miss" and len(results) == 5 and provider.calls == 2


def test_prompt_version_bump_invalidates_cache():
    provider = ScriptedProvider([good_results("q"), good_results("q")])
    cache = MemoryCache()
    Generator(make_client(provider), cache, prompt_version="v1").search("cats")
    _, meta = Generator(make_client(provider), cache, prompt_version="v2").search(
        "cats"
    )
    assert meta["cache"] == "miss" and provider.calls == 2


def test_ten_concurrent_identical_searches_make_one_provider_call():
    class SlowProvider(ScriptedProvider):
        def complete(self, *a, **kw):
            time.sleep(0.05)
            return super().complete(*a, **kw)

    provider = SlowProvider([good_results("q")] * 10)
    gen = Generator(make_client(provider), MemoryCache())
    barrier, metas = threading.Barrier(10), []

    def worker():
        barrier.wait()
        metas.append(gen.search("same query")[1]["cache"])

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert provider.calls == 1
    assert metas.count("miss") == 1


# --- guardrails ----------------------------------------------------------------


def test_cost_uses_model_prices():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
    assert cost_usd("m", usage, {"m": (2.0, 10.0)}) == 7.0
    assert cost_usd("unknown", usage, {"m": (2.0, 10.0)}) == 0.0
    assert (
        cost_usd("gpt-4", usage, {}) == 0.0
    )  # empty table must not fall back to defaults


def test_client_records_cost_and_stops_at_budget():
    budget = MemoryBudget(daily_usd=0.001)
    prices = {"m": (10.0, 10.0)}  # 60 tokens per call -> $0.0006
    provider = ScriptedProvider([good_results("q")] * 3)
    client = make_client(provider, budget=budget, prices=prices)
    out = client.complete("p", model="m", max_tokens=10, validate=parse_search_results)
    assert out.cost_usd == pytest.approx(0.0006)
    client.complete("p", model="m", max_tokens=10)  # $0.0012 spent: over budget now
    with pytest.raises(LLMError) as e:
        client.complete("p", model="m", max_tokens=10)
    assert e.value.kind == "budget_exceeded" and provider.calls == 2


def test_invalid_output_still_costs_money():
    budget = MemoryBudget(daily_usd=100)
    client = make_client(
        ScriptedProvider(["junk", good_results("q")]),
        budget=budget,
        prices={"m": (10.0, 10.0)},
    )
    out = client.complete("p", model="m", max_tokens=10, validate=parse_search_results)
    assert out.cost_usd == pytest.approx(0.0012) and budget.spent() == pytest.approx(
        0.0012
    )


def test_budget_resets_each_day():
    day = ["2026-10-13"]
    budget = MemoryBudget(daily_usd=1, today=lambda: day[0])
    budget.add(1)
    assert not budget.allow()
    day[0] = "2026-10-14"
    assert budget.allow()


def test_mongo_budget_is_shared_between_instances():
    col = mongomock.MongoClient().db.guard_state
    a, b = MongoBudget(col, 1.0), MongoBudget(col, 1.0)
    a.add(0.6)
    b.add(0.6)
    assert not a.allow() and a.spent() == pytest.approx(1.2)


def test_rate_limiters_fixed_window():
    now = [120.0]
    for limiter in (
        MemoryRateLimiter(2, 60, clock=lambda: now[0]),
        MongoRateLimiter(mongomock.MongoClient().db.g, 2, 60, clock=lambda: now[0]),
    ):
        now[0] = 120.0
        assert [limiter.hit("ip")[0] for _ in range(3)] == [True, True, False]
        assert limiter.hit("other-ip")[0]
        now[0] = 180.0  # next window
        assert limiter.hit("ip")[0]


# --- fallback and circuit breaker -----------------------------------------------

from harness.cache import MongoLease
from harness.fallback import CircuitBreaker, FallbackProvider


def test_breaker_opens_after_threshold_then_half_opens():
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, cooldown_s=10, clock=lambda: now[0])
    breaker.record_failure()
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()
    now[0] = 10
    assert breaker.allow()  # one trial call
    assert not breaker.allow()  # no second concurrent trial
    breaker.record_success()
    assert breaker.state == "closed" and breaker.allow()


def test_fallback_serves_secondary_when_primary_is_down():
    primary = ScriptedProvider([ProviderError("unavailable")])
    secondary = ScriptedProvider([good_results("q")])
    provider = FallbackProvider(primary, secondary, "backup-model")
    out = make_client(provider).complete(
        "p", model="main", max_tokens=10, validate=parse_search_results
    )
    assert out.model == "backup-model" and out.attempts == 1


def test_fallback_does_not_hide_auth_errors():
    provider = FallbackProvider(
        ScriptedProvider([ProviderError("auth")]),
        ScriptedProvider([good_results("q")]),
        "backup",
    )
    with pytest.raises(LLMError) as e:
        make_client(provider).complete("p", model="main", max_tokens=10)
    assert e.value.kind == "auth"


def test_open_breaker_skips_primary():
    primary = ScriptedProvider([])
    breaker = CircuitBreaker(threshold=1)
    breaker.record_failure()
    provider = FallbackProvider(
        primary, ScriptedProvider([good_results("q")]), "b", breaker
    )
    provider.complete("p", model="main", max_tokens=10, timeout=1)
    assert primary.calls == 0


def test_fallback_answers_are_served_but_not_cached():
    provider = FallbackProvider(
        ScriptedProvider([ProviderError("timeout"), good_results("q")]),
        ScriptedProvider([good_results("q")]),
        "backup",
    )
    gen = Generator(make_client(provider), MemoryCache(), search_model="main")
    _, first = gen.search("cats")
    _, second = gen.search("cats")
    assert first["model"] == "backup" and second["cache"] == "miss"
    assert second["model"] == "main"
    assert gen.search("cats")[1]["cache"] == "hit"


# --- cross-process coalescing --------------------------------------------------------


def test_lease_is_exclusive_and_expires():
    col = mongomock.MongoClient().db.inflight
    a, b = MongoLease(col, ttl_s=60), MongoLease(col, ttl_s=60)
    assert a.acquire("k") and not b.acquire("k")
    a.release("k")
    assert b.acquire("k")
    col.update_one({"_id": "k"}, {"$set": {"expires_at": datetime.now(timezone.utc)}})
    assert a.acquire("k")  # expired lease is taken over


def test_two_workers_share_one_generation():
    db = mongomock.MongoClient().db
    cache = MongoCache(db.llm_cache)

    class SlowProvider(ScriptedProvider):
        def complete(self, *a, **kw):
            time.sleep(0.2)
            return super().complete(*a, **kw)

    provider = SlowProvider([good_results("q")] * 2)
    # Two Generators = two worker processes: separate in-process SingleFlight.
    workers = [
        Generator(
            make_client(provider), cache, lease=MongoLease(db.inflight, poll_s=0.01)
        )
        for _ in range(2)
    ]
    results = []
    t1 = threading.Thread(
        target=lambda: results.append(workers[0].search("q")[1]["cache"])
    )
    t1.start()
    time.sleep(0.05)  # worker 0 holds the lease
    results.append(workers[1].search("q")[1]["cache"])
    t1.join()
    assert provider.calls == 1
    assert sorted(results) == ["miss", "peer"]


# --- prompt evaluation script (offline, stub SDK) --------------------------------


def test_prompt_eval_scores_strict_and_repaired_output():
    from types import SimpleNamespace
    from bench import prompt_eval

    replies = iter(
        [
            good_results("q"),  # strict JSON
            "```json\n" + good_results("q") + "\n```",  # valid only after repair
            "Sure! Here you go.",  # invalid
        ]
    )

    class StubSDK:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=300)
                    msg = SimpleNamespace(content=next(replies))
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                        usage=usage,
                    )

    rows = prompt_eval.run_pair(
        StubSDK, "v1-original", "m", ["a", "b", "c"], 500, {"m": (1.0, 1.0)}
    )
    s = prompt_eval.summarize(rows)
    assert [r["strict_parse"] for r in rows] == [True, False, False]
    assert [r["valid"] for r in rows] == [True, True, False]
    assert s["cost_per_1k_searches_usd"] == pytest.approx(0.4)


def test_prompt_eval_json_mode_unwraps_results():
    from bench import prompt_eval

    wrapped = '{"results": ' + good_results("q") + "}"
    assert prompt_eval.strict_parse(wrapped, "v3-json-mode")
    assert len(parse_search_results(prompt_eval.unwrap(wrapped, "v3-json-mode"))) == 5
