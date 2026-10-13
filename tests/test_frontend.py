# tests/test_frontend.py

import pytest
from app import app as flask_app

@pytest.fixture
def client():
    """Provide a Flask test client for the app."""
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as client:
        yield client

def test_root_route_serves_index(client):
    """GET / should return index.html with 200."""
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'<!DOCTYPE html>' in resp.data
    assert b'<div id="root"' in resp.data

def test_unknown_route_returns_index(client):
    """Unknown path should still return index.html (React Router)."""
    resp = client.get('/this/path/does/not/exist')
    assert resp.status_code == 200
    assert b'<!DOCTYPE html>' in resp.data

def test_page_route_with_query_returns_index(client):
    """GET /page?url=... should return index.html for client-side routing."""
    resp = client.get('/page?url=http://example.com')
    assert resp.status_code == 200
    assert b'<div id="root"' in resp.data

def test_favicon_route_returns_index_when_missing(client):
    """GET /favicon.ico (no actual file) should return index.html fallback."""
    resp = client.get('/favicon.ico')
    assert resp.status_code == 200
    assert resp.mimetype == 'text/html'
    assert b'<title>Giigle</title>' in resp.data

def test_index_contains_correct_title(client):
    """The served index.html should have the correct <title> tag."""
    resp = client.get('/')
    assert b'<title>Giigle</title>' in resp.data
