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
| `claude-cli` | *none* — uses the CLI's own auth | the CLI's default | `claude` on `PATH` |

`claude-cli` is a **coding agent**, not a base model, and runs the agentic
condition only — see [CLI coding agents](#cli-coding-agents-agentic-condition-only).

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
| `--provider` | `anthropic` | `anthropic` \| `openai` \| `gemini` \| `claude-cli`. |
| `--model ID` | provider default | Model to evaluate. |
| `--samples N` | `3` | Samples per (family, condition). |
| `--temperature T` | *unset* | See the caveat below. |
| `--families` / `--conditions` | `all` | Comma-separated subsets. |
| `--agentic-iterations N` | `3` | Max revise cycles in the agentic condition (API providers). |
| `--agent-timeout N` | `1800` | Wall-clock budget for one CLI-agent session, in seconds. |
| `--agent-max-usd N` | *unset* | Spend cap for one CLI-agent session. |
| `--show-agent-config` | off | Print what the CLI would inherit beyond the scaffold (hashes + relevance scan) and exit. |
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
        ├── iterations.json         # agentic condition, API providers
        ├── agent_run.json          # claude-cli only: turns, usage, scaffold size
        ├── agent_audit.json        # claude-cli only: the leak audit
        ├── agent_transcript.jsonl  # claude-cli only: the full session
        ├── scaffold/               # claude-cli only: everything the agent could read
        └── meta.json
```

Each CLI-agent session also leaves its own directory under
`~/.claude/projects/…t4scaffold-<family>-<suffix>/` (one per sample, holding the
original session JSONL). The transcript is copied into `results/raw/` as part of
the run, so those are redundant afterwards; they are left in place rather than
deleted, because removing session history from a user's config directory is not
the runner's call.

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

A `claude-cli` record carries the same `harness.Result` core, with the agent
metadata in place of the sampling metadata:

```json
{
  "family": "capture", "candidate": "claude-cli/default/agentic/s0",
  "schedule_id": "capture-crash-before-charge", "hidden": true, "ok": true,

  "provider": "claude-cli", "condition": "agentic",
  "model_id": "claude-cli/default", "resolved_model": "claude-opus-4-6[1m]",
  "agent_cli_version": "2.1.87 (Claude Code)",
  "num_turns": 24, "wall_clock_s": 393.5,
  "scaffold_tokens": 4035, "injected_context_tokens": 1044096,
  "tokens": { "input": 132, "output": 21874, "...": 0 },
  "cost_usd": 4.2119, "temperature": null,
  "agent": {
    "session_id": "...", "stop_reason": "end_turn",
    "scaffold": { "files": 7, "bytes": 16141, "est_tokens": 4035 },
    "leak_audit": { "clean": true, "transcript_checked": true,
                    "contamination": [], "protected_files_modified": [],
                    "denied_tool_attempts": 0,
                    "read_audit": { "clean": true, "out_of_scaffold_path_refs": 0,
                                    "t4_source_access_completed": [] } }
  },
  "config_isolation": { "clean": true, "setting_sources": "",
                        "user_settings_loaded": false, "session_memory_files": 0,
                        "inherited": [ { "path": "~/.claude/settings.json",
                                         "sha256": "5454506902c96299…",
                                         "loaded": false } ] }
}
```

Two token counts, because they answer different questions. `scaffold_tokens` is
what the *benchmark* handed the agent — small, fixed, comparable across runs.
`injected_context_tokens` is everything the CLI actually placed in the model's
context across all turns, including its own system prompt and tool definitions.
The gap between them is precisely why a CLI agent is not comparable to a
single-shot API call, and why it is confined to the agentic condition.

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
for a benchmark result. A CLI-agent session that blows its `--agent-timeout` is
treated the same way: the session did not finish, so whatever `candidate.go`
happened to be on disk when the clock ran out is not scored as if it had been
submitted.

## Model provenance and the contamination argument

Every record carries a `provenance` block naming the exact snapshot that served
it, so the paper can argue that a model could not have trained on this
benchmark. The argument rests on one comparison, and both sides of it are
measured rather than asserted:

```json
"provenance": {
  "requested_model": "gemini-flash-latest",
  "resolved_model": "gemini-3.6-flash",
  "alias_requested": true,
  "model_snapshot": "3.6-flash-07-2026",
  "model_display_name": "Gemini 3.6 Flash",
  "snapshot_date": "2026-07",
  "release_date": null,
  "training_cutoff": null,
  "training_cutoff_source": "not exposed by the Gemini API",
  "repo_published_at": "2026-08-13",
  "snapshot_predates_repo_publication": true
}
```

`REPO_PUBLISHED_AT` (`2026-08-13`) is the contamination boundary — the date this
repository became public.

**Aliases are resolved, not recorded as typed.** `provenance` is captured *after*
the first completion, because a `-latest` alias only resolves to a concrete
snapshot in the response (`model_version`). `gemini-flash-latest` requested here
resolves to `gemini-3.6-flash`; the alias's own metadata carries no date at all,
so recording the requested name would have produced an unfalsifiable claim.

| API | Snapshot source | Training cutoff |
|---|---|---|
| Gemini | `models.get(...).version`, e.g. `3.1-pro-preview-01-2026` | not exposed |
| Anthropic | `models.retrieve(...).created_at` | not exposed |
| OpenAI | `models.retrieve(...).created` (unix) | not exposed |
| `claude-cli` | serving model from `modelUsage`; no metadata endpoint | not exposed |

**No API exposes a training cutoff.** It is recorded as `null` with the reason
rather than guessed — a fabricated cutoff inside a contamination argument would
be worse than an absent one. The snapshot date is a sound substitute in the
direction the argument needs: a model published in January cannot have trained
on anything from August.

**What this does and does not license.** It supports the narrow, checkable claim
that a model cannot have memorised *this repository* — its hidden schedules, its
reference implementations, its mutants. It does **not** support the claim that
the model lacks knowledge of idempotency, reserve-then-effect, or retry-safe
payment design; that material is widely documented and is certainly in every
model's training data. The paper should make the first claim and not the second.

For `claude-cli` the snapshot date is unavailable — the CLI reports which model
served the session but exposes no metadata endpoint — so
`snapshot_predates_repo_publication` is `null` rather than assumed. Setting
`ANTHROPIC_API_KEY` would resolve it.

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

The agentic condition has two backends, because "agentic" means two different
things depending on what is being evaluated. An API model has no shell, so the
runner drives the loop for it. A CLI coding agent has its own shell and drives
its own loop — see [CLI coding agents](#cli-coding-agents-agentic-condition-only).
Both are scored identically, and both see only the public schedules while
iterating; they differ in who runs the build-and-test cycle, which is the honest
difference and the only one the prompt reflects.

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
   `NewCandidate`, every unexported top-level identifier prefixed `llm`, and an
   explicit statement that `Request`, `Service` and `Factory` already exist and
   must not be redeclared. All of it exists for one reason: the candidate
   compiles *inside* the family package (the wiring contract in
   `generation/README.md`), so a name it shares with the package fails the build
   for reasons that have nothing to do with retry-safety.

   The redeclaration clause was added after measuring its absence. A first sweep
   showed a 0.68 compile rate, and **10 of 16 failures were
   `Request redeclared in this block`** — the prompt displays `type Request
   struct` and `type Service interface` so the model knows their shape, and
   models reasonably wrote them out again. That is prompt-trap navigation, not
   retry-safety, and at ~30% of candidates it would have swamped the signal the
   benchmark exists to measure. Re-tested on the same model and condition after
   the fix: 4/4 compiled where it had been 1/4.

`agentic.md` additionally assumes an interactive shell the runner does not grant;
it gets an adapter note explaining that the runner performs the build-and-run
loop on the model's behalf, plus the interface and environment inline (it cannot
read the repository).

`{{RETRIEVED_PASSAGES}}` is filled with the illustrative corpus already shipped
in `retrieval.md`. **There is no retriever** — wiring a real corpus is future
work, and the retrieval condition should be read as "generic idempotency passages
in context", not as a retrieval system.

## CLI coding agents (agentic condition only)

`--provider claude-cli` evaluates a **coding agent** — Claude Code in headless
mode — rather than a base model. It needs no API key: the CLI uses whatever
authentication it already has.

```sh
python -m generation.runner --provider claude-cli --conditions agentic \
    --families capture --samples 1
```

### Why it is locked to `agentic`

A CLI agent is not a model. It brings its own system prompt, its own tool
definitions, and a multi-turn loop, and those dominate the measurement: on a
trivial prompt the CLI injects tens of thousands of tokens of its own context
before the task is even stated. Scoring it as `zero_shot` would credit that
scaffolding to the prompt condition, and the cross-condition comparison — the
whole point of having four conditions — would silently stop meaning anything.

So the runner **refuses**:

```
$ python -m generation.runner --provider claude-cli --conditions zero_shot
--provider claude-cli cannot run the zero_shot condition.
  claude-cli is a coding agent, not a base model: it brings its own system
  prompt, its own tools, and a multi-turn loop.
  Scoring it as zero_shot/retrieval/domain_guided would attribute its
  scaffolding to the prompt condition and corrupt the comparison.
  Re-run with: --provider claude-cli --conditions agentic
```

It refuses rather than filtering, because dropping the base conditions silently
would produce a run that looks like a four-condition sweep and is not one. The
one exception: leaving `--conditions` at its default is not a request for the
base conditions, so it narrows to `agentic` and says so. The guard is enforced
twice — at argument parsing and again at the point of use — so it cannot be
lost to a refactor of the CLI layer.

### The sealed scaffold

The agent has a shell, so it cannot be handed the repository: that would hand it
`HiddenCases()`, `Correct`, the six mutants, and the oracle. Instead each
(family, sample) gets a scaffold built from scratch, containing only:

```
go.mod                          module t4bench
README.md                       the contract, the rules, how it is scored
API.md                          the injected environment, in prose
harness/api.go                  Env/Store/Provider/Clock/Rand + value types
tasks/<family>/<family>.go      package doc, Request, Service, Factory
tasks/<family>/candidate.go     the only file the agent may edit
tasks/<family>/public_test.go   the PUBLIC schedules
bin/t4public                    the scorer (a binary; no source)
```

Roughly 16 KB — about 4k tokens. What is **absent** is the point:

| Withheld | How |
|---|---|
| Hidden schedules | `cases.go` is never copied. The scorer resolves `PublicCases()` and has no flag, argument, or environment variable that makes it resolve anything else. |
| Oracle internals | `harness/oracle.go` is never copied. The scaffold's harness package is types only — no `CheckFinancial`, no `Spec`, not even the kernel's `Run`. |
| Correct reference, mutants | The family slice is taken **by declaration name** (`Request`, `Service`, `Factory`), never by line offset, so it stops above `Correct` and `M1`–`M6`. A missing declaration is a hard error, not a quietly truncated file. |

`go test` still returns real oracle verdicts, because `public_test.go` shells
out to `bin/t4public`, a per-scaffold binary with the workspace path linked in
at build time (`-ldflags -X main.workspaceDir=...`). No file the agent can read
names a route back to the benchmark tree, and compiler output is scrubbed of
workspace paths before it is shown. There is still exactly one oracle.

The agent's `go test` looks like this:

```
PUBLIC schedules for capture (4):
  pass capture-happy                          runtime=completed
  FAIL capture-payload-conflict               runtime=completed  violated: payload_consistency
...
2/4 public schedules passed.
Hidden schedules were not run and will not be shown.
```

Its final `candidate.go` is then scored **outside** the scaffold, by the normal
path, against **all** schedules — public and hidden — with the same oracle the
pilot uses.

### Isolation is enforced, then audited

Structural isolation is checked at build time: a scaffold that contains a hidden
schedule id, `CheckFinancial`, `HiddenCases`, or `func Correct(` fails to build
and aborts the run. Beyond that:

- **`WebSearch` and `WebFetch` are denied.** This repository is public; an agent
  that can search can read the hidden schedules out of GitHub. This is the one
  isolation hole that is not structural, and it is closed by flag.
- **Only the Go toolchain may be run**: `--allowedTools 'Bash(go:*)'
  'Bash(gofmt:*)'`. Every other command is denied, and each attempt is counted
  in `denied_tool_attempts`.
- **A deny-list** blocks reads of the private workspace and the repository root.
  It is written *outside* the scaffold — a deny-list the agent can read is a map
  of where to look.
- **Session persistence is left on deliberately**, because the transcript is the
  only complete record of what the agent read and ran. It is scanned for every
  withheld string — the family's real hidden schedule ids, the oracle, the
  reference, and the paths to both — and archived next to the result.
- **Protected files are hashed** before and after. An agent that "passed" by
  rewriting its own test is caught rather than believed.

A finding never silently discards a result. It is recorded with it, in
`agent.leak_audit`, so a contaminated run is visible in `model_results.json`
rather than absent from it, and it is printed at the end of the run line.

`clean` tracks **contamination only** — withheld strings in the transcript, or
edits to protected files. Denied commands are counted separately and do not
clear the flag: a denial means the sandbox held, not that the run is tainted.
Conflating the two would either cry wolf over an incidental `ls` or, worse,
train the reader to ignore the flag that does matter.

> **Permissions are load-bearing, and were the one thing that had to be found by
> running it.** `--permission-mode acceptEdits` covers file edits but **not**
> `Bash`, and headless mode has nobody to approve a prompt — so the first
> verification run had all 36 of its `go build` / `go test` calls auto-denied.
> It still scored 10/10, from reasoning alone, having never once run the test
> suite. That is a perfectly good zero-shot result wearing an agentic label, and
> nothing in the output would have said so. Hence `--allowedTools 'Bash(go:*)'`,
> and hence `denied_tool_attempts` in every record: if the loop is ever broken
> again, the number will say so.

### Config isolation

`claude -p` loads more than the prompt: user settings, a global `CLAUDE.md`, and
session memory. This benchmark was **built in Claude Code on the same machine**,
so that inherited context is a live contamination channel — an agent primed with
notes about reserve-then-effect or the six invariants is recalling, not solving.

Inspect exactly what would be inherited:

```sh
python -m generation.runner --show-agent-config --families capture
```

It builds a real scaffold (session memory is scoped to the working directory, so
inspecting any other path answers a different question), hashes every file that
could be injected, scans each for benchmark-relevant terms, and prints a verdict.

Full relocation via `CLAUDE_CONFIG_DIR` was tested and **breaks subscription
auth** — the CLI returns `Not logged in`. `--bare` suppresses everything but
requires `ANTHROPIC_API_KEY`, which is what this backend exists to avoid needing.
So isolation is achieved three ways, and the third is what makes it safe rather
than lucky:

1. **`--setting-sources ''`** — no user, project, or local settings are loaded.
   This is necessary, not precautionary. The settings on the machine this was
   developed on allow `Bash(python3 -c ' *)`, several `Bash(curl …)` rules, and
   `WebFetch(domain:github.com)`; this repository is public, so any of those is a
   route to the hidden schedules that the Go-only allowlist would not stop.
   The explicit deny-list passed via `--settings` is a separate file and still
   applies.
2. **The scaffold cwd is a fresh `mkdtemp` path**, and Claude Code scopes memory
   by working directory — so the session's memory directory is empty *by
   construction*. Verified: scaffold-scoped memory directories contain 0 files,
   and a completed run's transcript contains none of the operator's memory
   strings.
3. **Everything still reachable is hashed and scanned before each sample**, and a
   benchmark-relevant hit **refuses the run** (`AgentContaminated`). Contamination
   here cannot be corrected after the fact — once the agent has read a note about
   reserve-then-effect, no audit can subtract it — so the only safe move is not
   to run.

The full state, including the SHA-256 of every inherited file, is recorded in
`config_isolation` on every record, so the paper can disclose it.

### Filesystem confinement

The scaffold and its private workspace are temp directories **outside the
checkout**, asserted at construction rather than assumed: `scratch_root` is a
parameter, and a scaffold created inside the repository would place the hidden
schedules, the oracle and the reference inside the agent's own permission
boundary.

Modification is not the risk — **reading** is. An agent that read `cases.go` has
the scoring set whether or not it wrote a byte, so the audit walks the session
transcript for every filesystem path the agent named (tool inputs plus absolute
paths and `../` traversals in Bash command lines), resolves each against the
scaffold, and classifies it:

| Field | Meaning |
|---|---|
| `out_of_scaffold_path_refs` | paths naming anything outside the scaffold |
| `t4_source_path_refs` | of those, ones under the repo or named like benchmark source (`cases.go`, `oracle.go`, …) |
| `t4_source_access_completed` | the subset **not** denied — the ones that matter |

`completed` means the call was not denied, which includes calls that only resolve
or stat a path (`realpath`) and return no file content. It is reported
conservatively either way; whether content actually crossed the barrier is
answered by the withheld-string scan of the same transcript, which would surface
a hidden schedule id verbatim.

Validated against a deliberately adversarial session — one told to read
`tasks/capture/cases.go` by absolute path, by `../` traversal, via `python3 -c`,
via `cat`, and via GitHub. The audit found all five attempts; the file's contents
never reached the agent.

### Budgets

The installed CLI (2.1.87) exposes **no `--max-turns`**, so iteration is bounded
by `--agent-timeout` (wall clock, enforced by the runner) and `--agent-max-usd`
(passed through to `--max-budget-usd`). `num_turns` is therefore *recorded* per
run rather than capped — it is in every record.

### Codex

`--provider codex-cli` is declared but **not implemented**: `codex` was not on
`PATH` when this backend was written, and shipping an untested adapter that
claims to work is worse than one that says it does not. Selecting it fails in
preflight with a pointer to the extension point. Adding it is a `CLIAgent`
subclass in [`agent_cli.py`](../generation/runner/agent_cli.py) plus a registry
entry; the scaffold, the guard, the audit, and the record path are all
provider-agnostic and need no changes.

### Known limitation

User/project/local settings are excluded and session memory is empty by
construction (see [Config isolation](#config-isolation)), but a **global
`~/.claude/CLAUDE.md` would still be loaded** — `--setting-sources` does not
govern it, and the config directory cannot be relocated without losing
subscription auth. It is therefore hashed and scanned before every sample, and a
benchmark-relevant hit refuses the run. On the machine this was developed on, no
global `CLAUDE.md` exists at all.

Treat CLI-agent numbers as machine-specific regardless: `config.json` records the
CLI version and every flag used, and `config_isolation` records the hash of every
inherited file, so a run stays auditable after the fact.

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
- **CLI-agent runs are not comparable to base-model runs**, by construction —
  that is why the guard exists. They are also not turn-capped (no `--max-turns`
  in the installed CLI) and inherit the operator's Claude Code configuration.
- **Generation is stochastic** — only scoring is reproducible. Re-scoring an
  archived `candidate.go.txt` gives identical results forever; re-generating does
  not. `prompt_sha256`, `resolved_model`, and `config.json` pin what was asked.
