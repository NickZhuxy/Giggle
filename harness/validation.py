"""Turn raw model text into trusted shapes, or raise ``ValueError``.

Repairing JSON is not enough: a parsed array can still hold wrong types,
empty fields, or half an object cut off by the token limit.
"""

import json
import re

SEARCH_FIELDS = ("title", "snippet", "url")
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text.strip()).strip()


def _array_items(text: str):
    """Yield every complete JSON value inside the first ``[ ... ]``.

    Stops quietly at the first incomplete element, so a truncated response
    still yields the elements that arrived whole.
    """
    start = text.find("[")
    if start == -1:
        raise ValueError("no JSON array found")
    decoder = json.JSONDecoder()
    i = start + 1
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if i >= len(text) or text[i] == "]":
            return
        try:
            item, i = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            return
        yield item


def _valid_result(item) -> bool:
    if not isinstance(item, dict):
        return False
    for key in SEARCH_FIELDS:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    return item["url"].startswith(("http://", "https://"))


def parse_search_results(text: str, limit: int = 5, min_results: int = 3) -> list:
    """Return up to ``limit`` valid ``{title, snippet, url}`` results.

    Fewer than ``min_results`` valid items (for example, a response cut off
    early by the token limit) counts as unusable, so the client retries.
    """
    items = [
        {k: item[k].strip() for k in SEARCH_FIELDS}
        for item in _array_items(_strip_fences(text))
        if _valid_result(item)
    ]
    if len(items) < min_results:
        raise ValueError(
            f"{len(items)} valid search results in model output; need {min_results}"
        )
    return items[:limit]


def parse_page_html(text: str) -> str:
    """Return the page HTML if it has a title and at least one paragraph."""
    html = _strip_fences(text)
    lowered = html.lower()
    if "<h1" not in lowered or "<p" not in lowered:
        raise ValueError("page HTML is missing <h1> or <p>")
    if "</p>" not in lowered:
        raise ValueError("page HTML looks truncated")
    return html
