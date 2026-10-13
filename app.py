"""Giigle web app – a fake AI-powered search engine using Flask and OpenAI."""

import os
import json
import re
import argparse
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv
from database.operations import (
    save_search_query,
    get_recent_search_results,
    save_generated_page,
    get_generated_page,
    get_search_history,
)

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
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
if crazy_mode:
    WATERMARK = "🚨 HAHAHAHAHAHA DIE HUMAN DIE 🚨"
else:
    WATERMARK = "🚨 FAKE CONTENT ! DO NOT TRUST 🚨"


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

    # Check cache first
    cached_results = get_recent_search_results(query)
    if cached_results:
        return jsonify(
            {
                "results": cached_results,
                "watermark": WATERMARK,
            }
        )

    # Generate new results if not in cache
    prompt = (
        f"Generate *exactly* 5 results "
        f'and output *only* the JSON array (no extra text). "{query}"\n\n'
        "Respond with *only* a JSON array (no Markdown fences, no comments),\n"
        "where each element has keys: "
        "title (string), snippet (string), url (string). Under 500 tokens"
    )

    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
    )
    raw = response.choices[0].message.content
    try:
        results = safe_parse_json(raw)
        # Save to database
        save_search_query(query, results)
    except ValueError as e:
        results = [{"title": "Parse Error", "snippet": str(e), "url": "#"}]

    return jsonify(
        {
            "results": results,
            "watermark": WATERMARK,
        }
    )


@app.route("/api/page", methods=["GET"])
def page_api():
    """Return fake HTML content based on a URL string using OpenAI."""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify(error="Missing `url` parameter"), 400

    # Check if page already exists
    existing_content = get_generated_page(url)
    if existing_content:
        return jsonify({"content": existing_content, "watermark": WATERMARK})

    prompt = (
        f"Generate a complete HTML page for this URL: {url}\n"
        "- Use <h1> for the title\n"
        "- Wrap each paragraph in <p>\n"
        "- Do NOT output any Markdown fences or code blocks, only raw HTML\n"
    )

    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
    )
    page_content = response.choices[0].message.content.strip()

    # Save the generated page to database
    save_generated_page(url, page_content)

    return jsonify({"content": page_content, "watermark": WATERMARK})


@app.route("/api/search/history", methods=["GET"])
def search_history():
    """Get recent search history."""
    limit = int(request.args.get("limit", 10))
    history = get_search_history(limit)
    return jsonify({"history": history})


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

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
    )

    roast = response.choices[0].message.content.strip()
    return jsonify({"roast": roast})


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
    app.run(debug=True, host="0.0.0.0", port=5441)
