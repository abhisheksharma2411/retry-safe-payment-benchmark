# Artifact evaluation

This document is for IEEE/ACM artifact reviewers. It states the requirements,
the `make` targets and what each one demonstrates, expected runtime and outputs,
the oracle-validity claim, and the hidden-set commitment protocol.

## Badges this artifact targets

- **Available** — public, licensed (Apache-2.0), with citation metadata
  (`CITATION.cff`).
- **Functional / Reusable** — builds and runs from source; every claim in the
  paper's pilot is regenerated from a single command; the family-authoring guide
  and JSON schemas make the contract reusable.

We do **not** claim a "Results Reproduced" badge for an LLM evaluation: no LLM is
evaluated in this artifact. The reproducible results here concern the harness,
the oracle, and the pilot over the shipped references/mutants. The LLM study is
documented future work (`docs/DEVELOPMENT_ORDER.md`, `CODING_AGENT_PROMPTS.md`).

## Requirements

Either a local toolchain or Docker.

- **Local:** Go 1.24 (standard library only; no third-party Go dependencies) and
  Python 3 with `matplotlib` and `numpy`. Optional: `jsonschema` to validate the
  committed results against `task-schema/`.
- **Docker:** `docker build -t t4-benchmark . && docker run --rm t4-benchmark`.
  The image pins `golang:1.24-bookworm` and installs the Python plotting stack;
  its default command is `make reproduce-small`.

No network access is required at run time.

## Targets and what each proves

| Target | What it runs | What it demonstrates |
|--------|--------------|----------------------|
| `make smoke` | `go build ./...` + `go test ./harness/` | The module compiles and the kernel/oracle tests pass. |
| `make test-harness` | `go test ./harness/ -v` | Kernel scheduling, fault injection, oracle math, and the trace shrinker behave as specified. |
| `make test-references` | `go test ./tasks/... -run TestCorrectReferencePassesAll -v` | Every family's **correct reference passes every schedule** (oracle validity, no false positives). |
| `make test-mutants` | `go test ./tasks/... -run TestMutantsAreCaught -v` | **Every seeded bug is caught by its intended invariant** (oracle validity, no false negatives). |
| `make test-determinism` | `go test ./tasks/... -run TestDeterminism` | A given `(candidate, schedule, seed)` yields identical traces/observations across runs (determinism contract). |
| `make test` | `go test ./...` | The full suite (all of the above). |
| `make pilot` | `go run ./cmd/t4run` | Executes every candidate × schedule, writes `results/pilot_results.json`, prints the per-candidate survival `R_k` table and a trace-shrinking demo. |
| `make figures` | `python3 analysis/make_figures.py` | Regenerates `analysis/figures/*.png`, `results/metrics_summary.json`, `results/metrics.csv`, and `analysis/figures/paper_data.tex` from the raw records. |
| `make reproduce-small` | `test` + `pilot` + `figures` | The end-to-end pilot pipeline (the main reviewer entry point). |
| `make reproduce-paper` | `reproduce-small`, then prints `metrics_summary.json` | Same pipeline; also echoes the headline numbers used in the paper. |
| `make manifest` | SHA-256 of `results/pilot_results.json` | The integrity digest underpinning the hidden-set commitment. |

## Expected runtime

On a commodity laptop the entire pipeline is fast: `make smoke` completes in a
few seconds, and `make reproduce-small` (full tests + pilot + figures) in well
under a minute. The Docker build is dominated by the base-image pull and the
apt install of the Python stack.

## Expected outputs

- `results/pilot_results.json` — **420 result records** (7 families × 6
  candidates × 10 schedules), each matching `task-schema/result.schema.json`.
- `results/metrics_summary.json` — headline numbers. Key values on the reference
  toolchain: `n_result_records = 420`, `compile_rate = 1.0`,
  `functional_pass_rate_happy = 0.8333`, `unconditional_safety_rate = 0.7`,
  `safety_given_functional_hidden = 0.8333`, and the correct reference's hidden
  survival `R₁ = 1.0`.
- `results/metrics.csv` — per-candidate-type survival table.
- `analysis/figures/survival.png`, `analysis/figures/failure_composition.png`,
  `analysis/figures/paper_data.tex` — the paper figures and pgfplots data.

Because the harness is deterministic, the **data** artifacts —
`pilot_results.json`, `metrics_summary.json`, `metrics.csv`, `paper_data.tex` —
regenerate **byte-for-byte** on the same toolchain. The figure PNGs regenerate
**pixel-for-pixel** (every rendered pixel matches) but are **not byte-identical
across platforms**: matplotlib stamps its version into a `tEXt` chunk and
libpng/zlib builds differ in compression details, so the same figure from macOS
and from Linux differs in container bytes only. Compare decoded pixels, not file
hashes; or build the `Dockerfile` for a byte-stable image.

## Validating the committed records against the schemas (optional)

```sh
python3 - <<'PY'
import json, jsonschema
schema = json.load(open("task-schema/result.schema.json"))
data = json.load(open("results/pilot_results.json"))
jsonschema.validate(data, schema)          # validates the whole array
print("OK:", len(data), "records conform to result.schema.json")
PY
```

## Oracle-validity claim

The pilot is designed so that oracle validity is checkable, not asserted:

1. **No false positives.** Every family's correct reference passes all of its
   schedules — public and hidden (`make test-references`; in the pilot, the
   correct reference's hidden survival `R_k = 1.0` for all `k`).
2. **No false negatives, correctly attributed.** Each of the five seeded-bug
   mutants per family is caught, and is caught by the *specific* invariant it was
   designed to violate (`make test-mutants`). The failure-composition figure
   shows each mutant type failing via its intended invariant.
3. **Determinism.** Repeated runs of a `(candidate, schedule, seed)` triple
   produce identical traces and observations (`make test-determinism`), so the
   pilot numbers are exactly reproducible.

Together these establish that the measurement apparatus separates retry-safe
from retry-unsafe code for reasons the oracle can name — a prerequisite for the
LLM evaluation in future work.

## Hidden-set commitment (freeze-then-reveal)

Scoring is done on **hidden** fault schedules so that candidates (including, in
future work, LLM-generated ones and agents iterating in a checkout) cannot
overfit to the scoring set. In this artifact the hidden schedules are marked in
code (`Schedule.Hidden == true`; `HiddenCases()` per family) and are visible to
reviewers, because there is no adversary yet — the shipped candidates are fixed
references and mutants.

The intended protocol for the v1.0 release and the LLM study is:

1. **Freeze.** Serialize the hidden schedule set (and the correct-reference
   source hashes) and record a **SHA-256 manifest**. `make manifest` shows the
   digest form; the release process signs the manifest and publishes only the
   digest, withholding the hidden schedules themselves.
2. **Generate under the barrier.** Models/agents are given only the public
   schedules; the generation runner enforces that the hidden set is never read
   during generation (`generation/README.md`). For an API model the runner holds
   the barrier by only ever executing `PublicCases()` during iteration. For a
   **coding agent with a shell**, which cannot merely be asked, each run is
   confined to a generated scaffold containing the family interface, the
   injected-environment API, and the public schedules — and not `cases.go`,
   `harness/oracle.go`, the correct reference, or the mutants. Every session
   transcript is then audited for the withheld strings and archived with the
   result (`docs/EVAL_RUNNER.md`).
3. **Reveal.** After results are recorded, the hidden schedules and the signed
   manifest are published so anyone can verify that the scoring set matches the
   committed digest and was not altered to fit the results.

This freeze-then-reveal discipline is what lets the survival `R_k` numbers be
interpreted as evidence about held-out robustness rather than memorized behavior.
