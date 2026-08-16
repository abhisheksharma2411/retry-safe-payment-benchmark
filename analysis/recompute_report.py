#!/usr/bin/env python3
"""Plain-text recomputation of the model-evaluation metrics.

Reads results/model_results.json and results/pilot_results.json. Reads only —
no raw data is written, modified, or re-run. The only outputs are stdout and
analysis/figures/model_data.tex.

The unit of analysis throughout is the **generated program**: one candidate for
one (system, condition, family, sample). A program has 4 public and 6 hidden
schedule-executions. Metrics that describe a program (compile, functional,
robust) are counted over programs; metrics that describe behaviour under fault
(public/hidden safety) are counted over schedule-executions, and the two are
never mixed into one number.
"""
import glob
import json
import math
import os
import sys
from collections import defaultdict
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_stats import survival_curve, survival_rk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIGDIR = os.path.join(HERE, "figures")
N_HIDDEN = 6
N_PUBLIC = 4

# The five curves the paper plots, and the macro name each must carry.
CURVES = [
    ("svAgentic", "claude-opus-4-6[1m]", "agentic", "Claude Code agent"),
    ("svProDG", "gemini-3.1-pro-preview", "domain_guided", "gemini-3.1-pro domain-guided"),
    ("svFlashLatestZS", "gemini-3.6-flash", "zero_shot", "gemini-3.6-flash zero-shot"),
    ("svFlashPrevZS", "gemini-3-flash-preview", "zero_shot", "gemini-3-flash-preview zero-shot"),
    ("svProZS", "gemini-3.1-pro-preview", "zero_shot", "gemini-3.1-pro zero-shot"),
]


def frac(num, den):
    """'a/b (0.dddd)' — exact counts alongside the decimal, never one alone."""
    if den == 0:
        return "0/0 (n/a)"
    return f"{num}/{den} ({num / den:.4f})"


def rule(ch="-", n=100):
    return ch * n


def load(name):
    with open(os.path.join(ROOT, "results", name)) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------

class Program:
    """One generated candidate and its 10 schedule-executions."""

    __slots__ = ("system", "condition", "family", "candidate", "requested",
                 "compiled", "hidden", "public", "hidden_ok", "public_ok", "happy_ok")

    def __init__(self, key):
        self.system, self.condition, self.family, self.candidate = key
        self.requested = set()
        self.compiled = False
        self.hidden = self.public = self.hidden_ok = self.public_ok = 0
        self.happy_ok = False

    @property
    def functional(self):
        return self.happy_ok

    @property
    def robust(self):
        return self.hidden > 0 and self.hidden_ok == self.hidden


def build_programs(rows):
    progs = {}
    for r in rows:
        key = (r["resolved_model"], r["condition"], r["family"], r["candidate"])
        p = progs.get(key)
        if p is None:
            p = progs[key] = Program(key)
        p.requested.add((r.get("provenance") or {}).get("requested_model") or r["model_id"])
        p.compiled = r["compile_status"] == "success"
        if r["hidden"]:
            p.hidden += 1
            p.hidden_ok += 1 if r["ok"] else 0
        else:
            p.public += 1
            p.public_ok += 1 if r["ok"] else 0
            if r["schedule_id"].endswith("-happy") and r["ok"]:
                p.happy_ok = True
    return list(progs.values())


def by_cell(progs):
    cells = defaultdict(list)
    for p in progs:
        cells[(p.system, p.condition)].append(p)
    return cells


CONDITION_ORDER = ["zero_shot", "retrieval", "domain_guided", "agentic"]


def cell_sort(key):
    return (key[0], CONDITION_ORDER.index(key[1]))


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def section_survival(cells, out):
    out.append(rule("="))
    out.append("1) SURVIVAL R_k  --  PER-PROGRAM AVERAGE")
    out.append(rule("="))
    out.append("")
    out.append("  R_k = (1/J) * SUM_j  C(m_j,k) / C(6,k)")
    out.append("")
    out.append("  m_j = hidden schedules passed by program j (of 6); J = programs in the cell.")
    out.append("  Averaged across programs, NOT pooled. Pooling forms C(sum m, k)/C(sum n, k),")
    out.append("  which treats runs of different programs as interchangeable draws and answers")
    out.append("  a question nobody asked; it also collapses the tail (see the last column).")
    out.append("")

    hdr = (f"  {'system':<24} {'condition':<14} {'J':>4} " +
           " ".join(f"{'R_' + str(k):>8}" for k in range(1, 7)) + f" {'pooled R_6':>11}")
    out.append(hdr)
    out.append("  " + rule("-", len(hdr) - 2))
    for key in sorted(cells, key=cell_sort):
        ps = cells[key]
        ms = [p.hidden_ok for p in ps]
        curve = survival_curve(ms, N_HIDDEN)
        pooled = survival_rk(sum(ms), N_HIDDEN * len(ms), 6)
        out.append(f"  {key[0]:<24} {key[1]:<14} {len(ps):>4} " +
                   " ".join(f"{curve[k]:>8.4f}" for k in range(1, 7)) +
                   f" {pooled:>11.4f}")
    out.append("")

    out.append("  IDENTITY CHECKS (must hold exactly):")
    out.append(f"    {'cell':<40} {'R_1':>9} {'mean hidden':>12}   {'R_6':>9} {'robust':>9}  ok")
    out.append("  " + rule("-", 96))
    all_ok = True
    for key in sorted(cells, key=cell_sort):
        ps = cells[key]
        ms = [p.hidden_ok for p in ps]
        curve = survival_curve(ms, N_HIDDEN)
        mean_hidden = sum(ms) / (N_HIDDEN * len(ms))
        robust = sum(1 for p in ps if p.robust) / len(ps)
        ok = (abs(curve[1] - mean_hidden) < 1e-12) and (abs(curve[6] - robust) < 1e-12)
        all_ok &= ok
        out.append(f"    {key[0] + ' / ' + key[1]:<40} {curve[1]:>9.4f} {mean_hidden:>12.4f}   "
                   f"{curve[6]:>9.4f} {robust:>9.4f}  {'OK' if ok else 'FAIL'}")
    out.append("")
    out.append(f"  ALL IDENTITIES HOLD: {all_ok}")
    out.append("")
    return all_ok


def survival_macros(cells):
    """(macro, label, J, coord string, curve) for the five named curves."""
    out = []
    for macro, system, cond, label in CURVES:
        ps = cells.get((system, cond), [])
        ms = [p.hidden_ok for p in ps]
        curve = survival_curve(ms, N_HIDDEN)
        coords = " ".join(f"({k},{curve[k]:.4f})" for k in range(1, 7))
        out.append((macro, label, system, cond, len(ps), coords, curve))
    return out


def section_macros(macros, out):
    out.append(rule("="))
    out.append("   pgfplots coordinate strings (also written to analysis/figures/model_data.tex)")
    out.append(rule("="))
    out.append("")
    for macro, label, system, cond, j, coords, _curve in macros:
        out.append(f"  % {label}  --  {system} / {cond}, J={j} programs")
        out.append(f"  \\def\\{macro}{{{coords}}}")
    out.append("")


def section_samples(cells, out):
    out.append(rule("="))
    out.append("3) SAMPLE SIZES")
    out.append(rule("="))
    out.append("")
    out.append("  A 'program' is one generated candidate: (system, condition, family, sample).")
    out.append("  gemini-3.1-pro-preview is the SERVED snapshot for two requested model ids,")
    out.append("  so its cells pool both and carry double the programs of the other systems.")
    out.append("")
    hdr = (f"  {'system (served)':<24} {'condition':<14} {'requested ids pooled':<44} "
           f"{'fam':>4} {'gen/task':>9} {'programs':>9}")
    out.append(hdr)
    out.append("  " + rule("-", len(hdr) - 2))
    for key in sorted(cells, key=cell_sort):
        ps = cells[key]
        reqs = sorted({r for p in ps for r in p.requested})
        fams = len({p.family for p in ps})
        out.append(f"  {key[0]:<24} {key[1]:<14} {', '.join(reqs):<44} "
                   f"{fams:>4} {len(ps) // fams:>9} {len(ps):>9}")
    out.append("")
    out.append("  Every program contributes exactly 4 public + 6 hidden schedule-executions.")
    out.append("")


def section_table(cells, out):
    out.append(rule("="))
    out.append("2) MAIN TABLE  --  hidden set reported separately from public; no combined number")
    out.append(rule("="))
    out.append("")
    out.append("  Denominators differ by row on purpose:")
    out.append("    compile / functional / robust  are per PROGRAM        (J)")
    out.append("    public safety                  is per PUBLIC exec     (4J)")
    out.append("    hidden safety                  is per HIDDEN exec     (6J)")
    out.append("    hidden | functional            is per HIDDEN exec of functional programs")
    out.append("")
    for key in sorted(cells, key=cell_sort):
        ps = cells[key]
        j = len(ps)
        fn = [p for p in ps if p.functional]
        out.append(f"  {key[0]}  /  {key[1]}    (J = {j} programs)")
        out.append(f"    {'compile rate':<32} {frac(sum(1 for p in ps if p.compiled), j)}")
        out.append(f"    {'functional rate (happy sched)':<32} {frac(len(fn), j)}")
        out.append(f"    {'public safety':<32} "
                   f"{frac(sum(p.public_ok for p in ps), sum(p.public for p in ps))}")
        out.append(f"    {'hidden safety':<32} "
                   f"{frac(sum(p.hidden_ok for p in ps), sum(p.hidden for p in ps))}")
        out.append(f"    {'hidden safety | functional':<32} "
                   f"{frac(sum(p.hidden_ok for p in fn), sum(p.hidden for p in fn))}")
        out.append(f"    {'robust (all 6 hidden)':<32} "
                   f"{frac(sum(1 for p in ps if p.robust), j)}")
        out.append("")


def section_counts(rows, progs, cells, pilot, out):
    out.append(rule("="))
    out.append("4) EXACT COUNTS")
    out.append(rule("="))
    out.append("")

    # -- oracle study -----------------------------------------------------
    out.append("  (a) ORACLE STUDY  --  results/pilot_results.json (references + mutants)")
    out.append("")
    n = len(pilot)
    ok = sum(1 for r in pilot if r["ok"])
    out.append(f"      total runs                       {n}")
    out.append(f"      unconditional safety             {frac(ok, n)}")

    happy = [r for r in pilot if r["schedule_id"].endswith("-happy")]
    functional = {(r["family"], r["candidate"]) for r in happy if r["ok"]}
    cond = [r for r in pilot if r["hidden"] and (r["family"], r["candidate"]) in functional]
    cond_ok = sum(1 for r in cond if r["ok"])
    all_c = {(r["family"], r["candidate"]) for r in pilot}
    out.append(f"      functional candidates            {frac(len(functional), len(all_c))}"
               f"   (pass the no-fault schedule)")
    out.append(f"      safety | functional (hidden)     {frac(cond_ok, len(cond))}")
    out.append("")
    out.append(f"      Note the two denominators: {ok / n:.4f} is over ALL {n} runs (public+hidden,")
    out.append(f"      every candidate); {cond_ok / len(cond):.4f} is over the {len(cond)} HIDDEN runs of functional")
    out.append("      candidates only. They are not comparable and neither is a subset-rate")
    out.append("      of the other.")
    out.append("")
    nonfunc = sorted(all_c - functional)
    out.append(f"      non-functional candidates ({len(nonfunc)}): "
               f"{', '.join(sorted({c for _f, c in nonfunc}))}")
    out.append("")

    # -- lost effect ------------------------------------------------------
    out.append("  (b) no_lost_effect FAILURES  --  results/model_results.json")
    out.append("")
    le_rows = [r for r in rows if not r["ok"] and "no_lost_effect" in (r["violations"] or [])]
    le_progs = {(r["resolved_model"], r["condition"], r["family"], r["candidate"]) for r in le_rows}
    le_fams = {r["family"] for r in le_rows}
    all_fams = {r["family"] for r in rows}
    out.append(f"      schedule-executions failing it   {frac(len(le_rows), len(rows))}")
    out.append(f"      programs affected                {frac(len(le_progs), len(progs))}")
    out.append(f"      families affected                {frac(len(le_fams), len(all_fams))}"
               f"   ({', '.join(sorted(le_fams))})")
    hid = [r for r in le_rows if r["hidden"]]
    out.append(f"      of those executions, hidden      {frac(len(hid), len(le_rows))}")
    out.append("")
    out.append(f"      {'by (system, condition)':<46} {'execs':>7} {'programs':>9}")
    out.append("      " + rule("-", 64))
    per = defaultdict(lambda: [0, set()])
    for r in le_rows:
        e = per[(r["resolved_model"], r["condition"])]
        e[0] += 1
        e[1].add((r["family"], r["candidate"]))
    for k in sorted(per, key=cell_sort):
        out.append(f"      {k[0] + ' / ' + k[1]:<46} {per[k][0]:>7} {len(per[k][1]):>9}")
    out.append("")
    out.append(f"      {'by family':<46} {'execs':>7} {'programs':>9}")
    out.append("      " + rule("-", 64))
    perf = defaultdict(lambda: [0, set()])
    for r in le_rows:
        e = perf[r["family"]]
        e[0] += 1
        e[1].add((r["resolved_model"], r["condition"], r["candidate"]))
    for k in sorted(perf):
        out.append(f"      {k:<46} {perf[k][0]:>7} {len(perf[k][1]):>9}")
    out.append("")

    # -- pro zero-shot compile failures -----------------------------------
    out.append("  (c) gemini-3.1-pro-preview / zero_shot COMPILE FAILURES")
    out.append("")
    ps = cells[("gemini-3.1-pro-preview", "zero_shot")]
    failed = [p for p in ps if not p.compiled]
    out.append(f"      programs failing to compile      {frac(len(failed), len(ps))}")
    out.append("")
    reasons = classify_compile_errors(rows)
    if reasons:
        out.append(f"      {'reason category':<40} {'programs':>9}   example")
        out.append("      " + rule("-", 92))
        for cat, items in sorted(reasons.items(), key=lambda x: -len(x[1])):
            out.append(f"      {cat:<40} {len(items):>9}   {items[0][1][:40]}")
        out.append("")
        out.append(f"      {'per family':<40} {'programs':>9}   reasons")
        out.append("      " + rule("-", 92))
        byfam = defaultdict(list)
        for cat, items in reasons.items():
            for fam, _msg in items:
                byfam[fam].append(cat)
        for fam in sorted(byfam):
            out.append(f"      {fam:<40} {len(byfam[fam]):>9}   "
                       f"{', '.join(sorted(set(byfam[fam])))}")
    else:
        out.append("      (no compile_error.txt files recoverable under results/raw/)")
    out.append("")


def classify_compile_errors(rows):
    """Reason categories for Pro zero-shot compile failures, from results/raw.

    Matched back to the run directories by reading each meta.json, because a
    served snapshot can come from more than one requested model id and therefore
    from more than one run directory.
    """
    wanted = set()
    for r in rows:
        if r["resolved_model"] == "gemini-3.1-pro-preview" and r["condition"] == "zero_shot":
            wanted.add(r["run_id"])
    reasons = defaultdict(list)
    for meta_path in glob.glob(os.path.join(ROOT, "results", "raw", "*", "*", "*", "*", "meta.json")):
        parts = meta_path.split(os.sep)
        run_id, family, condition = parts[-5], parts[-4], parts[-3]
        if run_id not in wanted or condition != "zero_shot":
            continue
        err_path = os.path.join(os.path.dirname(meta_path), "compile_error.txt")
        if not os.path.exists(err_path):
            continue
        with open(err_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        reasons[categorise(text)].append((family, first_error_line(text)))
    return reasons


def first_error_line(text):
    for line in text.splitlines():
        if ".go:" in line:
            return line.split(": ", 1)[-1].strip()
    return text.strip().splitlines()[0] if text.strip() else ""


def categorise(text):
    low = text.lower()
    # Not a compiler diagnostic: the runner never got as far as building, because
    # the response carried no `func Name(env harness.Env) Service`. Recorded as
    # compile_status=error like any other build failure, but it is a
    # contract-compliance failure rather than a Go error, so it gets its own row.
    if "no factory matching" in low:
        return "no factory (output-contract violation)"
    if "returned no go source" in low:
        return "no Go source extracted"
    if "redeclared in this block" in low:
        return "redeclared Request/Service"
    if "declared and not used" in low or "imported and not used" in low:
        return "unused declaration"
    if "syntax error" in low:
        return "syntax error (malformed/truncated output)"
    if "has no field or method" in low:
        return "method not on injected interface"
    if "undefined:" in low:
        return "undefined identifier"
    if "cannot use" in low or "mismatched types" in low or "as type" in low:
        return "type error"
    if "missing return" in low:
        return "missing return"
    return "other"


# ---------------------------------------------------------------------------

def write_tex(macros, cells, path):
    L = [
        "% Auto-generated by analysis/recompute_report.py -- measured model evaluation.",
        "% Do not edit by hand.",
        "%",
        "% SURVIVAL R_k IS A PER-PROGRAM AVERAGE:",
        "%     R_k = (1/J) * sum_j C(m_j,k)/C(6,k)",
        "% where m_j is the hidden schedules passed by program j (of 6) and J is the",
        "% number of programs in the cell. It is NOT pooled across programs: pooling",
        "% forms C(sum m, k)/C(sum n, k), treats runs of different programs as",
        "% interchangeable draws, and collapses the tail (Pro zero-shot R_6 = 0.224",
        "% pooled vs 0.524 correct).",
        "%",
        "% Identities, asserted at generation time:",
        "%     R_1 == mean hidden pass rate",
        "%     R_6 == robust-success rate (fraction passing all 6 hidden schedules)",
        "%",
        "% Coordinates are (k, R_k) for k = 1..6.",
        "%",
    ]
    for macro, label, system, cond, j, coords, curve in macros:
        L.append(f"% {label} -- {system} / {cond}, J={j} programs, "
                 f"R_1={curve[1]:.4f}, R_6={curve[6]:.4f}")
        L.append(f"\\def\\{macro}{{{coords}}}")
    L.append("%")
    L.append("% Programs per condition (Pro pools two requested ids onto one served snapshot).")
    for key in sorted(cells, key=cell_sort):
        L.append(f"%   {key[0]:<24} {key[1]:<14} J={len(cells[key])}")
    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")


def main():
    rows = load("model_results.json")
    pilot = load("pilot_results.json")
    progs = build_programs(rows)
    cells = by_cell(progs)

    out = []
    out.append(rule("="))
    out.append("T4 MODEL EVALUATION -- RECOMPUTED METRICS")
    out.append(rule("="))
    out.append("")
    out.append(f"  model_results.json : {len(rows)} schedule-executions, {len(progs)} programs")
    out.append(f"  pilot_results.json : {len(pilot)} schedule-executions (oracle study)")
    out.append("  raw data unchanged; aggregation only")
    out.append("")

    ok = section_survival(cells, out)
    macros = survival_macros(cells)
    section_macros(macros, out)
    section_table(cells, out)
    section_samples(cells, out)
    section_counts(rows, progs, cells, pilot, out)

    write_tex(macros, cells, os.path.join(FIGDIR, "model_data.tex"))
    out.append(rule("="))
    out.append(f"wrote {os.path.relpath(os.path.join(FIGDIR, 'model_data.tex'), ROOT)}")
    out.append(rule("="))
    print("\n".join(out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
