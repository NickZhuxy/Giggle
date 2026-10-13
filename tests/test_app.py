import pytest
import json
from app import app, safe_parse_json, WATERMARK
import app as giigle_app

@pytest.fixture
def client():
    giigle_app.app.config['TESTING'] = True
    return giigle_app.app.test_client()

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

def test_search_cached(client, monkeypatch):
    sample = [{"title":"T","snippet":"S","url":"U"}]
    monkeypatch.setattr(giigle_app, 'get_recent_search_results', lambda q: sample)
    res = client.get('/api/search?q=test')
    assert res.status_code == 200
    data = res.get_json()
    assert data["results"] == sample
    assert data["watermark"] == WATERMARK

class DummySearchResponse:
    def __init__(self, content):
        self.choices = [type('obj', (), {'message': type('msg', (), {'content': content})})]

def test_search_new_results_success(client, monkeypatch):
    monkeypatch.setattr(giigle_app, 'get_recent_search_results', lambda q: [])
    monkeypatch.setattr(giigle_app, 'save_search_query', lambda q,r: None)
    dummy = DummySearchResponse('[{"title":"T","snippet":"S","url":"U"}]')
    monkeypatch.setattr(giigle_app.client.chat.completions, 'create', lambda **kwargs: dummy)
    res = client.get('/api/search?q=hello')
    assert res.status_code == 200
    data = res.get_json()
    assert data["results"] == [{"title":"T","snippet":"S","url":"U"}]
    assert data["watermark"] == WATERMARK

def test_search_parse_error(client, monkeypatch):
    monkeypatch.setattr(giigle_app, 'get_recent_search_results', lambda q: [])
    monkeypatch.setattr(giigle_app, 'save_search_query', lambda q,r: None)
    dummy = DummySearchResponse('invalid')
    monkeypatch.setattr(giigle_app.client.chat.completions, 'create', lambda **kwargs: dummy)
    res = client.get('/api/search?q=hello')
    assert res.status_code == 200
    data = res.get_json()
    assert data["results"][0]["title"] == "Parse Error"
    assert data["results"][0]["snippet"] == "no JSON array found"
    assert data["results"][0]["url"] == "#"
    assert data["watermark"] == WATERMARK

def test_page_missing_url(client):
    res = client.get('/api/page')
    assert res.status_code == 400
    data = res.get_json()
    assert "Missing `url`" in data["error"]

def test_page_cached(client, monkeypatch):
    html = "<h1>Hi</h1>"
    monkeypatch.setattr(giigle_app, 'get_generated_page', lambda url: html)
    res = client.get('/api/page?url=http://example.com')
    assert res.status_code == 200
    data = res.get_json()
    assert data["content"] == html
    assert data["watermark"] == WATERMARK

class DummyPageResponse:
    def __init__(self, content):
        self.choices = [type('obj', (), {'message': type('msg', (), {'content': content})})]

def test_page_new(client, monkeypatch):
    monkeypatch.setattr(giigle_app, 'get_generated_page', lambda url: None)
    monkeypatch.setattr(giigle_app, 'save_generated_page', lambda u,c: None)
    dummy = DummyPageResponse('<h1>Title</h1><p>Paragraph</p>')
    monkeypatch.setattr(giigle_app.client.chat.completions, 'create', lambda **kwargs: dummy)
    res = client.get('/api/page?url=http://example.com')
    assert res.status_code == 200
    data = res.get_json()
    assert data["content"] == '<h1>Title</h1><p>Paragraph</p>'
    assert data["watermark"] == WATERMARK

def test_search_history(client, monkeypatch):
    sample_history = [{"query": "test"}]

    monkeypatch.setattr(giigle_app, 'get_search_history', lambda limit: sample_history)

    res = client.get('/api/search/history')
    assert res.status_code == 200
    data = res.get_json()
    assert data["history"] == sample_history

def test_roast_user_with_history(client, monkeypatch):
    sample_history = [{"query": "cats"}, {"query": "dogs"}]
    dummy_roast_response = "<funny roast>"

    monkeypatch.setattr(giigle_app, 'get_search_history', lambda limit: sample_history)
    monkeypatch.setattr(giigle_app.client.chat.completions, 'create', lambda **kwargs: type('obj', (), {'choices': [type('msg', (), {'message': type('msg', (), {'content': dummy_roast_response})})]})())

    res = client.get('/api/roast')
    assert res.status_code == 200
    data = res.get_json()
    assert data["roast"] == dummy_roast_response

def test_roast_user_no_history(client, monkeypatch):
    monkeypatch.setattr(giigle_app, 'get_search_history', lambda limit: [])

    res = client.get('/api/roast')
    assert res.status_code == 200
    data = res.get_json()
    assert "can't roast" in data["roast"].lower()
