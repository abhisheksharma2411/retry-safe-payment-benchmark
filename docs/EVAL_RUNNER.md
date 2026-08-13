# Model-evaluation runner

`generation/runner/` turns a live LLM into scored `harness.Result` records. For
every **(family × condition × sample)** it renders one of the four generation
prompts, asks the model to implement that family's `Service`, archives the raw
response, compiles the result, and — if it builds — scores it against the
family's own `Cases()` using the shipped oracle.

This is P5 of [`CODING_AGENT_PROMPTS.md`](../CODING_AGENT_PROMPTS.md). It adds no
Go dependencies: `harness/`, `tasks/`, `cmd/`, and `go.mod` are read but never
modified, and the module stays standard-library-only.

**Three providers are supported** — Anthropic, OpenAI, and Google Gemini — which
is what makes the four generation conditions worth measuring: the interesting
question is how retry-safety varies across models, not just across prompts. All
vendor-specific code is confined to
[`providers.py`](../generation/runner/providers.py); prompt rendering, the
compile gate, oracle scoring, and record emission are provider-blind.

## Prerequisites

| Need | Why |
|------|-----|
| Go 1.24 on `PATH` | the compile gate and the scoring driver |
| Python 3.9+ | the runner itself |
| `pip install -r generation/runner/requirements.txt` | the provider SDKs |
| An API key for the provider you target | see below |

Runner dependencies are **not** benchmark dependencies: `make reproduce-small`
needs none of them.

## Providers and environment variables

| `--provider` | API key env var | Default model | SDK |
|---|---|---|---|
| `anthropic` *(default)* | `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) | `claude-opus-5` | `anthropic` |
| `openai` | `OPENAI_API_KEY` | `gpt-5` | `openai` |
| `gemini` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | `gemini-3.1-pro-preview` | `google-genai` |

| Variable | Meaning |
|----------|---------|
| `T4_PROVIDER` | Default provider. Overridden by `--provider`. |
| `T4_MODEL_ID` | Default model. Overridden by `--model`. |

```sh
export GEMINI_API_KEY=...
python -m generation.runner --provider gemini --families capture --samples 1
```

Keys are read from the environment only — never passed on the command line, and
never written into any artifact.

**Pin the model for archival runs.** Aliases like `gemini-pro-latest` resolve to
whatever is current, which defeats reproducibility. Every record carries
`resolved_model` (the model the API says actually served the request) alongside
the requested `model_id`, so an alias run is still auditable after the fact.

## Running it

```sh
# The full sweep: 7 families x 4 conditions x 3 samples
python -m generation.runner --samples 3

# One family, one condition — start here
python -m generation.runner --families capture --conditions zero_shot --samples 1

# No API key needed:
python -m generation.runner --stub       # full pipeline against the shipped reference
python -m generation.runner --dry-run    # render + archive prompts only
```

Run from the repository root so `generation.runner` resolves.

### Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--provider` | `anthropic` | `anthropic` \| `openai` \| `gemini`. |
| `--model ID` | provider default | Model to evaluate. |
| `--samples N` | `3` | Samples per (family, condition). |
| `--temperature T` | *unset* | See the caveat below. |
| `--families` / `--conditions` | `all` | Comma-separated subsets. |
| `--agentic-iterations N` | `3` | Max revise cycles in the agentic condition. |
| `--max-tokens` / `--effort` | `32000` / `high` | Generation budget and reasoning depth. |
| `--price-in` / `--price-out` | list price | USD per 1M tokens, for `cost_usd`. |
| `--out PATH` | `results/model_results.json` | Records file. |
| `--fresh` | off | Overwrite the records file instead of appending. |
| `--stub` | off | Offline stub returning the shipped correct reference. |
| `--dry-run` | off | Render prompts, skip the model. |

### The `--temperature` caveat

Reasoning models increasingly **reject `temperature`**: the current Claude models
(Opus 5, Fable 5, Opus 4.8/4.7) return a 400, and OpenAI's gpt-5 family accepts
only the default. The flag is therefore opt-in:

- omitted — the parameter is not sent, and records carry `"temperature": null`;
- passed — it is sent, and if the model refuses it the runner warns, drops it,
  and retries. The *effective* value is what lands in the records.

Every backend negotiates the same way, covering `thinking` / `reasoning_effort` /
`max_completion_tokens` as well. This is what lets one runner sweep across
vendors and model generations with no per-model configuration. Anything dropped
is listed in `config.json` under `model.dropped_params`.

### Cost accounting is approximate by default

`providers.py` carries a small list-price table as a convenience. It is **not
authoritative** and will go stale; models absent from it (including the Gemini 3
family) record `cost_usd: 0.0` and print a warning. Pass `--price-in` /
`--price-out` to record real cost. Token counts are always exact, and
`tokens.reasoning` breaks out the thinking share — often the large majority of
output tokens on reasoning models.

## What it writes

```
results/
├── model_results.json              # appended across runs (--fresh to reset)
└── raw/<run_id>/
    ├── config.json                 # full provenance for the run
    └── <family>/<condition>/s<N>/
        ├── prompt_system.txt       # exactly what was sent
        ├── prompt_user.txt
        ├── response_raw.md         # the unmodified completion
        ├── candidate.go.txt        # the extracted Go file
        ├── compile_error.txt       # only when the build failed
        ├── iterations.json         # agentic condition only
        └── meta.json
```

Both paths are gitignored: unlike the pilot artifacts, model output is
stochastic, unbounded, and must not masquerade as a measured result. `git add -f`
a run you want in the record.

The candidate is archived as **`candidate.go.txt`**, not `.go`, because
`results/` sits inside the Go module and a stray `package capture` file there
would be swept up by `go build ./...` and break `make test`. To re-score one by
hand, copy it to `tasks/<family>/candidate.go` and follow
[`generation/README.md`](../generation/README.md).

### Record shape

Every record carries all of `harness.Result` plus the generation metadata:

```json
{
  "family": "capture", "semantic_task_id": "capture-t1",
  "candidate": "gemini-3.1-pro-preview/zero_shot/s0", "correct_ref": false,
  "schedule_id": "capture-crash-before-charge", "seed": 8, "hidden": true,
  "compile_status": "success", "runtime_status": "completed",
  "invariants": { "at_most_one": true, "no_lost_effect": false, "...": true },
  "violations": ["no_lost_effect", "no_false_dedup"], "ok": false,

  "provider": "gemini", "model_id": "gemini-3.1-pro-preview",
  "resolved_model": "gemini-3.1-pro-preview",
  "condition": "zero_shot", "sample": 0, "temperature": null,
  "tokens": { "input": 963, "output": 19944, "cache_read": 0,
              "cache_creation": 0, "reasoning": 18871, "total": 20907 },
  "cost_usd": 0.0,
  "run_id": "20260813T092750Z", "prompt_sha256": "9bafe85...",
  "agentic_iterations": null, "generated_at": "2026-08-13T09:30:41Z"
}
```

The `harness.Result` subset of every record validates against
[`task-schema/result.schema.json`](../task-schema/result.schema.json). One
vocabulary extension: `runtime_status` gains **`not_run`**, used when the
candidate never compiled and so was never executed.

Failure modes are recorded, never dropped — one record per schedule always:

| Situation | `compile_status` | `runtime_status` |
|-----------|------------------|------------------|
| Scored normally | `success` | `completed` / `nonconvergent` |
| Build failed, or no factory found | `error` | `not_run` |
| Panicked during a schedule | `success` | `crashed` |
| Exceeded the 120 s run budget | `success` | `nonconvergent` |

A model call that fails outright (rate limit, exhausted credits, refusal) emits
**no** records for that candidate and is logged under
`config.json → generation_failures`, so a billing problem can never be mistaken
for a benchmark result.

## How scoring works

The runner copies the Go module into a scratch workspace, writes the candidate
into the family package there, and generates a driver that binds it through the
family's own `TaskFor` and scores it with `harness.CheckFinancial` — the same
oracle the pilot uses. No second oracle exists, and your checkout is never
touched.

`compile_status` is an honest measurement. Extraction strips markdown fences and
surrounding prose (an output-format artifact); the Go itself — package clause,
imports, bodies — is compiled exactly as returned. The raw response is archived
next to the extracted file so any reviewer can check.

## The four conditions

`zero_shot`, `retrieval`, and `domain_guided` are single-shot. `agentic` runs a
real iterate loop: generate → compile → run the **public** schedules → feed the
compiler output and per-schedule oracle verdicts back → revise, up to
`--agentic-iterations`. Hidden schedules are never executed during iteration and
never shown to the model, so the hidden-set commitment in
[`ARTIFACT_EVALUATION.md`](../ARTIFACT_EVALUATION.md) holds by construction. Only
the final candidate is scored against the full set.

### Documented prompt expansions

Each file in `generation/prompts/` is self-contained — it carries the full
injected-environment interface and its own `Request` definition, so it is correct
read standalone. (Earlier revisions of `retrieval.md` and `domain_guided.md`
deferred the `Env` block to "the zero-shot condition", and `retrieval.md` never
defined `Request` at all; both were inlined. `agentic.md` carries them as a
reference section in its preamble — the renderer reads only the `## SYSTEM` and
`## USER` sections, so that block documents the file without entering the prompt,
which still directs a real agent to read the definitions from the checkout. The
renderer keeps the splice logic as a defensive no-op in case a template
regresses.)

Two expansions remain, applied **uniformly across all four conditions** so they
cannot bias one against another:

1. **The payload field.** The templates hardcode `capture`'s `Amount`; families
   that carry a `Payload` (`outbox`, `consumer`) get the correct field
   substituted, so every condition describes that family's real interface.
2. **An output contract.** One Go file, `package <family>`, a factory named
   `NewCandidate`, and every unexported top-level identifier prefixed `llm`. The
   prefix matters: the candidate compiles *inside* the family package (the wiring
   contract in `generation/README.md`), so an unprefixed `fingerprint` or
   `conflict` collides with the reference implementation's own helpers and fails
   the build for reasons that have nothing to do with retry-safety.

`agentic.md` additionally assumes an interactive shell the runner does not grant;
it gets an adapter note explaining that the runner performs the build-and-run
loop on the model's behalf, plus the interface and environment inline (it cannot
read the repository).

`{{RETRIEVED_PASSAGES}}` is filled with the illustrative corpus already shipped
in `retrieval.md`. **There is no retriever** — wiring a real corpus is future
work, and the retrieval condition should be read as "generic idempotency passages
in context", not as a retrieval system.

## Verifying the setup

```sh
python -m generation.runner --stub --samples 1 --conditions zero_shot
```

This is P5's acceptance check: the full pipeline with no API key, no network,
and no cost. The stub returns the shipped correct reference, so every family
must score **10/10 (6/6 hidden)** and the run must total **42/42** hidden
schedules — the same figure as the measured pilot. Anything less means the
harness wiring regressed, not the model.

### A worked live result

The first live run (`gemini-3.1-pro-preview`, `capture`, zero-shot) is a good
illustration of what the benchmark is for. The model produced a clean,
well-commented file that **compiled and passed all four public schedules and
five of six hidden ones** — then failed `capture-crash-before-charge` on
`no_lost_effect`. Its recovery path reads:

```go
// State is "reserved" and charge is not found.
// The original caller is likely still executing concurrently or crashed before charging.
// We safely defer to it.
return harness.Response{Status: "IN_PROGRESS"}
```

It correctly identified that the original caller may have crashed, and still
chose to defer — but in that schedule the caller is gone, and the recovery pass
is the only thing that can complete the effect. The payment is silently lost. The
model traded `no_lost_effect` away to protect `at_most_one`, and nothing in the
public schedules would have revealed it.

## Limitations

- **`make figures` does not read `model_results.json`.** `analysis/make_figures.py`
  consumes `results/pilot_results.json` only. Merging model runs into the figure
  pipeline is P8.
- **No retriever** for the retrieval condition (see above).
- **List-price table is approximate** and omits models it does not know rather
  than guessing (see above).
- **Generation is stochastic** — only scoring is reproducible. Re-scoring an
  archived `candidate.go.txt` gives identical results forever; re-generating does
  not. `prompt_sha256`, `resolved_model`, and `config.json` pin what was asked.
