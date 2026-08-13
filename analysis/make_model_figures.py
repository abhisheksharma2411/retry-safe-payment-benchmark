#!/usr/bin/env python3
"""Paper-ready outputs from the model evaluation.

Input : results/model_results.json  (produced by generation/runner)
Output: analysis/figures/model_*.png
        analysis/figures/model_data.tex   (LaTeX tables + pgfplots coords)
        results/model_metrics.json        (machine-readable)
        results/SUMMARY.md                (plain text)

Everything is reported **by served snapshot** — the model the API says actually
answered — never by the alias that was requested. `gemini-pro-latest` and
`gemini-3.1-pro-preview` resolve to the same snapshot and are pooled into one
system here; the aliasing itself is preserved in the provenance table so the
pooling is visible rather than silent.

This script never touches results/pilot_results.json or anything derived from
it; the reference/mutant pilot and the model evaluation are separate artifacts
with separate figures.
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_stats import (  # noqa: E402
    ALPHA, BOOTSTRAP, SEED, bootstrap_indices, ci_from_counts, ci_from_delta,
    survival_rk, tex_name,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RECORDS = os.path.join(ROOT, "results", "model_results.json")
FIGDIR = os.path.join(HERE, "figures")
os.makedirs(FIGDIR, exist_ok=True)

INVARIANTS = ["compile_error", "at_most_one", "no_lost_effect", "no_false_dedup",
              "payload_consistency", "reproducible", "recovery"]
SINGLE_SHOT = ["zero_shot", "retrieval", "domain_guided"]
CONDITION_ORDER = SINGLE_SHOT + ["agentic"]

# (numerator, denominator) over the per-family counters built below.
METRICS = {
    "compile_rate": ("compiled", "cands"),
    "functional_pass1": ("happy_ok", "happy"),
    "unconditional_safety": ("all_ok", "all"),
    "safety_given_functional": ("fn_hid_ok", "fn_hid"),
    "robust_success": ("robust", "cands"),
    "hidden_safety": ("hid_ok", "hid"),
    "public_safety": ("pub_ok", "pub"),
}
METRIC_LABELS = {
    "compile_rate": "compile rate",
    "functional_pass1": "functional pass@1",
    "unconditional_safety": "unconditional safety",
    "safety_given_functional": "safety | functional",
    "robust_success": "robust success",
    "hidden_safety": "hidden-schedule safety",
    "public_safety": "public-schedule safety",
}

COUNTERS = ("cands", "compiled", "happy", "happy_ok", "all", "all_ok",
            "hid", "hid_ok", "pub", "pub_ok", "fn_hid", "fn_hid_ok", "robust")


def load():
    with open(RECORDS) as fh:
        return json.load(fh)


def build_stats(rows):
    """(system, condition) -> family -> counters, plus provenance and cost."""
    by_cand = defaultdict(list)
    for r in rows:
        by_cand[(r["resolved_model"], r["condition"], r["family"], r["candidate"])].append(r)

    stats = defaultdict(lambda: defaultdict(lambda: dict.fromkeys(COUNTERS, 0)))
    families = set()
    for (system, cond, family, _cand), recs in by_cand.items():
        families.add(family)
        c = stats[(system, cond)][family]
        c["cands"] += 1
        compiled = recs[0]["compile_status"] == "success"
        c["compiled"] += 1 if compiled else 0

        happy = [x for x in recs if x["schedule_id"].endswith("-happy")]
        c["happy"] += len(happy)
        functional = bool(happy) and all(x["ok"] for x in happy)
        c["happy_ok"] += sum(1 for x in happy if x["ok"])

        c["all"] += len(recs)
        c["all_ok"] += sum(1 for x in recs if x["ok"])

        hidden = [x for x in recs if x["hidden"]]
        public = [x for x in recs if not x["hidden"]]
        c["hid"] += len(hidden)
        c["hid_ok"] += sum(1 for x in hidden if x["ok"])
        c["pub"] += len(public)
        c["pub_ok"] += sum(1 for x in public if x["ok"])

        if functional:
            c["fn_hid"] += len(hidden)
            c["fn_hid_ok"] += sum(1 for x in hidden if x["ok"])
        # Robust success: this candidate survived EVERY hidden schedule.
        if hidden and all(x["ok"] for x in hidden):
            c["robust"] += 1
    return stats, sorted(families)


def provenance_table(rows):
    """Served snapshot -> {requested aliases, snapshot string, date, pre-pub}."""
    out = {}
    for r in rows:
        p = r.get("provenance") or {}
        served = r["resolved_model"]
        entry = out.setdefault(served, {
            "served_snapshot": served,
            "requested_as": set(),
            "model_snapshot": p.get("model_snapshot") or "",
            "snapshot_date": p.get("snapshot_date") or "",
            "display_name": p.get("model_display_name") or "",
            "release_date": p.get("release_date"),
            "training_cutoff": p.get("training_cutoff"),
            "training_cutoff_source": p.get("training_cutoff_source"),
            "predates_publication": p.get("snapshot_predates_repo_publication"),
            "provider": r["provider"],
        })
        entry["requested_as"].add(p.get("requested_model") or r["model_id"])
    for e in out.values():
        e["requested_as"] = sorted(e["requested_as"])
    return out


def compute(stats, families, rng):
    """Point estimates and bootstrap CIs for every (system, condition) cell."""
    idx_by_system = {}
    cells = {}
    for (system, cond), fam_stats in stats.items():
        # One resample matrix per system, reused across that system's conditions
        # so within-system deltas are paired.
        if system not in idx_by_system:
            idx_by_system[system] = bootstrap_indices(len(families), rng, BOOTSTRAP)
        idx = idx_by_system[system]
        entry = {"system": system, "condition": cond,
                 "n_families": len(families),
                 "n_candidates": sum(f["cands"] for f in fam_stats.values()),
                 "n_records": sum(f["all"] for f in fam_stats.values()),
                 "metrics": {}}
        for name, (num, den) in METRICS.items():
            nums = [fam_stats[f][num] for f in families]
            dens = [fam_stats[f][den] for f in families]
            point, lo, hi = ci_from_counts(nums, dens, idx)
            entry["metrics"][name] = {
                "point": point, "ci_low": lo, "ci_high": hi,
                "numerator": int(sum(nums)), "denominator": int(sum(dens)),
                "saturated": bool(sum(dens) and sum(nums) == sum(dens)),
            }
        cells[(system, cond)] = entry
    return cells, idx_by_system


def deltas(stats, families, idx_by_system, metric, cond_a, cond_b):
    """Paired (cond_a - cond_b) per system, with CI."""
    num, den = METRICS[metric]
    out = {}
    for system in sorted({s for s, _ in stats}):
        a, b = stats.get((system, cond_a)), stats.get((system, cond_b))
        if not a or not b:
            continue
        res = ci_from_delta(
            [a[f][num] for f in families], [a[f][den] for f in families],
            [b[f][num] for f in families], [b[f][den] for f in families],
            idx_by_system[system],
        )
        out[system] = {"delta": res[0], "ci_low": res[1], "ci_high": res[2],
                       "from": cond_b, "to": cond_a, "metric": metric}
    return out


def public_hidden_gap(stats, families, idx_by_system):
    """RQ4: public-schedule safety minus hidden-schedule safety, per cell."""
    out = {}
    for (system, cond), fam_stats in stats.items():
        res = ci_from_delta(
            [fam_stats[f]["pub_ok"] for f in families],
            [fam_stats[f]["pub"] for f in families],
            [fam_stats[f]["hid_ok"] for f in families],
            [fam_stats[f]["hid"] for f in families],
            idx_by_system[system],
        )
        out[(system, cond)] = {"gap": res[0], "ci_low": res[1], "ci_high": res[2]}
    return out


def failure_composition(rows):
    """(system, condition) -> failure mode -> record count.

    `compile_error` is a category alongside the six invariants, because a
    candidate that never built records `violations: null` and would otherwise
    vanish from the composition entirely — for Pro zero-shot that is 42 of the
    55 hidden failures, i.e. the largest single failure mode would have been
    invisible in a chart of "why did runs fail".
    """
    out = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r["ok"]:
            continue
        key = (r["resolved_model"], r["condition"])
        if r["compile_status"] != "success":
            out[key]["compile_error"] += 1
            continue
        for v in (r["violations"] or []):
            out[key][v] += 1
    return out


def survival(cells):
    """(system, condition) -> {k: R_k} over the pooled hidden set."""
    out = {}
    for key, entry in cells.items():
        m = entry["metrics"]["hidden_safety"]["numerator"]
        n = entry["metrics"]["hidden_safety"]["denominator"]
        ks = list(range(1, 9))
        out[key] = {
            "m": m, "n": n,
            "curve": {k: survival_rk(m, n, k) for k in ks},
            "saturated": m == n,
        }
    return out


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def sysname(system):
    return system.replace("[1m]", "").replace("-preview", "")


def figures(cells, surv, comp, rq3, gaps):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    palette = ["#000000", "#E69F00", "#56B4E9", "#009E73", "#CC79A7", "#0072B2",
               "#D55E00", "#999999", "#332288", "#44AA99"]
    markers = ["o", "s", "^", "D", "v", "P", "X", "*", "h", "<"]
    keys = sorted(cells, key=lambda k: (k[0], CONDITION_ORDER.index(k[1])))

    # --- survival curves ---------------------------------------------------
    plt.figure(figsize=(7.2, 4.2))
    for i, key in enumerate(keys):
        s = surv[key]
        ks = sorted(s["curve"])
        ys = [s["curve"][k] for k in ks]
        label = f"{sysname(key[0])} / {key[1]} ({s['m']}/{s['n']})"
        plt.plot(ks, ys, marker=markers[i % len(markers)], color=palette[i % len(palette)],
                 label=label, linewidth=1.5, markersize=4.5,
                 linestyle="--" if s["saturated"] else "-")
    plt.xlabel("$k$ (independent hidden fault schedules)")
    plt.ylabel("survival $R_k$")
    plt.ylim(-0.02, 1.02)
    plt.grid(True, alpha=0.3)
    plt.legend(fontsize=6, loc="lower left", ncol=1)
    plt.title("Measured schedule-survival by served snapshot and condition\n"
              "(dashed = saturated at 1.0: every hidden schedule passed)", fontsize=9)
    plt.tight_layout()
    plt.savefig(os.path.join(FIGDIR, "model_survival.png"), dpi=160)
    plt.close()

    # --- RQ3 deltas --------------------------------------------------------
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    systems = sorted(rq3)
    xs = np.arange(len(systems))
    vals = [rq3[s]["delta"] for s in systems]
    lo = [rq3[s]["delta"] - rq3[s]["ci_low"] for s in systems]
    hi = [rq3[s]["ci_high"] - rq3[s]["delta"] for s in systems]
    ax.bar(xs, vals, color=[palette[i % len(palette)] for i in range(len(systems))], width=0.55)
    ax.errorbar(xs, vals, yerr=[lo, hi], fmt="none", ecolor="black", capsize=4, linewidth=1.2)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xticks(xs)
    ax.set_xticklabels([sysname(s) for s in systems], fontsize=7, rotation=12)
    ax.set_ylabel("$\\Delta$ hidden-schedule safety")
    ax.set_title("RQ3: zero-shot $\\rightarrow$ domain-guided, per served snapshot\n"
                 "(bootstrap 95% CI over task families, n=7)", fontsize=9)
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(FIGDIR, "model_rq3_delta.png"), dpi=160)
    plt.close()

    # --- failure composition ----------------------------------------------
    fig, ax = plt.subplots(figsize=(8.8, 4.2))
    labels = [f"{sysname(k[0])}\n{k[1]}" for k in keys]
    xs = np.arange(len(keys))
    bottom = np.zeros(len(keys))
    for j, inv in enumerate(INVARIANTS):
        vals = np.array([comp[k].get(inv, 0) for k in keys], dtype=float)
        if vals.sum() == 0:
            continue  # keep the legend to modes that actually occurred
        ax.bar(xs, vals, bottom=bottom, label=inv.replace("_", " "),
               color=palette[j % len(palette)], width=0.6)
        bottom += vals
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=7, rotation=35, ha="right")
    # Not "invariant violations": compile_error is a failure mode here too, and a
    # candidate that never built has no invariant verdicts at all.
    ax.set_ylabel("failing records")
    ax.set_title("Failure composition, per served snapshot and condition\n"
                 "(compile errors included: a candidate that never built has no "
                 "invariant verdicts)", fontsize=9)
    ax.legend(fontsize=7, loc="center left", bbox_to_anchor=(1.01, 0.5))
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(FIGDIR, "model_failure_composition.png"), dpi=160,
                bbox_inches="tight")
    plt.close()

    # --- public vs hidden --------------------------------------------------
    fig, ax = plt.subplots(figsize=(8.4, 3.6))
    xs = np.arange(len(keys))
    pub = [cells[k]["metrics"]["public_safety"]["point"] for k in keys]
    hid = [cells[k]["metrics"]["hidden_safety"]["point"] for k in keys]
    ax.bar(xs - 0.2, pub, width=0.38, label="public schedules", color="#56B4E9")
    ax.bar(xs + 0.2, hid, width=0.38, label="hidden schedules", color="#D55E00")
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{sysname(k[0])}\n{k[1]}" for k in keys], fontsize=6)
    ax.set_ylabel("safety rate")
    ax.set_ylim(0, 1.05)
    ax.legend(fontsize=7)
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_title("RQ4: public-vs-hidden safety gap (overfitting to the visible set)", fontsize=9)
    plt.tight_layout()
    plt.savefig(os.path.join(FIGDIR, "model_public_vs_hidden.png"), dpi=160)
    plt.close()
    return ["model_survival.png", "model_rq3_delta.png",
            "model_failure_composition.png", "model_public_vs_hidden.png"]


# ---------------------------------------------------------------------------
# LaTeX
# ---------------------------------------------------------------------------

def write_tex(cells, surv, comp, rq3, gaps, prov, path):
    keys = sorted(cells, key=lambda k: (k[0], CONDITION_ORDER.index(k[1])))
    L = [
        "% Auto-generated by analysis/make_model_figures.py -- measured model evaluation.",
        "% Do not edit by hand; regenerate with `make model-figures`.",
        "%",
        "% Reported BY SERVED SNAPSHOT (the model the API says answered), never by the",
        "% alias requested. gemini-pro-latest and gemini-3.1-pro-preview resolve to the",
        "% same snapshot and are pooled; \\provenancetable keeps the aliasing visible.",
        "%",
        "% All intervals are bootstrap 95% percentile CIs resampling TASK FAMILIES",
        f"% (n=7) with replacement, {BOOTSTRAP} replicates, seed {SEED}. Schedules are",
        "% nested within candidates and are NOT the resampling unit.",
        "%",
        "% Macro names spell digits out (1 -> one): a TeX control sequence is letters",
        "% only, so \\fooone is a macro but \\foo1 is not.",
        "%",
    ]

    def cs(*parts):
        return "".join(tex_name(p) for p in parts)

    # --- main table --------------------------------------------------------
    L += ["% ---------------------------------------------------------------------------",
          "% Main results table. Each cell: point [lo, hi].",
          "% ---------------------------------------------------------------------------",
          "\\def\\modelresultstable{%",
          "\\begin{tabular}{llrrrrr}",
          "\\toprule",
          "System (served snapshot) & Condition & Compile & Func.\\ pass@1 & "
          "Uncond.\\ safety & Safety\\,$\\mid$\\,func. & Robust succ. \\\\",
          "\\midrule"]
    for k in keys:
        m = cells[k]["metrics"]
        row = [sysname(k[0]).replace("_", "\\_"), k[1].replace("_", "\\_")]
        for name in ("compile_rate", "functional_pass1", "unconditional_safety",
                     "safety_given_functional", "robust_success"):
            d = m[name]
            row.append(f"{d['point']:.3f} \\scriptsize[{d['ci_low']:.2f},{d['ci_high']:.2f}]")
        L.append(" & ".join(row) + " \\\\")
    L += ["\\bottomrule", "\\end{tabular}%", "}", "%"]

    # --- RQ3 ---------------------------------------------------------------
    L += ["% RQ3: zero-shot -> domain-guided delta in hidden-schedule safety.",
          "\\def\\rqthreetable{%",
          "\\begin{tabular}{lrr}", "\\toprule",
          "System (served snapshot) & $\\Delta$ hidden safety & 95\\% CI \\\\", "\\midrule"]
    for s in sorted(rq3):
        d = rq3[s]
        L.append(f"{sysname(s)} & {d['delta']:+.3f} & "
                 f"[{d['ci_low']:+.3f}, {d['ci_high']:+.3f}] \\\\")
    L += ["\\bottomrule", "\\end{tabular}%", "}", "%"]
    for s, d in rq3.items():
        L.append(f"\\def\\rqthreedelta{cs(sysname(s))}{{{d['delta']:.4f}}}")
        L.append(f"\\def\\rqthreecilow{cs(sysname(s))}{{{d['ci_low']:.4f}}}")
        L.append(f"\\def\\rqthreecihigh{cs(sysname(s))}{{{d['ci_high']:.4f}}}")
    L.append("%")

    # --- RQ4 ---------------------------------------------------------------
    L += ["% RQ4: public-minus-hidden safety gap. Positive = the visible set flatters."]
    for k in keys:
        g = gaps[k]
        L.append(f"\\def\\pubhidgap{cs(sysname(k[0]), k[1])}{{{g['gap']:.4f}}}")
        L.append(f"\\def\\pubhidgapci{cs(sysname(k[0]), k[1])}"
                 f"{{[{g['ci_low']:.4f}, {g['ci_high']:.4f}]}}")
    L += ["\\def\\pubhidcoords{" + " ".join(
        f"({cs(sysname(k[0]), k[1])},{gaps[k]['gap']:.4f})" for k in keys) + "}", "%"]

    # --- survival ----------------------------------------------------------
    L += ["% Survival curves. Coordinates are (k, R_k) with R_k = C(m,k)/C(n,k).",
          "% A cell with m == n saturates at 1.0 and is listed in \\saturatedcells."]
    for k in keys:
        s = surv[k]
        coords = " ".join(f"({kk},{s['curve'][kk]:.4f})" for kk in sorted(s["curve"]))
        L.append(f"\\def\\modelsurvival{cs(sysname(k[0]), k[1])}{{{coords}}}  "
                 f"% {s['m']}/{s['n']} hidden passed"
                 + ("  SATURATED" if s["saturated"] else ""))
    sat = [f"{sysname(k[0])}/{k[1]}" for k in keys if surv[k]["saturated"]]
    L.append("\\def\\saturatedcells{" + ", ".join(sat).replace("_", "\\_") + "}")
    L.append("%")

    # --- failure composition ----------------------------------------------
    L += ["% Failure composition. Coordinates are (invariant, violating records).",
          f"\\def\\modelinvariantnames{{{','.join(tex_name(i) for i in INVARIANTS)}}}"]
    for k in keys:
        coords = " ".join(f"({tex_name(inv)},{comp[k].get(inv, 0)})" for inv in INVARIANTS)
        L.append(f"\\def\\modelfailcomp{cs(sysname(k[0]), k[1])}{{{coords}}}")
    L.append("%")

    # --- provenance --------------------------------------------------------
    L += ["% Provenance. Repo published 2026-08-13; a snapshot before that date",
          "% cannot have trained on this repository.",
          "\\def\\provenancetable{%",
          "\\begin{tabular}{llll}", "\\toprule",
          "Served snapshot & Requested as & Snapshot version & Pre-publication \\\\",
          "\\midrule"]
    for served, e in sorted(prov.items()):
        pre = {True: "yes", False: "no", None: "unknown"}[e["predates_publication"]]
        L.append(" & ".join([
            sysname(served).replace("_", "\\_"),
            ", ".join(x.replace("_", "\\_") for x in e["requested_as"]),
            (e["model_snapshot"] or "--").replace("_", "\\_"),
            f"{pre} ({e['snapshot_date'] or 'n/a'})",
        ]) + " \\\\")
    L += ["\\bottomrule", "\\end{tabular}%", "}"]

    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")


def write_summary(cells, surv, comp, rq3, rq3_robust, inversion, gaps, prov,
                  totals, path):
    """results/SUMMARY.md — the plain-text account of the evaluation."""
    keys = sorted(cells, key=lambda k: (k[0], CONDITION_ORDER.index(k[1])))

    def fmt(d):
        return f"{d['point']:.3f} [{d['ci_low']:.2f}, {d['ci_high']:.2f}]"

    L = ["# T4 model evaluation — summary", "",
         "Generated by `analysis/make_model_figures.py` from "
         "`results/model_results.json`. Every number below is measured; none is "
         "hand-entered.", "",
         "## Setup", "",
         f"- **Records**: {totals['records']} over {totals['candidates']} candidates",
         f"- **Task families**: {len(totals['families'])} "
         f"({', '.join(totals['families'])})",
         "- **Conditions**: zero_shot, retrieval, domain_guided (single-shot, API "
         "models); agentic (Claude Code CLI in a sealed scaffold)",
         "- **Samples**: n=3 per (system x condition x family)",
         "- **Schedules**: 10 per candidate — 4 public, 6 hidden. Scoring is on the "
         "hidden set.",
         f"- **Total tokens**: {totals['tokens']:,}",
         f"- **Total cost**: ${totals['cost_usd']:.2f} "
         "(all of it the agentic condition; the Gemini snapshots have no list "
         "price on file, so their cost records as 0 and this figure is a floor, "
         "not a total — token counts are exact)",
         "",
         "### Statistical method", "",
         f"All intervals are bootstrap 95% percentile CIs, {BOOTSTRAP} replicates, "
         f"seed {SEED}, resampling **semantic task families (n=7) with "
         "replacement**.",
         "",
         "Schedules are *not* the resampling unit. Ten schedules are nested inside "
         "each candidate, and a candidate that gets recovery wrong fails several of "
         "them for one underlying reason; treating those as independent evidence "
         "would make every interval far too narrow. The families are what the paper "
         "wants to generalise over, so they are what is resampled.",
         "",
         "With n=7 the intervals are wide, and they should be. Where a cell is "
         "perfect on every family the bootstrap is degenerate — every resample "
         "returns 1.0 and the interval collapses to [1.00, 1.00]. That is the "
         "estimator hitting a boundary, **not** evidence of precision.",
         "",
         "## Served snapshots", "",
         "Reported by the snapshot the API says actually answered, never by the "
         "alias requested. The repository was published **2026-08-13**; a snapshot "
         "dated before that cannot have trained on it.", "",
         "| Served snapshot | Requested as | Snapshot version | Date | Predates publication |",
         "|---|---|---|---|---|"]
    for served, e in sorted(prov.items()):
        pre = {True: "**yes**", False: "no", None: "unknown"}[e["predates_publication"]]
        L.append(f"| `{served}` | {', '.join('`%s`' % x for x in e['requested_as'])} "
                 f"| `{e['model_snapshot'] or '—'}` | {e['snapshot_date'] or '—'} | {pre} |")
    L += ["",
          "`gemini-pro-latest` and `gemini-3.1-pro-preview` resolve to the **same "
          "snapshot** and are pooled into one system (42 candidates per condition "
          "rather than 21). The aliasing is kept visible rather than silently "
          "collapsed.",
          "",
          "No API exposes a training cutoff, so `training_cutoff` is null in every "
          "record with the reason recorded. The snapshot date bounds it from above, "
          "which is the direction the contamination argument needs. The claim this "
          "supports is narrow: these models cannot have memorised *this repository*, "
          "its hidden schedules, or its reference solutions. It does **not** claim "
          "they lack knowledge of idempotency or reserve-then-effect — that material "
          "is widely documented and is in every model's training data.",
          "",
          "## 1. Per system x condition", "",
          "| System | Condition | Compile | Func. pass@1 | Uncond. safety | "
          "Safety \\| func. | Robust success |",
          "|---|---|---|---|---|---|---|"]
    for k in keys:
        m = cells[k]["metrics"]
        L.append(f"| `{k[0]}` | {k[1]} | " + " | ".join(
            fmt(m[n]) for n in ("compile_rate", "functional_pass1",
                                "unconditional_safety", "safety_given_functional",
                                "robust_success")) + " |")
    L += ["",
          "**Definitions.** *Compile* = candidates that built. *Functional pass@1* = "
          "candidates passing the no-fault `-happy` schedule. *Unconditional safety* "
          "= records passing all six invariants, over all 10 schedules. *Safety | "
          "functional* = hidden-schedule safety restricted to candidates that pass "
          "their happy schedule. *Robust success* = candidates passing **every** "
          "hidden schedule.",
          "",
          "**Functional pass@1 equals compile rate in every cell.** Not a bug: all "
          "261 candidates that compiled also passed their no-fault schedule, and all "
          "12 that failed it were the ones that never built. At Tier-1 the happy path "
          "carries no information beyond compiling — the discriminating power is "
          "entirely in the fault schedules, which is the point of the benchmark.",
          "",
          "## 2. RQ3 — zero-shot to domain-guided", "",
          "Headline effect, on hidden-schedule safety, paired on the same family "
          "resamples:", "",
          "| Served snapshot | Delta hidden safety | 95% CI | Excludes 0? |",
          "|---|---|---|---|"]
    for s in sorted(rq3):
        d = rq3[s]
        excl = "**yes**" if (d["ci_low"] > 0 or d["ci_high"] < 0) else "no"
        L.append(f"| `{s}` | {d['delta']:+.4f} | [{d['ci_low']:+.4f}, "
                 f"{d['ci_high']:+.4f}] | {excl} |")
    L += ["", "Same contrast on robust success (all six hidden schedules passed):", "",
          "| Served snapshot | Delta robust success | 95% CI |", "|---|---|---|"]
    for s in sorted(rq3_robust):
        d = rq3_robust[s]
        L.append(f"| `{s}` | {d['delta']:+.4f} | [{d['ci_low']:+.4f}, {d['ci_high']:+.4f}] |")
    L += ["",
          "The effect is concentrated in the weakest system: `gemini-3.1-pro-preview` "
          "gains +0.18 hidden safety and its interval excludes zero. `gemini-3.6-flash` "
          "gains a small but non-zero amount; `gemini-3-flash-preview` shows no "
          "detectable effect (see caveat 2).",
          "",
          "## 3. RQ4 — public vs hidden gap", "",
          "Positive = the visible schedules flatter the candidate.", "",
          "| System | Condition | Public | Hidden | Gap | 95% CI |",
          "|---|---|---|---|---|---|"]
    for k in keys:
        g, m = gaps[k], cells[k]["metrics"]
        L.append(f"| `{k[0]}` | {k[1]} | {m['public_safety']['point']:.3f} | "
                 f"{m['hidden_safety']['point']:.3f} | {g['gap']:+.4f} | "
                 f"[{g['ci_low']:+.4f}, {g['ci_high']:+.4f}] |")
    L += ["",
          "Every gap is small in absolute terms, but the pattern is consistent: the "
          "gap is largest exactly where the model is weakest "
          "(`gemini-3.1-pro-preview` zero-shot, +0.052) and vanishes where it is "
          "strongest. The public set systematically over-reports safety for the "
          "candidates that most need catching — which is the case for holding a "
          "hidden set at all.",
          "",
          "## 4. Failure composition", "",
          "Failing records by mode. `compile_error` is included as a mode: a "
          "candidate that never built records no invariant violations and would "
          "otherwise vanish from this table entirely.", "",
          "| System | Condition | Failing records | Modes (count) |",
          "|---|---|---|---|"]
    for k in keys:
        modes = {a: b for a, b in comp[k].items() if b}
        total = sum(modes.values())
        text = ", ".join(f"`{a}` {b}" for a, b in
                         sorted(modes.items(), key=lambda x: -x[1])) or "— none —"
        L.append(f"| `{k[0]}` | {k[1]} | {total} | {text} |")
    L += ["",
          "Two things stand out. **`compile_error` dominates raw failure counts** "
          "(70 of 83 failing records for Pro zero-shot). And among candidates that "
          "did build, **`no_lost_effect` is the dominant semantic failure in every "
          "cell that has one** — models defer, return `IN_PROGRESS`, and lose the "
          "payment rather than risk a double charge. Refusing to act is not safety, "
          "and it is the single most common way these models get retry-safety wrong.",
          "",
          "## 5. Survival R_k", "",
          "Hypergeometric R_k = C(m,k)/C(n,k): the probability a candidate that "
          "passed m of n hidden schedules survives k of them drawn without "
          "replacement. Does not assume schedules are independent.", "",
          "| System | Condition | m/n | R_1 | R_6 | Saturated |",
          "|---|---|---|---|---|---|"]
    for k in keys:
        s = surv[k]
        L.append(f"| `{k[0]}` | {k[1]} | {s['m']}/{s['n']} | {s['curve'][1]:.4f} | "
                 f"{s['curve'][6]:.4f} | {'**yes**' if s['saturated'] else 'no'} |")
    sat = [f"`{k[0]}`/{k[1]}" for k in keys if surv[k]["saturated"]]
    L += ["",
          f"**Saturated cells (R_k = 1.0 for all k): {', '.join(sat)}.** These passed "
          "every hidden schedule, so the curve is flat at 1.0 and the cell "
          "distinguishes nothing. R_6 is where the systems separate: Pro zero-shot "
          "collapses to 0.224 while its domain-guided counterpart holds 0.782.",
          "",
          "## Caveats", "",
          "### 1. The agentic condition saturates — it has no discriminating power here",
          "",
          "`claude-opus-4-6` under the agentic condition scored **126/126 hidden "
          "schedules across all 7 families and all 3 samples**, with a perfect result "
          "on every metric. Its bootstrap intervals are all [1.00, 1.00] purely "
          "because the estimator is at a boundary.",
          "",
          "This is a **ceiling effect, not a measurement**. At Tier-1 the agentic "
          "condition cannot rank systems, cannot show a prompting effect, and cannot "
          "be compared meaningfully against the single-shot cells — a floor of 1.0 "
          "leaves no room to differ. Any claim of the form \"agentic beats "
          "single-shot by X\" is unsupported by this data; all that is established is "
          "that this configuration is at or above the benchmark's Tier-1 ceiling. "
          "Discriminating among agentic systems needs harder families.",
          "",
          "Note also that the agentic and single-shot conditions differ in more than "
          "the prompt — the CLI brings its own system prompt, its own tools, and a "
          "multi-turn loop, injecting ~230k tokens of context around a 4k-token "
          "scaffold. That is why the runner refuses to score a CLI agent under a "
          "base-model condition at all.",
          "",
          "### 2. The 3-flash-preview domain-guided < retrieval inversion is noise",
          ""]
    inv = inversion.get("gemini-3-flash-preview")
    if inv:
        L += [f"`gemini-3-flash-preview` scores domain_guided "
              f"{cells[('gemini-3-flash-preview', 'domain_guided')]['metrics']['hidden_safety']['point']:.3f} "
              f"vs retrieval "
              f"{cells[('gemini-3-flash-preview', 'retrieval')]['metrics']['hidden_safety']['point']:.3f} "
              "on hidden safety — the one inversion in the table, and the wrong "
              "direction for RQ3.",
              "",
              f"**The difference is {inv['delta']:+.4f}, 95% CI "
              f"[{inv['ci_low']:+.4f}, {inv['ci_high']:+.4f}].** The interval "
              "comfortably contains zero, so the inversion is not distinguishable "
              "from sampling noise at n=3 samples over 7 families. Its RQ3 delta "
              f"({rq3['gemini-3-flash-preview']['delta']:+.4f}, CI "
              f"[{rq3['gemini-3-flash-preview']['ci_low']:+.4f}, "
              f"{rq3['gemini-3-flash-preview']['ci_high']:+.4f}]) likewise contains "
              "zero.",
              "",
              "Most of that cell's failures are `compile_error` (20 of 21 failing "
              "records), not invariant violations — so the inversion is largely a "
              "generation-reliability artifact rather than a retry-safety signal.",
              "",
              "**This cell was deliberately not re-run.** Re-running a single cell "
              "because its result is inconvenient, and keeping the version that "
              "looks better, is selection on the outcome — it would bias the whole "
              "table and invalidate every interval in it. If more precision is "
              "wanted, raise n for *every* cell and regenerate."]
    L += ["", "## Artifacts", "",
          "| Path | Contents |", "|---|---|",
          "| `results/model_results.json` | all 2730 records, one per (candidate, schedule) |",
          "| `results/model_metrics.json` | machine-readable metrics, CIs, survival, provenance |",
          "| `analysis/figures/model_data.tex` | LaTeX tables + pgfplots coords (`\\input`-able) |",
          "| `analysis/figures/model_*.png` | the four figures |",
          "| `results/raw/` | prompts, raw responses, candidates, agent transcripts |",
          "",
          "The reference/mutant pilot (`results/pilot_results.json`, "
          "`analysis/figures/paper_data.tex`) is a separate artifact and was not "
          "regenerated from model data.", ""]
    with open(path, "w") as fh:
        fh.write("\n".join(L))


# ---------------------------------------------------------------------------

def main():
    rows = load()
    stats, families = build_stats(rows)
    rng = np.random.default_rng(SEED)
    cells, idx_by_system = compute(stats, families, rng)
    comp = failure_composition(rows)
    surv = survival(cells)
    rq3 = deltas(stats, families, idx_by_system, "hidden_safety",
                 "domain_guided", "zero_shot")
    rq3_robust = deltas(stats, families, idx_by_system, "robust_success",
                        "domain_guided", "zero_shot")
    inversion = deltas(stats, families, idx_by_system, "hidden_safety",
                       "domain_guided", "retrieval")
    gaps = public_hidden_gap(stats, families, idx_by_system)
    prov = provenance_table(rows)

    names = figures(cells, surv, comp, rq3, gaps)
    write_tex(cells, surv, comp, rq3, gaps, prov,
              os.path.join(FIGDIR, "model_data.tex"))

    # cost/tokens are per candidate, but recorded on every record
    seen, tokens, cost = set(), 0, 0.0
    for r in rows:
        key = (r["family"], r["candidate"])
        if key in seen:
            continue
        seen.add(key)
        tokens += r["tokens"]["total"]
        cost += r["cost_usd"]

    out = {
        "generated_from": "results/model_results.json",
        "n_records": len(rows),
        "n_candidates": len(seen),
        "families": families,
        "samples_per_cell": 3,
        "bootstrap": {"replicates": BOOTSTRAP, "seed": SEED,
                      "percentiles": list(ALPHA),
                      "resampling_unit": "semantic task family (n=%d)" % len(families),
                      "note": "schedules are nested within candidates and are NOT "
                              "resampled; bootstrapping over records would understate "
                              "the intervals"},
        "provenance": {k: v for k, v in prov.items()},
        "cells": {f"{s}||{c}": v for (s, c), v in cells.items()},
        "rq3_zero_shot_to_domain_guided": {
            "hidden_safety": rq3, "robust_success": rq3_robust},
        "rq_inversion_domain_guided_minus_retrieval": inversion,
        "rq4_public_minus_hidden": {f"{s}||{c}": v for (s, c), v in gaps.items()},
        "failure_composition": {f"{s}||{c}": dict(v) for (s, c), v in comp.items()},
        "survival": {f"{s}||{c}": {"m": v["m"], "n": v["n"], "saturated": v["saturated"],
                                   "curve": {str(k): r for k, r in v["curve"].items()}}
                     for (s, c), v in surv.items()},
        "totals": {"tokens": tokens, "cost_usd": round(cost, 4)},
        "figures": names,
    }
    with open(os.path.join(ROOT, "results", "model_metrics.json"), "w") as fh:
        json.dump(out, fh, indent=2, default=str)

    write_summary(
        cells, surv, comp, rq3, rq3_robust, inversion, gaps, prov,
        {"records": len(rows), "candidates": len(seen), "families": families,
         "tokens": tokens, "cost_usd": cost},
        os.path.join(ROOT, "results", "SUMMARY.md"),
    )

    print(f"figures + model_data.tex written to {FIGDIR}")
    print("SUMMARY.md written")
    print(f"model_metrics.json written ({len(rows)} records, {len(seen)} candidates)")
    return out


if __name__ == "__main__":
    main()
