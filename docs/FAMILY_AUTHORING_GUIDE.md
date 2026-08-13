# Task-family authoring guide

Every task family in this benchmark is a Go package under `tasks/<family>/` that
plugs into the shared deterministic harness in `harness/`. The `capture` family
(`tasks/capture/`) is the canonical, fully-tested template — read it before
writing a new family and mirror its structure exactly.

## The kernel API you build against (package `harness`)

Injected environment handed to every candidate (one `Env` per process/instance):

```go
type Env interface {
    Store() Store        // durable, crash-surviving key/value
    Provider() Provider  // external effects log ("the rail"), keyed by Identity
    Clock() Clock        // injected logical time (monotonic counter)
    Rand() Rand          // injected deterministic randomness
    SetResponse(Response) // record this instance's observable response
}

type Store interface {
    Get(key string) (Record, bool)
    Reserve(key, fingerprint string) bool // atomic create-if-absent; true iff this caller created it
    Complete(key, ref string)
    Fail(key, errCode string)
    Put(key string, rec Record)           // low-level (for naive/buggy refs)
}
type Provider interface {
    Charge(id Identity, amt Money) (ref string, err error) // the ONE external effect; dedups by Identity under idempotent profiles
    Query(id Identity) (ref string, found bool, err error) // reconcile an unknown outcome
}

type Identity struct{ Merchant, Op, Resource, CallerKey string } // .Key() is the canonical dedup key
type Money struct{ Amount int64; Currency string }
type Record struct{ State, Fingerprint, Ref, ErrCode string } // State: ""|reserved|completed|failed
type Response struct{ Status, Ref, Err string }                // Status: OK|CONFLICT|IN_PROGRESS|FAILED|ERROR
```

Model your family's single externally-effecting action as `Provider().Charge`
with a family-appropriate `Identity.Op` (e.g. `"refund"`, `"publish"`,
`"process"`, `"post"`, `"compensate"`, `"apply"`). The observer scores the
effects log independently of whatever the candidate stored.

## Schedules (serializable adversaries)

```go
type Event struct{ Op, Inst, Req, Fault, Arg string }
type Schedule struct{ ID string; Seed int64; Hidden bool; Note string; Events []Event }
```

Event vocabulary (all deterministic; exactly one candidate runs at a time):

| `Op`          | meaning |
|---------------|---------|
| `start`       | launch a candidate instance `Inst` for request `Req`, run to its first injected op |
| `step`        | resolve the instance's current op normally and advance to the next |
| `run`         | resolve normally until the instance terminates (diverging → non-convergence) |
| `run_until`   | step normally until the instance is parked *before* an op of kind `Arg` (`get`/`reserve`/`complete`/`fail`/`put`/`charge`/`query`) |
| `fault`       | apply a fault to the current op; `Fault` ∈ {`drop_response` (commit effect, lose ack, stay parked), `provider_timeout_after_commit` (commit + return timeout, resume), `provider_timeout_no_commit` (no effect + timeout), `store_ack_lost`} |
| `crash`       | kill the instance now; volatile memory lost, durable store kept |
| `recover`     | run the family's recovery program to completion (bounded; non-termination = non-convergence) |
| `faults_stop` | mark the point after which the run should stabilize |

The canonical crash-after-effect hazard is: `start; run_until charge; fault
drop_response; crash; recover`.

## Scoring (shared — do NOT write your own oracle)

Use `harness.Spec` + `harness.CheckFinancial(obs, spec)`. Author one `Spec` per
schedule from its semantics:

```go
type Spec struct {
    TotalEffects       int          // exact # of effects expected after stabilization
    Eligible           []Identity   // identities that must end resolved
    Conflicts          []string     // instance ids whose response must be CONFLICT
    DistinctIdentities int          // # distinct effect-identities expected (false-dedup)
}
```

The six shared invariants (`harness.InvAtMostOne`, `InvNoLostEffect`,
`InvNoFalseDedup`, `InvPayloadConsistency`, `InvReproducible`, `InvRecover`) are
scored uniformly for every family. Your family exercises different **hazards**
(fault patterns), not different invariant math.

## Required files per family (mirror `tasks/capture/`)

- `<family>.go`   — `Request`, `Service` interface, `Factory func(harness.Env) Service`, `Correct`, and 3–5 mutants `M1..Mn` (each breaking exactly one invariant under the assumed profile).
- `cases.go`      — identities/requests, `program`, `TaskFor(Factory) harness.Task`, `Candidate`/`Candidates()`, `Case`/`Cases()` (public + hidden), `PublicCases`/`HiddenCases`, `const Family`, `const Profile`.
- `run.go`        — `RunAll() []harness.Result` (mirror capture exactly).
- `<family>_test.go` — `TestCorrectReferencePassesAll`, `TestMutantsAreCaught`, `TestDeterminism` (mirror capture exactly).

## Hard rules

1. Do **not** modify anything in `harness/`, `go.mod`, `cmd/`, or other families.
2. Only injected `Env` may be used — no real time/rand/goroutines/network/globals. No package-level mutable state (breaks determinism).
3. `go test ./tasks/<family>/` MUST pass before you finish: the correct reference passes every schedule, every mutant is caught by its target invariant, and runs are deterministic.
4. Keep the rail profile explicit (`const Profile = harness.Queryable`).
