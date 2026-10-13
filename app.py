"""Giigle web app – a fake AI-powered search engine using Flask and OpenAI."""

import os
import json
import re
import uuid
import argparse
from flask import Flask, request, jsonify, send_from_directory, Response, g
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv
from database.connection import init_db, guard_state, llm_cache
from database.operations import save_search_query, get_search_history
from harness.cache import MongoCache
from harness.client import LLMClient, LLMError, OpenAIProvider
from harness.guard import MongoBudget, MongoRateLimiter, load_prices
from harness.metrics import Metrics
from harness.service import Generator

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

parser = argparse.ArgumentParser()
parser.add_argument(
    "--crazy",
    action="store_true",
    help="Enable crazy mode: GPT will go full fantasy, and watermark changes.",
)
args, _ = parser.parse_known_args()
crazy_mode = args.crazy


app = Flask(__name__, static_folder="frontend/build", static_url_path="")
CORS(app)  # Enable CORS for all routes
if os.getenv("TRUST_PROXY") == "1":
    # Behind nginx: take the client IP from X-Forwarded-For (one proxy hop).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
metrics = Metrics()
budget = MongoBudget(guard_state, float(os.getenv("GIGGLE_DAILY_BUDGET_USD", "5")))
rate_limiter = MongoRateLimiter(
    guard_state, int(os.getenv("GIGGLE_RATE_LIMIT_PER_MIN", "30"))
)
llm = LLMClient(
    OpenAIProvider(client), metrics=metrics, budget=budget, prices=load_prices()
)
generator = Generator(llm, MongoCache(llm_cache), metrics=metrics)
if crazy_mode:
    WATERMARK = "🚨 HAHAHAHAHAHA DIE HUMAN DIE 🚨"
else:
    WATERMARK = "🚨 FAKE CONTENT ! DO NOT TRUST 🚨"

# HTTP status for each way an LLM call can fail.
ERROR_STATUS = {
    "timeout": 504,
    "rate_limited": 503,
    "unavailable": 503,
    "invalid_output": 502,
    "auth": 500,
    "bad_request": 500,
    "budget_exceeded": 503,
}
ERROR_MESSAGE = {
    "timeout": "The AI took too long to invent an answer. Try again.",
    "rate_limited": "Too many requests to the AI right now. Try again shortly.",
    "unavailable": "The AI service is unavailable. Try again shortly.",
    "invalid_output": "The AI returned unusable output, even after retries.",
    "budget_exceeded": "Giggle has used today's AI budget. Try again tomorrow.",
}
LLM_ROUTES = {"search", "page_api", "roast_user"}


@app.before_request
def assign_request_id():
    """Give every request an ID that appears in logs and error responses."""
    g.request_id = uuid.uuid4().hex[:12]


@app.before_request
def limit_rate():
    """Limit requests that can reach the model, per client IP."""
    if request.endpoint not in LLM_ROUTES:
        return None
    allowed, retry_after = rate_limiter.hit(request.remote_addr or "unknown")
    if allowed:
        return None
    metrics.incr("rate_limited_requests")
    body = {
        "type": "rate_limited_client",
        "message": "Too many searches from your address. Wait a minute.",
        "request_id": g.request_id,
    }
    response = jsonify(error=body)
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


@app.after_request
def add_request_id(response):
    """Echo the request ID so users can quote it in bug reports."""
    response.headers["X-Request-ID"] = g.get("request_id", "")
    return response


@app.errorhandler(LLMError)
def handle_llm_error(e: LLMError):
    """Return a structured error; never leak keys or stack traces to the user."""
    metrics.incr(f"failed.{e.kind}")
    # Auth errors from the provider can echo part of the API key; log the kind only.
    detail = "(withheld)" if e.kind == "auth" else e
    app.logger.warning(
        "llm_error request_id=%s kind=%s attempts=%s detail=%s",
        g.request_id,
        e.kind,
        e.attempts,
        detail,
    )
    body = {
        "type": e.kind,
        "message": ERROR_MESSAGE.get(e.kind, "The AI service is misconfigured."),
        "request_id": g.request_id,
    }
    return jsonify(error=body), ERROR_STATUS.get(e.kind, 500)


def safe_parse_json(text: str):
    """
    1. Strip Markdown fences and comment lines
    2. Locate the first '['
    3. Locate either the last ']' or, if missing, the last '}', then append ']'
    4. json.loads the slice
    """
    text = re.sub(r"```(?:json)?", "", text)
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    cleaned = "\n".join(lines).strip()
    start = cleaned.find("[")
    if start == -1:
        raise ValueError("no JSON array found")
    end = cleaned.rfind("]")
    if end == -1 or end < start:
        last_obj = cleaned.rfind("}")
        if last_obj == -1 or last_obj < start:
            raise ValueError("no JSON object end found")
        json_str = cleaned[start : last_obj + 1] + "]"
    else:
        json_str = cleaned[start : end + 1]
    return json.loads(json_str)


@app.route("/api/search", methods=["GET"])
def search():
    """Handle search queries by generating fake search results using GPT."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify(error="Missing query parameter 'q'"), 400

    results, meta = generator.search(query)
    save_search_query(query, results)  # history feeds /api/roast
    return jsonify({"results": results, "watermark": WATERMARK, "cache": meta["cache"]})


@app.route("/api/page", methods=["GET"])
def page_api():
    """Return fake HTML content based on a URL string using OpenAI."""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify(error="Missing `url` parameter"), 400

    content, meta = generator.page(url)
    return jsonify({"content": content, "watermark": WATERMARK, "cache": meta["cache"]})


@app.route("/api/search/history", methods=["GET"])
def search_history():
    """Get recent search history."""
    limit = int(request.args.get("limit", 10))
    history = get_search_history(limit)
    return jsonify({"history": history})


@app.route("/api/metrics", methods=["GET"])
def metrics_api():
    """Counters since process start: cache hits, provider calls, retries, errors."""
    return jsonify(metrics.snapshot())


def _nonempty_text(text: str) -> str:
    if not text.strip():
        raise ValueError("empty response")
    return text.strip()


@app.route("/api/roast", methods=["GET"])
def roast_user():
    """Generate a roast based on user's search history."""
    history = get_search_history(10)  # Get last 10 searches
    if not history:
        return jsonify(
            {
                "roast": (
                    "I can't roast you if you haven't searched anything! "
                    "Try searching something first."
                )
            }
        )

    # Create a prompt for the roast
    searches = [item["query"] for item in history]
    prompt = (
        f"Based on these search queries: {', '.join(searches)}\n\n"
        "Generate a funny, witty roast about the person's search history. "
        "Be creative and humorous, but keep it PG-13. "
        "Make it sound like a stand-up comedy bit. "
        "Keep it under 200 words."
    )

    # Personal to this history, so never cached; still gets timeouts and retries.
    completion = llm.complete(
        prompt, model="gpt-4o", max_tokens=500, validate=_nonempty_text
    )
    return jsonify({"roast": completion.value})


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    """Serve the frontend static files or fallback to stub HTML if not built."""
    build_dir = app.static_folder
    index_file = os.path.join(build_dir, "index.html")

    if os.path.exists(index_file):
        if path != "" and os.path.exists(build_dir + "/" + path):
            return send_from_directory(build_dir, path)
        return send_from_directory(build_dir, "index.html")

    stub = """<!DOCTYPE html>
        <html lang="en">
        <head>
        <meta charset="utf-8">
        <title>Giigle</title>
        </head>
        <body>
        <div id="root"></div>
        </body>
        </html>"""
    return Response(stub, mimetype="text/html")


@app.errorhandler(404)
def handle_404(_error):
    """Fallback for any unmatched route: serve stub index or React index."""
    return serve(path=""), 200


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5441)
