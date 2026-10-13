import pytest
from app import safe_parse_json, WATERMARK
import app as giigle_app
from harness.cache import MemoryCache
from harness.client import LLMClient, ProviderError
from harness.fake import ScriptedProvider, good_results
from harness.guard import MemoryBudget, MemoryRateLimiter
from harness.metrics import Metrics
from harness.service import Generator


@pytest.fixture
def client(monkeypatch):
    giigle_app.app.config['TESTING'] = True
    monkeypatch.setattr(giigle_app, 'rate_limiter', MemoryRateLimiter(limit=1000))
    return giigle_app.app.test_client()


@pytest.fixture
def fake_llm(monkeypatch):
    """Swap the OpenAI-backed client for a scripted one; returns a loader."""
    monkeypatch.setattr(giigle_app, 'save_search_query', lambda q, r: None)

    def load(*outcomes, budget=None):
        provider = ScriptedProvider(outcomes)
        metrics = Metrics()
        llm = LLMClient(provider, metrics=metrics, budget=budget, sleep=lambda s: None)
        monkeypatch.setattr(giigle_app, 'metrics', metrics)
        monkeypatch.setattr(giigle_app, 'llm', llm)
        monkeypatch.setattr(giigle_app, 'generator', Generator(llm, MemoryCache(), metrics=metrics))
        return provider
    return load


def test_safe_parse_json_valid():
    raw = '```json\n[{"x":1,"y":2}]\n```'
    result = safe_parse_json(raw)
    assert result == [{"x":1,"y":2}]

def test_safe_parse_json_no_array():
    with pytest.raises(ValueError):
        safe_parse_json("no json here")

def test_search_missing_q(client):
    res = client.get('/api/search')
    assert res.status_code == 400
    data = res.get_json()
    assert "Missing query" in data["error"]

def test_search_new_results_then_cache_hit(client, fake_llm):
    provider = fake_llm(good_results("hello"))
    first = client.get('/api/search?q=hello').get_json()
    second = client.get('/api/search?q=Hello').get_json()
    assert len(first["results"]) == 5
    assert first["watermark"] == WATERMARK
    assert (first["cache"], second["cache"]) == ("miss", "hit")
    assert provider.calls == 1

def test_search_recovers_from_bad_output(client, fake_llm):
    provider = fake_llm('invalid', good_results("hello"))
    res = client.get('/api/search?q=hello')
    assert res.status_code == 200
    assert provider.calls == 2

def test_search_gives_structured_error_after_retries(client, fake_llm):
    fake_llm('invalid', 'invalid', 'invalid')
    res = client.get('/api/search?q=hello')
    assert res.status_code == 502
    err = res.get_json()["error"]
    assert err["type"] == "invalid_output"
    assert err["request_id"] == res.headers["X-Request-ID"]

def test_search_timeout_maps_to_504(client, fake_llm):
    fake_llm(*[ProviderError("timeout")] * 3)
    res = client.get('/api/search?q=hello')
    assert res.status_code == 504
    assert res.get_json()["error"]["type"] == "timeout"

def test_auth_error_does_not_leak_details(client, fake_llm):
    fake_llm(ProviderError("auth", "Incorrect API key provided: sk-secret"))
    res = client.get('/api/search?q=hello')
    assert res.status_code == 500
    assert "sk-secret" not in res.get_data(as_text=True)

def test_page_missing_url(client):
    res = client.get('/api/page')
    assert res.status_code == 400
    data = res.get_json()
    assert "Missing `url`" in data["error"]

def test_page_new_then_cached(client, fake_llm):
    provider = fake_llm('<h1>Title</h1><p>Paragraph</p>')
    first = client.get('/api/page?url=http://example.com').get_json()
    second = client.get('/api/page?url=http://example.com').get_json()
    assert first["content"] == '<h1>Title</h1><p>Paragraph</p>'
    assert first["watermark"] == WATERMARK
    assert second["cache"] == "hit" and provider.calls == 1

def test_metrics_endpoint_counts_calls(client, fake_llm):
    fake_llm(ProviderError("rate_limited"), good_results("x"))
    client.get('/api/search?q=x')
    client.get('/api/search?q=x')
    m = client.get('/api/metrics').get_json()
    assert m["provider_calls"] == 2
    assert m["retries"] == 1
    assert m["cache_miss"] == 1 and m["cache_hit"] == 1

def test_search_history(client, monkeypatch):
    sample_history = [{"query": "test"}]

    monkeypatch.setattr(giigle_app, 'get_search_history', lambda limit: sample_history)

    res = client.get('/api/search/history')
    assert res.status_code == 200
    data = res.get_json()
    assert data["history"] == sample_history

def test_roast_user_with_history(client, monkeypatch, fake_llm):
    sample_history = [{"query": "cats"}, {"query": "dogs"}]
    monkeypatch.setattr(giigle_app, 'get_search_history', lambda limit: sample_history)
    fake_llm("<funny roast>")

    res = client.get('/api/roast')
    assert res.status_code == 200
    data = res.get_json()
    assert data["roast"] == "<funny roast>"

def test_roast_user_no_history(client, monkeypatch):
    monkeypatch.setattr(giigle_app, 'get_search_history', lambda limit: [])

    res = client.get('/api/roast')
    assert res.status_code == 200
    data = res.get_json()
    assert "can't roast" in data["roast"].lower()

def test_rate_limit_returns_429_with_retry_after(client, monkeypatch, fake_llm):
    fake_llm(*[good_results("x")] * 5)
    monkeypatch.setattr(giigle_app, 'rate_limiter', MemoryRateLimiter(limit=2))
    codes = [client.get(f'/api/search?q=q{i}').status_code for i in range(3)]
    assert codes == [200, 200, 429]
    res = client.get('/api/search?q=again')
    assert int(res.headers["Retry-After"]) > 0
    assert res.get_json()["error"]["type"] == "rate_limited_client"

def test_rate_limit_ignores_non_llm_routes(client, monkeypatch):
    monkeypatch.setattr(giigle_app, 'rate_limiter', MemoryRateLimiter(limit=0))
    monkeypatch.setattr(giigle_app, 'get_search_history', lambda limit: [])
    assert client.get('/api/search/history').status_code == 200
    assert client.get('/api/metrics').status_code == 200

def test_budget_exhausted_returns_503(client, fake_llm):
    budget = MemoryBudget(daily_usd=0.0)
    provider = fake_llm(good_results("x"), budget=budget)
    res = client.get('/api/search?q=x')
    assert res.status_code == 503
    assert res.get_json()["error"]["type"] == "budget_exceeded"
    assert provider.calls == 0
