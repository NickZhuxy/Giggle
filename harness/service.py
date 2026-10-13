"""Search and page generation on top of the client, cache, and coalescing."""

from .cache import SingleFlight, cache_key, normalize_query
from .validation import parse_page_html, parse_search_results

# Bump when a prompt or validator changes; old cache entries stop matching.
PROMPT_VERSION = "search-v2/page-v2"

SEARCH_PROMPT = (
    "Generate *exactly* 5 results "
    'and output *only* the JSON array (no extra text). "{query}"\n\n'
    "Respond with *only* a JSON array (no Markdown fences, no comments),\n"
    "where each element has keys: "
    "title (string), snippet (string), url (string). Under 500 tokens"
)

PAGE_PROMPT = (
    "Generate a complete HTML page for this URL: {url}\n"
    "- Use <h1> for the title\n"
    "- Wrap each paragraph in <p>\n"
    "- Do NOT output any Markdown fences or code blocks, only raw HTML\n"
)


class Generator:
    def __init__(
        self,
        client,
        cache,
        *,
        search_model="gpt-4",
        page_model="gpt-4",
        search_ttl_s=30 * 60,
        page_ttl_s=None,
        prompt_version=PROMPT_VERSION,
        coalesce=True,
        lease=None,
        metrics=None,
    ):
        self.client = client
        self.cache = cache
        self.search_model = search_model
        self.page_model = page_model
        self.search_ttl_s = search_ttl_s
        self.page_ttl_s = page_ttl_s
        self.prompt_version = prompt_version
        self.flight = SingleFlight() if coalesce else None
        self.lease = lease  # cross-process coalescing (MongoLease), optional
        self.metrics = metrics

    def _count(self, name):
        if self.metrics is not None:
            self.metrics.incr(name)

    def search(self, query: str):
        """Return ``(results, meta)`` for a query."""
        norm = normalize_query(query)
        params = {"max_tokens": 500, "n": 5}
        key = cache_key(
            "search",
            norm,
            model=self.search_model,
            prompt_version=self.prompt_version,
            params=params,
        )
        return self._cached(
            key,
            self.search_ttl_s,
            lambda: self.client.complete(
                SEARCH_PROMPT.format(query=norm),
                model=self.search_model,
                max_tokens=params["max_tokens"],
                validate=parse_search_results,
            ),
            kind="search",
            model=self.search_model,
        )

    def page(self, url: str):
        """Return ``(html, meta)`` for a URL. Pages stay until the prompt version changes."""
        url = url.strip()
        params = {"max_tokens": 1000}
        key = cache_key(
            "page",
            url,
            model=self.page_model,
            prompt_version=self.prompt_version,
            params=params,
        )
        return self._cached(
            key,
            self.page_ttl_s,
            lambda: self.client.complete(
                PAGE_PROMPT.format(url=url),
                model=self.page_model,
                max_tokens=params["max_tokens"],
                validate=parse_page_html,
            ),
            kind="page",
            model=self.page_model,
        )

    def _cached(self, key, ttl_s, generate, **meta):
        value = self.cache.get(key)
        if value is not None:
            self._count("cache_hit")
            return value, {"cache": "hit", "attempts": 0, "model": meta["model"]}
        self._count("cache_miss")

        def produce():
            # Another leader may have filled the cache since our first check.
            value = self.cache.get(key)
            if value is not None:
                return value, {"cache": "hit", "attempts": 0, "model": meta["model"]}
            leased = self.lease is None or self.lease.acquire(key)
            if not leased:
                # Another process is generating this key: wait for its result.
                value = self.lease.wait_for(key, self.cache.get)
                if value is not None:
                    self._count("coalesced_cross_process")
                    return value, {
                        "cache": "peer",
                        "attempts": 0,
                        "model": meta["model"],
                    }
                leased = self.lease.acquire(key)  # peer failed or lease expired
            try:
                completion = generate()  # raises LLMError; failures are never cached
                served_by = completion.model or meta["model"]
                if served_by == meta["model"]:
                    self.cache.set(
                        key,
                        completion.value,
                        ttl_s=ttl_s,
                        prompt_version=self.prompt_version,
                        **meta,
                    )
                else:
                    # A fallback model's answer is served but not cached, so the
                    # primary model's answer replaces it once the primary recovers.
                    self._count("fallback_served_uncached")
            finally:
                if self.lease is not None and leased:
                    self.lease.release(key)
            return completion.value, {
                "cache": "miss",
                "attempts": completion.attempts,
                "model": served_by,
            }

        if self.flight is None:
            return produce()
        (value, info), shared = self.flight.do(key, produce)
        if shared:
            self._count("coalesced")
            return value, {**info, "cache": "coalesced", "attempts": 0}
        return value, info
