# Plugging an LLM / agent into T4

This directory holds the four **generation conditions** used in the paper and a
recipe for wiring a model's output back into the harness as a new candidate.
Nothing here runs a model for you — that is the documented next step (see
`CODING_AGENT_PROMPTS.md`, prompt P5). What is here is a stable contract: the
prompts, and the exact shape a generated candidate must have to be scored by the
existing deterministic harness.

## The four conditions

| File | Condition | What the model is given |
|------|-----------|-------------------------|
| `prompts/zero_shot.md` | zero-shot | interface + injected API + hard rules + task |
| `prompts/retrieval.md` | retrieval-augmented | above + generic retrieved idempotency/exactly-once passages |
| `prompts/agentic.md` | agentic | a repo checkout + shell; iterates against **public** schedules and tests |
| `prompts/domain_guided.md` | domain-guided | above (zero-shot) + an explicit retry-safety checklist |

Each prompt is a fill-in template with `{{PLACEHOLDERS}}` drawn from a task
manifest (`task-schema/task.schema.json`). The worked example throughout is the
`capture` family, so the templates are directly runnable against
`tasks/capture/`.

## How scoring works (so you know what to generate)

The harness never inspects the model's chain of thought or its internal status.
It scores an **independent observation** — the provider effects log, the durable
store, and the observable responses — against a per-schedule `harness.Spec`
using the shared oracle `harness.CheckFinancial`. So all you have to produce is
a candidate that implements the family's `Service` interface via its `Factory`.
Reliability is reported as survival `R_k` over the hidden schedules (the
probability of surviving `k` independently-sampled hidden schedules), not
`pass@k`.

## Wiring a generated candidate back in

Each family exposes a `Factory` type — `func(env harness.Env) Service` — and a
`TaskFor(Factory) harness.Task` binder (see `tasks/capture/capture.go` and
`tasks/capture/cases.go`). To score model output:

1. **Save the model's file** into the family package, e.g.
   `tasks/capture/candidate_zeroshot.go`, in `package capture`. It must define a
   factory, e.g. `func CandidateZeroShot(env harness.Env) Service`. Do not edit
   `harness/`, `go.mod`, `cmd/`, the family's `*_test.go`, or other families.

2. **Score it against the schedules** with the existing binder and oracle. A
   minimal driver (put it in a throwaway `_test.go` or a small `cmd/`):

   ```go
   for _, c := range capture.Cases() { // or HiddenCases() for scoring only
       obs := harness.Run(c.Sch, capture.TaskFor(capture.CandidateZeroShot))
       res := harness.CheckFinancial(obs, c.Exp)
       // record harness.NewResult(...) exactly as tasks/capture/run.go does
   }
   ```

   The result records match `task-schema/result.schema.json`; append them to the
   pilot output and regenerate figures with `make figures`.

3. **Iterate only against public schedules** for the agentic condition. The
   hidden schedules (`c.Sch.Hidden == true`, or `HiddenCases()`) must never be
   read by the model or its harness during generation — that is the hidden-set
   commitment (see `ARTIFACT_EVALUATION.md`).

## What the full runner adds (next step, not shipped)

The paper's generation runner (P5 in `CODING_AGENT_PROMPTS.md`) wraps the above
with: model id + provider snapshot, temperature, sample count, the exact prompt
used, token cost, and run date logged per candidate; archival of every raw model
output; a compile gate; and per-condition aggregation into the same
`harness.Result` records this repo already understands. Because the harness is
deterministic, only *generation* is stochastic — scoring a saved candidate is
fully reproducible.
