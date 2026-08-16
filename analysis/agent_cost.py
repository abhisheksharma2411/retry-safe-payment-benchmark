#!/usr/bin/env python3
"""Token and cost accounting for the agentic (Claude Code) condition.

Read-only over `results/`. Aggregates the 21 archived Claude Code sessions and
reconstructs each session's bill from its own recorded usage counters, so the
headline dollar figure is attributed to a specific rate card instead of being
asserted.

Why this script exists
----------------------
The agentic condition reports two numbers that look contradictory: roughly 200k
tokens of "agent context" per session, and about $6 for all 21 sessions
together. Three facts reconcile them, and the paper has to state all three:

  1. The token figure is CUMULATIVE ACROSS TURNS, not a context-window size.
     Each session takes ~13 turns and re-sends the conversation prefix every
     turn; the per-turn prompt is ~15k tokens, of which ~4k is the scaffold.
  2. Over 92% of those input tokens are CACHE READS, billed at one tenth of the
     base input rate. Priced as uncached input the same runs would cost ~4x.
  3. The sessions authenticated against a Claude subscription, not an API key
     (`ANTHROPIC_API_KEY` was unset — see the provenance note recorded in every
     agentic result). The dollar figure is therefore the CLI's own client-side
     API-equivalent estimate at list rates. No invoice was issued for it.

The rate card below is not taken on faith: `verify` recomputes every session's
cost from its raw counters and asserts agreement with the figure the CLI
reported, to within a rounding tolerance.

Usage:  python3 analysis/agent_cost.py
"""
import json
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDS = os.path.join(ROOT, "results", "model_results.json")
RAW = os.path.join(ROOT, "results", "raw")

# Anthropic list rates in USD per million tokens, for the model the CLI served
# (claude-opus-4-6[1m]). Cache write is 1.25x base and cache read is 0.10x base;
# both multipliers are confirmed exactly by `verify` against all 21 sessions.
RATE = {"input": 5.00, "cache_creation": 6.25, "cache_read": 0.50, "output": 25.00}
TOLERANCE_USD = 5e-6


def sessions():
    """One record per (family, sample) agentic session, in stable order."""
    by_key = {}
    for r in json.load(open(RECORDS, encoding="utf-8")):
        if r.get("condition") == "agentic":
            by_key[(r["family"], r["candidate"])] = r
    return sorted(by_key.values(), key=lambda r: (r["family"], r["sample"]))


def tool_calls(rec):
    """Tool-use count and mix for one session, from its archived transcript.

    Returns None when no transcript was archived, so the caller can report the
    tool means over the sessions that actually have one rather than silently
    averaging over a smaller denominator.
    """
    path = os.path.join(RAW, rec["run_id"], rec["family"], "agentic",
                        f"s{rec['sample']}", "agent_transcript.jsonl")
    if not os.path.exists(path):
        return None
    by_name = defaultdict(int)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    by_name[block.get("name")] += 1
    return dict(by_name)


def price(tokens):
    """Cost in USD for one session's token counters at the rate card above."""
    return sum(tokens[k] * rate for k, rate in RATE.items()) / 1e6


def verify(recs):
    """Assert the rate card reproduces every CLI-reported cost."""
    bad = [(r["family"], r["sample"], price(r["tokens"]), r["cost_usd"])
           for r in recs if abs(price(r["tokens"]) - r["cost_usd"]) > TOLERANCE_USD]
    if bad:
        raise SystemExit(
            "rate card does not reproduce the CLI-reported cost for "
            f"{len(bad)} session(s): {bad[:3]}"
        )
    return len(recs)


def main():
    recs = sessions()
    verify(recs)

    tools = {(r["family"], r["sample"]): tool_calls(r) for r in recs}
    n = len(recs)
    with_transcript = sum(1 for v in tools.values() if v is not None)

    cols = ("family", "s", "in", "cache_wr", "cache_rd", "out", "reason",
            "tools", "turns", "wall_s", "cost_$")
    width = (16, 3, 6, 10, 11, 8, 8, 7, 7, 9, 10)

    def row(values):
        return "".join(str(v).rjust(w) for v, w in zip(values, width))

    print("=" * 78)
    print(f"AGENTIC TOKEN AND COST ACCOUNTING  --  {n} Claude Code sessions")
    print(f"served model: {recs[0]['resolved_model']}   "
          f"CLI: {recs[0]['agent_cli_version']}")
    print("=" * 78)
    print(row(cols))
    print("-" * sum(width))

    total = defaultdict(int)
    total_cost = total_wall = 0.0
    mix = defaultdict(int)

    for r in recs:
        tok = r["tokens"]
        tc = tools[(r["family"], r["sample"])]
        n_tools = sum(tc.values()) if tc is not None else None
        if tc is not None:
            for name, count in tc.items():
                mix[name] += count
        for k in ("input", "cache_creation", "cache_read", "output", "reasoning"):
            total[k] += tok[k]
        total["turns"] += r["num_turns"]
        total["tools"] += n_tools or 0
        total_cost += r["cost_usd"]
        total_wall += r["wall_clock_s"]
        print(row((r["family"], r["sample"], tok["input"], tok["cache_creation"],
                   tok["cache_read"], tok["output"], tok["reasoning"],
                   n_tools if n_tools is not None else "-", r["num_turns"],
                   f"{r['wall_clock_s']:.1f}", f"{r['cost_usd']:.6f}")))

    print("-" * sum(width))
    print(row(("TOTAL", n, total["input"], total["cache_creation"],
               total["cache_read"], total["output"], total["reasoning"],
               total["tools"], total["turns"], f"{total_wall:.1f}",
               f"{total_cost:.4f}")))
    print(row(("MEAN", "", f"{total['input'] / n:.1f}",
               f"{total['cache_creation'] / n:.0f}",
               f"{total['cache_read'] / n:.0f}", f"{total['output'] / n:.0f}",
               f"{total['reasoning'] / n:.1f}",
               f"{total['tools'] / max(1, with_transcript):.1f}",
               f"{total['turns'] / n:.1f}", f"{total_wall / n:.1f}",
               f"{total_cost / n:.6f}")))
    print()
    print(f"transcripts archived for {with_transcript}/{n} sessions; tool means are")
    print("over those. Tool mix: " +
          ", ".join(f"{k} {v}" for k, v in sorted(mix.items(), key=lambda kv: -kv[1])))

    in_side = total["input"] + total["cache_creation"] + total["cache_read"]
    print()
    print("=" * 78)
    print("RECONCILIATION")
    print("=" * 78)
    print(f"  input-side tokens, all {n} sessions   {in_side:>12,}")
    for label, key in (("cache READS", "cache_read"),
                       ("cache WRITES", "cache_creation"),
                       ("uncached input", "input")):
        print(f"    of which {label:<26}{total[key]:>12,}  "
              f"({100 * total[key] / in_side:.3f}%)")
    print(f"  output tokens                        {total['output']:>12,}")
    print(f"  reasoning tokens                     {total['reasoning']:>12,}")
    print()
    print(f"  mean input-side tokens per session   {in_side / n:>12,.0f}")
    print(f"  mean turns per session               {total['turns'] / n:>12.1f}")
    print(f"  mean prompt per TURN                 {in_side / total['turns']:>12,.0f}"
          "   <- the real context size")
    print(f"  mean scaffold size                   "
          f"{sum(r['scaffold_tokens'] for r in recs) / n:>12,.0f}")

    print()
    print("  cost decomposition at the verified rate card:")
    for key in ("input", "cache_creation", "cache_read", "output"):
        amount = total[key] * RATE[key] / 1e6
        print(f"    {key:<16}{total[key]:>10,} x ${RATE[key]:>5.2f}/M = "
              f"${amount:>7.4f}  ({100 * amount / total_cost:>5.1f}%)")
    print(f"    {'CLI-reported':<16}{'':>10}                = ${total_cost:>7.4f}"
          f"   (reproduced for {n}/{n} sessions)")

    uncached = (in_side * RATE["input"] + total["output"] * RATE["output"]) / 1e6
    print()
    print(f"  Counterfactual: priced as uncached input, the same {n} sessions would")
    print(f"  cost ${uncached:.2f} — {uncached / total_cost:.1f}x the reported ${total_cost:.2f}.")
    print()
    print("  STATUS OF THE DOLLAR FIGURE: estimated, not measured. It is the Claude")
    print("  Code CLI's own `total_cost_usd`, computed client-side from the counters")
    print("  above at Anthropic list rates. The sessions authenticated against a")
    print("  subscription (no ANTHROPIC_API_KEY), so no per-request API invoice")
    print("  exists and nothing was billed at this amount. Token counts ARE measured;")
    print("  only their translation into dollars is a list-price reconstruction.")


if __name__ == "__main__":
    main()
