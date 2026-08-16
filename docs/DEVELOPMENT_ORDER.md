# Development order (milestone plan)

This is the intended build order for T4, from the frozen contract to the full
paper. It records what is **done** in this artifact and what is **pending**, so a
reviewer (and a future coding agent) can see exactly where the line is. The
artifact deliberately ships a small, honest slice: a working harness, seven
Tier-1 families, a validated oracle, and a measured pilot. The full LLM
evaluation is the documented next step, not a completed result.

| Milestone | Scope | Status |
|-----------|-------|--------|
| **M1 — Spec freeze** | Fix the benchmark contract: injected `Env`/`Store`/`Provider`/`Clock`/`Rand` API, the identity model, the six financial-safety invariants, the rail profiles, and the schedule/event vocabulary. Capture it in `docs/FAMILY_AUTHORING_GUIDE.md` and `task-schema/`. | **DONE** |
| **M2 — Seven-task Go slice** | Implement seven Tier-1 families (`capture`, `refund`, `outbox`, `consumer`, `ledger`, `saga`, `reconciliation`), each with one correct reference, six seeded-bug mutants (one per financial property), public + hidden schedules, and the shared oracle. All family tests green; determinism enforced. | **DONE** |
| **M3 — Java parity** | Add a Java adapter and Java references/mutants for each semantic task, with Go/Java semantic-parity tests so a task means the same thing in both languages. | pending |
| **M4 — Tier expansion** | Expand each family from Tier-1 (single reserve-then-effect mechanism) to three tiers x three semantic variants, each variant exercising a *distinct* hazard rather than a paraphrase (target: 63 semantic tasks / 126 language-instances). | pending |
| **M5 — Pilot** | Run every shipped candidate x schedule through the harness, archive `results/pilot_results.json`, and regenerate measured figures/metrics. This validates the oracle (correct refs pass all; each mutant caught by its intended invariant) and the survival machinery on known-ground-truth candidates. | **DONE** |
| **M6 — Freeze v1.0** | Scale and freeze the hidden schedule set with a coverage-saturation criterion; publish a signed SHA-256 manifest of the hidden set and the reference hashes; tag v1.0. | pending |
| **M7 — Full eval + paper** | Run the pre-registered LLM/agent experiment across the four generation conditions and the research questions, add the hierarchical statistical analysis, regenerate all paper tables/figures from archived model outputs, and rewrite the paper around measured LLM results. | pending |

## What "DONE" means here

- The harness, the seven families, the oracle, and the pilot are complete,
  tested, and reproducible (`make reproduce-small`).
- The pilot's subjects are the **shipped** correct references and seeded-bug
  mutants — not LLM output. Its purpose is to demonstrate the measurement
  apparatus is sound (oracle validity + survival curves on known ground truth),
  which is a prerequisite for M7.

## What is explicitly NOT claimed yet

- No LLM has been evaluated in this artifact. M3, M4, M6, and M7 are future work.
- Tier-1 families share the reserve-then-effect mechanism by design; the
  cross-family view is uniform on purpose. Genuine difficulty scaling arrives
  with M4.
