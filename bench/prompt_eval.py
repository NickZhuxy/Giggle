"""Compare search-prompt versions and models on the real OpenAI API.

    python -m bench.prompt_eval --estimate          # cost estimate only, no calls
    python -m bench.prompt_eval --yes --out bench/results/prompt-eval-<date>

For each (prompt version, model) pair, every query in bench/queries.txt is
sent once. Each response is scored two ways:

  strict parse   json.loads succeeds on the raw text (no repair)
  valid          the harness validator accepts it (repair + schema checks)

plus latency and token usage. Costs use harness.guard prices; verify them on
the provider's pricing page before the run and pass --prices to override.
This script spends real money: it refuses to call the API without --yes.
"""

import argparse
import json
import os
import statistics
import time
from pathlib import Path

from harness.guard import DEFAULT_PRICES, cost_usd
from harness.validation import parse_search_results

QUERIES = Path(__file__).with_name("queries.txt")

SCHEMA_HINT = (
    "Each element has keys title (string), snippet (string), url (string, "
    "starting with https://)."
)

# Each version changes one thing relative to the previous one.
PROMPTS = {
    # v1: the original course-project prompt, unchanged.
    "v1-original": {
        "system": None,
        "user": (
            "Generate *exactly* 5 results "
            'and output *only* the JSON array (no extra text). "{query}"\n\n'
            "Respond with *only* a JSON array (no Markdown fences, no comments),\n"
            "where each element has keys: "
            "title (string), snippet (string), url (string). Under 500 tokens"
        ),
        "json_mode": False,
    },
    # v2: move format rules into a system message; the user turn holds only data.
    "v2-system-schema": {
        "system": (
            "You are the result generator for Giggle, a fictional search engine. "
            "Reply with a JSON array of exactly 5 invented search results and "
            "nothing else: no prose, no Markdown fences. " + SCHEMA_HINT
        ),
        "user": "Search query: {query}",
        "json_mode": False,
    },
    # v3: as v2, plus the API's JSON mode (needs a top-level object).
    "v3-json-mode": {
        "system": (
            "You are the result generator for Giggle, a fictional search engine. "
            'Reply with a JSON object {{"results": [...]}} holding exactly 5 '
            "invented search results. " + SCHEMA_HINT
        ),
        "user": "Search query: {query}",
        "json_mode": True,
    },
}


def messages(version, query):
    spec = PROMPTS[version]
    msgs = []
    if spec["system"]:
        msgs.append({"role": "system", "content": spec["system"].format(query=query)})
    msgs.append({"role": "user", "content": spec["user"].format(query=query)})
    return msgs


def unwrap(text, version):
    """JSON mode returns {"results": [...]}; hand the validator the array."""
    if PROMPTS[version]["json_mode"]:
        try:
            return json.dumps(json.loads(text)["results"])
        except (ValueError, KeyError, TypeError):
            return text
    return text


def strict_parse(text, version):
    try:
        value = json.loads(text)
        if PROMPTS[version]["json_mode"]:
            value = value["results"]
        return isinstance(value, list)
    except (ValueError, KeyError, TypeError):
        return False


def estimate(queries, versions, models, prices):
    # Rough per-call usage seen in this app: ~120 input and ~350 output tokens.
    per_call = {"prompt_tokens": 120, "completion_tokens": 350}
    total = 0.0
    for model in models:
        total += cost_usd(model, per_call, prices) * len(queries) * len(versions)
    return total


def run_pair(sdk, version, model, queries, max_tokens, prices):
    rows = []
    for query in queries:
        kwargs = {
            "model": model,
            "messages": messages(version, query),
            "max_tokens": max_tokens,
        }
        if PROMPTS[version]["json_mode"]:
            kwargs["response_format"] = {"type": "json_object"}
        start = time.perf_counter()
        try:
            resp = sdk.chat.completions.create(**kwargs)
        except Exception as e:  # pylint: disable=broad-except
            rows.append({"query": query, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        latency = time.perf_counter() - start
        text = resp.choices[0].message.content or ""
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
        }
        try:
            parse_search_results(unwrap(text, version))
            valid = True
        except ValueError:
            valid = False
        rows.append(
            {
                "query": query,
                "strict_parse": strict_parse(text, version),
                "valid": valid,
                "latency_s": latency,
                "usage": usage,
                "cost_usd": cost_usd(model, usage, prices),
                "finish_reason": resp.choices[0].finish_reason,
                "raw": text,
            }
        )
    return rows


def summarize(rows):
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"n": 0, "errors": len(rows)}
    lat = sorted(r["latency_s"] for r in ok)
    return {
        "n": len(ok),
        "errors": len(rows) - len(ok),
        "strict_parse_rate": sum(r["strict_parse"] for r in ok) / len(ok),
        "valid_rate": sum(r["valid"] for r in ok) / len(ok),
        "p50_latency_s": statistics.median(lat),
        "p95_latency_s": lat[min(len(lat) - 1, int(0.95 * len(lat)))],
        "mean_output_tokens": statistics.mean(
            r["usage"]["completion_tokens"] for r in ok
        ),
        "cost_per_1k_searches_usd": 1000 * statistics.mean(r["cost_usd"] for r in ok),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models", nargs="+", default=["gpt-4o-mini", "gpt-4o", "gpt-4"]
    )
    parser.add_argument("--versions", nargs="+", default=list(PROMPTS))
    parser.add_argument("--max-tokens", type=int, default=500)
    parser.add_argument("--prices", help='JSON override, e.g. {"gpt-4o": [2.5, 10]}')
    parser.add_argument(
        "--estimate", action="store_true", help="print the cost estimate and exit"
    )
    parser.add_argument(
        "--yes", action="store_true", help="confirm that real API calls may be made"
    )
    parser.add_argument("--out", default="bench/results/prompt-eval")
    args = parser.parse_args()

    prices = dict(DEFAULT_PRICES)
    if args.prices:
        prices.update({k: tuple(v) for k, v in json.loads(args.prices).items()})
    queries = [
        q.strip()
        for q in QUERIES.read_text().splitlines()
        if q.strip() and not q.startswith("#")
    ]
    est = estimate(queries, args.versions, args.models, prices)
    print(
        f"{len(queries)} queries x {len(args.versions)} versions x {len(args.models)} models "
        f"= {len(queries) * len(args.versions) * len(args.models)} calls, estimated ${est:.2f}"
    )
    if args.estimate or not args.yes:
        if not args.yes and not args.estimate:
            print("Refusing to call the API without --yes.")
        return

    from openai import OpenAI  # pylint: disable=import-outside-toplevel

    sdk = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for version in args.versions:
        for model in args.models:
            rows = run_pair(sdk, version, model, queries, args.max_tokens, prices)
            results[f"{version}|{model}"] = {"summary": summarize(rows), "rows": rows}
            print(version, model, results[f"{version}|{model}"]["summary"])

    (out / "results.json").write_text(
        json.dumps(
            {
                "args": vars(args),
                "prices": prices,
                "queries": queries,
                "results": results,
            },
            indent=2,
        )
    )
    lines = [
        "| Prompt | Model | Strict JSON | Valid after harness | p50 s | p95 s | $ per 1k searches |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, value in results.items():
        version, model = key.split("|")
        s = value["summary"]
        if not s.get("n"):
            lines.append(f"| {version} | {model} | error | error | | | |")
            continue
        lines.append(
            f"| {version} | {model} | {s['strict_parse_rate']:.0%} | {s['valid_rate']:.0%} | "
            f"{s['p50_latency_s']:.1f} | {s['p95_latency_s']:.1f} | {s['cost_per_1k_searches_usd']:.2f} |"
        )
    lines += [
        "",
        f"n = {len(queries)} queries per row, one call each. Prices: {json.dumps(prices)}",
    ]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
