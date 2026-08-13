"""T4 model-evaluation runner.

For every (family x condition x sample) it renders the condition's prompt, asks
the model to implement the family's `Service`, archives the raw response and the
extracted source, compiles the result, scores it against the family's own
`Cases()` through the shipped oracle, and appends one record per schedule to
results/model_results.json.

    python -m generation.runner --samples 3

See docs/EVAL_RUNNER.md.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import evaluate, prompts
from .evaluate import Workspace, extract_go_source
from .families import ALL_FAMILIES, FAMILIES, ROOT, check_families
from .model import DEFAULT_EFFORT, DEFAULT_MAX_TOKENS, ModelError, StubClient
from .providers import DEFAULT_MODELS, PROVIDERS, build_client, missing_credentials

RESULTS_DIR = os.path.join(ROOT, "results")
RAW_DIR = os.path.join(RESULTS_DIR, "raw")
DEFAULT_OUT = os.path.join(RESULTS_DIR, "model_results.json")

INVARIANTS = evaluate.INVARIANTS


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m generation.runner",
        description="Evaluate an LLM on the T4 retry-safety benchmark.",
    )
    p.add_argument(
        "--samples", type=int, default=3, help="samples per (family, condition) [default: 3]"
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=None,
        help=(
            "sampling temperature. Omitted by default: the current frontier models "
            "reject this parameter outright. When passed, it is sent and dropped "
            "with a warning if the model refuses it."
        ),
    )
    p.add_argument(
        "--provider",
        default=os.environ.get("T4_PROVIDER", "anthropic"),
        choices=list(PROVIDERS),
        help="model vendor; defaults to $T4_PROVIDER, then anthropic",
    )
    p.add_argument(
        "--model",
        default=None,
        help=(
            "model id; defaults to $T4_MODEL_ID, then the provider default "
            + ", ".join(f"{k}={v}" for k, v in DEFAULT_MODELS.items())
        ),
    )
    p.add_argument(
        "--families",
        default="all",
        help=f"comma-separated subset of {','.join(ALL_FAMILIES)}, or 'all'",
    )
    p.add_argument(
        "--conditions",
        default="all",
        help=f"comma-separated subset of {','.join(prompts.CONDITIONS)}, or 'all'",
    )
    p.add_argument(
        "--agentic-iterations",
        type=int,
        default=3,
        help="max revise cycles for the agentic condition, against PUBLIC schedules [default: 3]",
    )
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--price-in", type=float, default=None, help="USD per 1M input tokens")
    p.add_argument("--price-out", type=float, default=None, help="USD per 1M output tokens")
    p.add_argument("--out", default=DEFAULT_OUT, help=f"records file [default: {DEFAULT_OUT}]")
    p.add_argument(
        "--fresh", action="store_true", help="overwrite the records file instead of appending"
    )
    p.add_argument(
        "--stub",
        action="store_true",
        help=(
            "run the full pipeline against an offline stub that returns the shipped "
            "correct reference. No API key, no network, no cost; every family should "
            "score 10/10. This is P5's acceptance check."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="render prompts and verify the toolchain without calling the model",
    )
    return p.parse_args(argv)


def _resolve(value, allowed, label):
    if value == "all":
        return list(allowed)
    picked = [v.strip() for v in value.split(",") if v.strip()]
    unknown = [v for v in picked if v not in allowed]
    if unknown:
        sys.exit(f"unknown {label}: {', '.join(unknown)}. Expected from: {', '.join(allowed)}")
    return picked


def preflight(args):
    problems = []
    if not evaluate.go_available():
        problems.append("`go` was not found on PATH (Go 1.24 is required)")
    problems.extend(check_families())
    if not (args.dry_run or args.stub):
        missing = missing_credentials(args.provider)
        if missing:
            problems.append(missing)
    if args.samples < 1:
        problems.append("--samples must be >= 1")
    if problems:
        sys.exit("preflight failed:\n  - " + "\n  - ".join(problems))


def compile_error_records(fam, schedules, candidate_id, meta, reason):
    """One record per schedule for a candidate that never compiled."""
    out = []
    for sch in schedules:
        rec = {
            "family": fam.name,
            "semantic_task_id": fam.semantic_task_id,
            "candidate": candidate_id,
            "correct_ref": False,
            "schedule_id": sch["schedule_id"],
            "seed": sch["seed"],
            "hidden": sch["hidden"],
            "compile_status": "error",
            "runtime_status": "not_run",
            "invariants": {k: False for k in INVARIANTS},
            "violations": None,
            "ok": False,
        }
        rec.update(meta)
        rec["compile_error"] = reason[:2000]
        out.append(rec)
    return out


def reconcile(results, fam, schedules, candidate_id, meta, note):
    """Attach metadata and fill in any schedule the driver failed to report."""
    by_id = {r["schedule_id"]: r for r in results}
    out = []
    for sch in schedules:
        rec = by_id.get(sch["schedule_id"])
        if rec is None:
            status = "nonconvergent" if "did not terminate" in (note or "") else "crashed"
            rec = {
                "family": fam.name,
                "semantic_task_id": fam.semantic_task_id,
                "candidate": candidate_id,
                "correct_ref": False,
                "schedule_id": sch["schedule_id"],
                "seed": sch["seed"],
                "hidden": sch["hidden"],
                "compile_status": "success",
                "runtime_status": status,
                "invariants": {k: False for k in INVARIANTS},
                "violations": None,
                "ok": False,
            }
            if note:
                rec["runtime_note"] = note[:2000]
        rec.update(meta)
        out.append(rec)
    return out


def public_feedback(outcome):
    """Compiler output or per-schedule oracle verdicts, for the agentic loop.

    Only PUBLIC schedules ever reach this function, so the hidden-set
    commitment in ARTIFACT_EVALUATION.md holds by construction.
    """
    if outcome.compile_status == "error":
        return (
            "Your file did not compile. The Go toolchain reported:\n\n"
            f"```\n{outcome.error[:4000]}\n```\n\n"
            "Return the complete corrected file."
        )
    failing = [r for r in outcome.results if not r.get("ok")]
    if not failing:
        return ""
    lines = [
        "Your file compiled, but the shared oracle rejected it on these PUBLIC "
        "schedules (hidden schedules were not run):",
        "",
    ]
    for rec in failing:
        viol = ", ".join(rec.get("violations") or []) or "none reported"
        lines.append(
            f"- `{rec['schedule_id']}` — runtime {rec['runtime_status']}, "
            f"violated: {viol}"
        )
    lines += [
        "",
        "Diagnose why each invariant was violated and return the complete "
        "corrected file.",
    ]
    return "\n".join(lines)


def generate(client, system, user, fam, ws, args, candidate_id, condition, artifact_dir):
    """Produce one candidate. Returns (source, raw_text, totals, iterations)."""
    totals = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "reasoning": 0,
        "total": 0,
        "cost_usd": 0.0,
    }
    iterations = []
    messages = [{"role": "user", "content": user}]
    source, raw = "", ""

    rounds = args.agentic_iterations if condition == "agentic" else 1
    for step in range(max(1, rounds)):
        comp = client.complete(system, messages)
        raw = comp.text
        for key, val in comp.token_dict().items():
            totals[key] += val
        totals["cost_usd"] = round(totals["cost_usd"] + comp.cost_usd, 6)
        source = extract_go_source(raw)

        if condition != "agentic":
            break

        probe = ws.evaluate(fam.name, source, candidate_id, subset="public")
        failed = [r["schedule_id"] for r in probe.results if not r.get("ok")]
        iterations.append(
            {
                "iteration": step,
                "compile_status": probe.compile_status,
                "compile_error": probe.error[:2000] if probe.compile_status == "error" else "",
                "public_failures": failed,
                "tokens": comp.token_dict(),
                "cost_usd": comp.cost_usd,
            }
        )
        feedback = public_feedback(probe)
        if not feedback:
            break
        if step == rounds - 1:
            break
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": feedback})

    with open(os.path.join(artifact_dir, "response_raw.md"), "w", encoding="utf-8") as fh:
        fh.write(raw)
    # Archived as .go.txt, not .go: results/ lives inside the Go module, and a
    # stray `package capture` file here would be picked up by `go build ./...`
    # and break `make test` for anyone who has run the evaluator. Rename it to
    # tasks/<family>/candidate.go to re-score it by hand.
    with open(os.path.join(artifact_dir, "candidate.go.txt"), "w", encoding="utf-8") as fh:
        fh.write(source)
    if iterations:
        with open(os.path.join(artifact_dir, "iterations.json"), "w", encoding="utf-8") as fh:
            json.dump(iterations, fh, indent=2)
    return source, raw, totals, iterations


def main(argv=None):
    args = parse_args(argv)
    preflight(args)

    families = [FAMILIES[f] for f in _resolve(args.families, ALL_FAMILIES, "family")]
    conditions = _resolve(args.conditions, prompts.CONDITIONS, "condition")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started = datetime.now(timezone.utc).isoformat()
    run_dir = os.path.join(RAW_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)

    current = {}
    client = None
    if args.stub:
        client = StubClient(lambda: current["fam"])
    elif not args.dry_run:
        try:
            client = build_client(
                args.provider,
                model_id=args.model,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                effort=args.effort,
                price_in=args.price_in,
                price_out=args.price_out,
            )
        except ModelError as exc:
            sys.exit(str(exc))
        if not client.pricing_known:
            print(
                f"warning: no list price on file for {client.model_id}; cost_usd will be 0. "
                "Pass --price-in/--price-out to record real cost.",
                file=sys.stderr,
            )

    model_id = client.model_id if client else (args.model or "dry-run")
    print(f"run {run_id}  provider={getattr(client, 'provider', args.provider)}  "
          f"model={model_id}  families={len(families)}  "
          f"conditions={len(conditions)}  samples={args.samples}")

    ws = Workspace()
    records, failures = [], []
    try:
        schedules = ws.schedules()

        for fam in families:
            current["fam"] = fam
            for condition in conditions:
                system, user = prompts.render(condition, fam)
                for sample in range(args.samples):
                    candidate_id = f"{model_id}/{condition}/s{sample}"
                    label = f"{fam.name:<15} {condition:<14} s{sample}"
                    artifact_dir = os.path.join(run_dir, fam.name, condition, f"s{sample}")
                    os.makedirs(artifact_dir, exist_ok=True)
                    for name, text in (
                        ("prompt_system.txt", system),
                        ("prompt_user.txt", user),
                    ):
                        with open(os.path.join(artifact_dir, name), "w", encoding="utf-8") as fh:
                            fh.write(text)

                    if args.dry_run:
                        print(f"  {label}  [dry-run] prompt {len(system) + len(user)} chars")
                        continue

                    started_at = time.time()
                    try:
                        source, _raw, totals, iterations = generate(
                            client, system, user, fam, ws, args, candidate_id,
                            condition, artifact_dir,
                        )
                    except ModelError as exc:
                        print(f"  {label}  MODEL ERROR: {exc}", file=sys.stderr)
                        failures.append({"candidate": candidate_id, "family": fam.name,
                                         "error": str(exc)})
                        continue

                    outcome = ws.evaluate(fam.name, source, candidate_id, subset="all")
                    meta = {
                        "model_id": model_id,
                        "condition": condition,
                        "sample": sample,
                        "temperature": client.config_snapshot()["temperature"],
                        "provider": getattr(client, "provider", args.provider),
                        "resolved_model": getattr(client, "resolved_model", "") or model_id,
                        "tokens": {k: totals[k] for k in
                                   ("input", "output", "cache_read", "cache_creation",
                                    "reasoning", "total")},
                        "cost_usd": totals["cost_usd"],
                        "run_id": run_id,
                        "prompt_sha256": prompts.prompt_hash(system, user),
                        "agentic_iterations": len(iterations) or None,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }

                    if outcome.compile_status == "error":
                        with open(os.path.join(artifact_dir, "compile_error.txt"), "w",
                                  encoding="utf-8") as fh:
                            fh.write(outcome.error)
                        new = compile_error_records(
                            fam, schedules[fam.name], candidate_id, meta, outcome.error
                        )
                        verdict = "COMPILE ERROR"
                    else:
                        new = reconcile(
                            outcome.results, fam, schedules[fam.name],
                            candidate_id, meta, outcome.error,
                        )
                        passed = sum(1 for r in new if r["ok"])
                        hidden = [r for r in new if r["hidden"]]
                        hidden_pass = sum(1 for r in hidden if r["ok"])
                        verdict = (f"{passed}/{len(new)} all, "
                                   f"{hidden_pass}/{len(hidden)} hidden")

                    records.extend(new)
                    with open(os.path.join(artifact_dir, "meta.json"), "w", encoding="utf-8") as fh:
                        json.dump({**meta, "factory": outcome.factory,
                                   "compile_status": outcome.compile_status}, fh, indent=2)
                    print(f"  {label}  {verdict}  "
                          f"({totals['total']} tok, ${totals['cost_usd']:.4f}, "
                          f"{time.time() - started_at:.0f}s)")
    finally:
        ws.cleanup()

    if args.dry_run:
        print(f"\ndry run complete; prompts written to {run_dir}")
        return 0

    existing = []
    if os.path.exists(args.out) and not args.fresh:
        with open(args.out, encoding="utf-8") as fh:
            try:
                existing = json.load(fh)
            except json.JSONDecodeError:
                print(f"warning: {args.out} was not valid JSON; starting fresh", file=sys.stderr)
    combined = existing + records
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(combined, fh, indent=2)

    config = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model": client.config_snapshot(),
        "samples": args.samples,
        "agentic_iterations_max": args.agentic_iterations,
        "families": [f.name for f in families],
        "conditions": conditions,
        "records_written": len(records),
        "records_total_in_file": len(combined),
        "generation_failures": failures,
        "prompt_template_sha256": prompts.template_hashes(),
        "totals": {
            "tokens": sum(r["tokens"]["total"] for r in records),
            "cost_usd": round(sum(r["cost_usd"] for r in records), 4),
        },
    }
    with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)

    summarize(records)
    print(f"\nwrote {len(records)} records to {args.out} ({len(combined)} total)")
    print(f"raw artifacts + config.json in {run_dir}")
    if failures:
        print(f"{len(failures)} generation failure(s) — see config.json", file=sys.stderr)
    return 0


def summarize(records):
    """Per-condition compile rate and hidden-schedule safety, over the run."""
    if not records:
        return
    by_cond = {}
    for rec in records:
        agg = by_cond.setdefault(
            rec["condition"], {"cands": {}, "hidden": 0, "hidden_ok": 0, "cost": 0.0}
        )
        agg["cands"].setdefault(rec["candidate"], rec["compile_status"])
        if rec["hidden"]:
            agg["hidden"] += 1
            agg["hidden_ok"] += 1 if rec["ok"] else 0
    # cost is recorded per-record but billed per-candidate; de-duplicate before summing
    costs = {}
    for rec in records:
        costs.setdefault(rec["candidate"], rec["cost_usd"])
    print("\ncondition        compile      hidden-schedule safety")
    print("-" * 55)
    for cond, agg in sorted(by_cond.items()):
        n = len(agg["cands"])
        ok = sum(1 for v in agg["cands"].values() if v == "success")
        rate = f"{ok}/{n}"
        hid = f"{agg['hidden_ok']}/{agg['hidden']}" if agg["hidden"] else "n/a"
        pct = f" ({agg['hidden_ok'] / agg['hidden']:.2f})" if agg["hidden"] else ""
        print(f"{cond:<16} {rate:<12} {hid}{pct}")
    print(f"\ntotal generation cost: ${sum(costs.values()):.4f}")


if __name__ == "__main__":
    sys.exit(main())
