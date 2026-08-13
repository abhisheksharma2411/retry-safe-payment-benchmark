# Coding-agent prompts: taking T4 from pilot to full paper

This file is a sequence of copy-paste-ready prompts for a coding agent (Claude
Code or similar) working **inside this repository**. They take the current
artifact — a working harness, 7 Tier-1 families, a validated oracle, and a
measured pilot — end-to-end to the full paper described in
`docs/DEVELOPMENT_ORDER.md` (milestones M3-M7).

Ground rules that apply to every prompt below (state them to the agent):

- The Go module is `t4bench` (Go 1.24, standard library only). Do **not** break
  the build or the determinism contract. `make smoke` and `make test` must stay
  green after every prompt.
- Never modify `harness/` semantics, `go.mod`, or a family's `*_test.go` unless
  the prompt explicitly says to. Add new code alongside the existing structure;
  `tasks/capture/` is the canonical template to mirror.
- Candidates may use ONLY the injected `harness.Env` (no real
  clock/rand/goroutines/network/files/globals). Author a `harness.Spec` per
  schedule and reuse `harness.CheckFinancial`; never write a second oracle.
- Run the prompt's **acceptance check** and report its output before moving on.

---

## P1 — Freeze the benchmark contract into schemas + a coverage matrix

**Context.** The contract already exists implicitly in `harness/`,
`docs/FAMILY_AUTHORING_GUIDE.md`, and `task-schema/*.schema.json`, but nothing
emits a machine-checkable manifest per task or a coverage matrix tying families
× hazards × invariants together. Freeze it so later expansion can be validated
mechanically.

**Deliverables.**
- A `tasks/<family>/manifest.json` for each of the 7 families that validates
  against `task-schema/task.schema.json` (fill `identity`, `payload_fingerprint`,
  `expected_outcome`, `invariants`, `rail_profile`, `schedules`, `mutants`,
  `reference_hash`). Generate `reference_hash` as the SHA-256 of the frozen
  `<family>.go`.
- A small Go program `cmd/t4manifest/` (or a `_test.go`) that emits/verifies
  these manifests from the live `Cases()`/`Candidates()` so code and manifest
  cannot drift.
- `docs/COVERAGE_MATRIX.md`: a table of family × (hazard exercised) × (invariant
  scored), plus a per-invariant count, generated from the manifests.

**Acceptance check.** `python3 -c "import json,jsonschema; [jsonschema.validate(json.load(open(m)), json.load(open('task-schema/task.schema.json'))) for m in __import__('glob').glob('tasks/*/manifest.json')]; print('manifests valid')"`
and `make test` both pass; `docs/COVERAGE_MATRIX.md` shows all six invariants
covered by at least one family.

---

## P2 — Expand each family from Tier-1 to 3 tiers × 3 semantic variants

**Context.** Today each family is a single Tier-1 semantic task
(`capture-t1`, etc.) sharing the reserve-then-effect mechanism. The paper needs
**63 semantic tasks** (7 families × 3 tiers × 3 variants) and, with the Java
adapter from P3, **126 language-instances**. Variants must exercise a
**distinct hazard**, not a paraphrase of the same one — reject any variant whose
failing witness is identical to an existing task's.

**Distinct hazards to spread across the variants (each variant owns at least one
not already covered in its family):**
- crash after the effect commits but before completion is recorded
  (`drop_response` + `crash` + `recover`);
- unknown outcome from a post-commit timeout (`provider_timeout_after_commit`);
- pre-commit timeout with no effect (`provider_timeout_no_commit`);
- lost store acknowledgement on the reservation (`store_ack_lost`);
- concurrent duplicate retries racing on the same identity;
- false-dedup pressure (distinct identities sharing a raw caller key);
- changed-payload replay of an existing identity;
- out-of-order / redelivered messages (consumer-style);
- multi-step partial failure needing compensation (saga-style);
- reconcile-an-unknown-prior-effect via `Query` before acting;
- non-terminating recovery pressure (bounded-recovery stress);
- interleaved read-modify-write on a shared balance (ledger double-spend).

**Deliverables.**
- For each family, semantic tasks at Tier 1/2/3 with 3 variants each, mirroring
  the `tasks/capture/` file layout (`<family>_<tier>_<variant>.go`, extended
  `cases.go`, `run.go`, tests). Higher tiers **compound** hazards (e.g. crash +
  concurrent retry + changed payload in one schedule).
- Each new semantic task ships 1 correct reference + ≥3 seeded-bug mutants (each
  breaking exactly one invariant) and public + hidden schedules.
- Update `semantic_task_id`s and manifests (P1) accordingly.

**Acceptance check.** `make test` green; the pilot now emits the expected record
count; a new test asserts that no two semantic tasks share an identical minimal
failing witness (via `harness.Shrink`) for the same mutant class.

---

## P3 — Add a Java adapter with Go/Java semantic-parity tests

**Context.** The paper compares languages, so each semantic task needs a Java
instance whose meaning is identical to the Go one. The harness stays the source
of truth; Java candidates are scored through an adapter that presents the same
injected `Store`/`Provider`/`Clock`/`Rand` and the same schedule/oracle.

**Deliverables.**
- A `java/` module with a faithful port of the injected interfaces and a
  bridge (in-process JNI/exec/IPC — your choice, but deterministic and
  single-threaded) so a Java candidate runs against the Go kernel or a
  byte-for-byte Java re-implementation of it.
- Java correct references + mutants for each semantic task.
- Cross-language **semantic-parity tests**: for every `(semantic_task_id,
  schedule)`, the Go and Java correct references must produce the same oracle
  verdict and the same effect count, and each mutant must fail the same
  invariant in both languages.
- `language: java` instances added to the manifests (P1).

**Acceptance check.** A `make test-parity` target runs the parity suite and
passes; the manifest count reaches 126 language-instances once P2 is complete.

---

## P4 — Add rail profiles (TTL, weak, async) and the weak-rail impossibility case

**Context.** `harness` already models `StrongIdempotency`, `Queryable`, and
`WeakRail`, but the families only use `Queryable`. The paper argues that
retry-safety is **profile-relative** — and that on some profiles at-most-once is
impossible without extra assumptions. Make that concrete.

**Deliverables.**
- Extend the kernel's provider with a **TTL profile** (idempotency memory
  expires after N logical ticks) and an **async-ack profile** (the effect's
  acknowledgement can arrive after recovery has begun). Keep these purely
  deterministic and additive; do not change existing profile behavior.
- Per-family schedule variants that run under each profile.
- A documented **weak-rail impossibility case**: a schedule + spec under
  `WeakRail` for which no candidate can satisfy `at_most_one` and
  `no_lost_effect` simultaneously, with a test asserting even the correct
  reference cannot pass it, and prose in `docs/` explaining why (the benchmark's
  honesty check on its own assumptions).

**Acceptance check.** `make test` green; a test proves the impossibility case is
unpassable under `WeakRail` and becomes passable under `Queryable`; profile
coverage appears in `docs/COVERAGE_MATRIX.md`.

---

## P5 — Build the LLM/agent generation runner for the 4 conditions

**Context.** `generation/prompts/{zero_shot,retrieval,agentic,domain_guided}.md`
and `generation/README.md` define the conditions and the wiring contract, but no
code drives a model or archives outputs. Build the runner that turns a prompt +
model into scored `harness.Result` records, with full provenance.

**Deliverables.**
- `cmd/t4gen/` (Go) or `generation/runner/` (Python): for each
  `(family, semantic_task, condition, sample)` it renders the prompt from the
  manifest, calls the model, writes the raw output to
  `generation/outputs/<run_id>/...`, compiles it (compile gate), wires it in as a
  `Factory` exactly as `generation/README.md` describes, scores it against
  **hidden** schedules via the existing oracle, and appends records to a run
  JSON.
- A per-run `config.json` logging: model id, provider snapshot/date, temperature,
  sample count, the exact prompt text/hash per condition, token counts + cost,
  and wall-clock run date. Archive every raw completion (never overwrite).
- The **agentic** condition runs the model in a sandboxed checkout that exposes
  only public schedules/tests and blocks reads of `Hidden` material.

**Acceptance check.** A dry-run against a stub "model" (returns the shipped
correct reference) produces valid records matching
`task-schema/result.schema.json`, a complete `config.json`, and archived raw
outputs; `make figures` ingests the run without change.

---

## P6 — Scale the hidden schedule set with a coverage-saturation criterion, then freeze/sign

**Context.** Reliable survival `R_k` needs enough independent hidden schedules
that adding more stops changing verdicts. Grow the hidden set until it saturates
coverage, then freeze it under the commitment protocol in
`ARTIFACT_EVALUATION.md`.

**Deliverables.**
- A generator that enumerates hidden schedules per semantic task (varying
  fault type, injection point, interleaving, seed) and a **saturation
  criterion**: stop when the set of (mutant × invariant) verdicts and the
  shrunk-witness classes stop changing as schedules are added (report the
  saturation curve).
- The frozen hidden set serialized to disk, plus a **signed SHA-256 manifest**
  (extend `make manifest`) of the hidden schedules and reference hashes; publish
  only the digest until reveal.
- A verifier that recomputes the digest and checks the scoring set matches.

**Acceptance check.** `make manifest` emits the frozen digest; a test shows the
last K added schedules did not change any mutant's caught-invariant set
(saturation); the verifier round-trips the signed manifest.

---

## P7 — Run the pre-registered experiment (RQ1-RQ6) + hierarchical statistics

**Context.** With the runner (P5) and frozen hidden set (P6), execute the
pre-registered study and analyze it properly. Effects must be estimated with
uncertainty at the level of **semantic tasks** (the unit of generalization), not
individual runs, and comparisons across conditions must be paired.

**Deliverables.**
- `docs/PREREGISTRATION.md` stating RQ1-RQ6 (e.g. RQ1 compile vs. retry-safety
  gap; RQ2 effect of the domain checklist; RQ3 agentic iteration vs. single
  shot; RQ4 retrieval effect; RQ5 per-invariant failure profile; RQ6
  tier/hazard scaling), the metrics, and the analysis plan — committed before
  running.
- Execute P5 across all conditions/models against the P6 hidden set; archive raw
  outputs and records.
- `analysis/statistics.py`: **bootstrap confidence intervals over semantic
  tasks** for each metric; **paired** comparisons between conditions with
  **effect sizes**; and a **mixed-effects logistic model** of per-run pass with
  random effects for semantic task and family and fixed effects for
  condition/tier/profile. Emit a machine-readable stats summary.

**Acceptance check.** `python3 analysis/statistics.py` runs on the archived
records and writes `results/stats_summary.json` with CIs, paired effect sizes,
and model coefficients; numbers are deterministic given a fixed bootstrap seed.

---

## P8 — Regenerate all paper tables/figures from archived outputs

**Context.** The paper must be reproducible from archived artifacts alone, with
no live model calls. Extend `analysis/make_figures.py` (or add
`analysis/make_paper.py`) so every table and figure in the paper is emitted from
the P5/P7 archives.

**Deliverables.**
- One command that reads the archived run records + `results/stats_summary.json`
  and regenerates **every** paper figure (`analysis/figures/*.png`), the
  pgfplots data (`analysis/figures/paper_data.tex`), and every results table
  (LaTeX under `analysis/tables/`), including the survival curves, failure
  composition, and the per-condition comparison with CIs.
- A `make reproduce-paper` upgrade that runs the full archived pipeline
  (analysis → figures → tables) and diffs regenerated artifacts against the
  committed ones.

**Acceptance check.** `make reproduce-paper` regenerates all figures/tables and
reports **no numeric diff** against the committed artifacts; the paper builds
against `analysis/figures/paper_data.tex` and `analysis/tables/` with no
hand-edited numbers.
