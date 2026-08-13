# Generation condition: retrieval-augmented

Same task as zero-shot, but the prompt is augmented with retrieved reference
material: general documentation on idempotency keys and exactly-once effects.
The retrieved passages are *generic* (not the T4 reference solution and not the
domain checklist) — this condition measures whether relevant background improves
retry-safety without handing the model the answer.

Fill the `{{PLACEHOLDERS}}` from the task manifest. Worked values are for
`capture`. The `{{RETRIEVED_PASSAGES}}` block is filled by the retrieval runner
from the corpus (see generation/README.md); the examples below are illustrative.

---

## SYSTEM

You are an expert Go engineer implementing a single service type against a
provided interface. Output only the Go source for one compilable file — no
prose, no fences, no `main`, no tests.

Hard constraints (identical to the zero-shot condition):
- Use ONLY the injected `harness.Env`. No real clock/rand/goroutines/network/
  files/globals. The harness is a deterministic single-process simulator.
- Only the injected `Store` survives a crash; Go memory does not. The injected
  `Provider` is the external effects log.
- Implement exactly the requested type and factory; add no other exported
  symbols.

You are given retrieved reference material. Use it as background; it is not
specific to this task and may be partially irrelevant.

## USER

### Retrieved reference material

{{RETRIEVED_PASSAGES}}

Illustrative passages (replace with retriever output):

> "An idempotency key must be stable across retries of the *same* logical
> operation and distinct across *different* operations. Deriving it from a
> timestamp or a fresh UUID per attempt defeats deduplication."

> "For exactly-once external effects under at-least-once delivery, a common
> pattern is reserve-then-act: durably claim the operation, perform the side
> effect, then durably record completion. On recovery, re-check the durable
> claim and the downstream system of record before re-acting."

> "When a remote call times out, the outcome is unknown: the effect may or may
> not have happened. Retrying blindly can double-apply. Prefer querying the
> remote for the effect's existence, or rely on provider-side idempotency."

### Interface and environment

The injected environment:

```go
type Env interface {
    Store() Store        // durable, crash-surviving key/value
    Provider() Provider  // external effects log ("the rail"), keyed by Identity
    Clock() Clock        // injected logical time (monotonic counter)
    Rand() Rand          // injected deterministic randomness
    SetResponse(Response)
}
type Store interface {
    Get(key string) (Record, bool)
    Reserve(key, fingerprint string) bool // atomic create-if-absent; true iff caller created it
    Complete(key, ref string)
    Fail(key, errCode string)
    Put(key string, rec Record)
}
type Provider interface {
    Charge(id Identity, amt Money) (ref string, err error) // the ONE external effect
    Query(id Identity) (ref string, found bool, err error) // reconcile an unknown outcome
}
type Identity struct{ Merchant, Op, Resource, CallerKey string } // .Key() is the canonical dedup key
type Money struct{ Amount int64; Currency string }
type Record struct{ State, Fingerprint, Ref, ErrCode string } // State: ""|reserved|completed|failed
type Response struct{ Status, Ref, Err string }               // Status: OK|CONFLICT|IN_PROGRESS|FAILED|ERROR
```

The interface you must implement:

```go
type Request struct {
    ID       string
    Identity harness.Identity
    Amount   harness.Money
}
type {{SERVICE_TYPE}} interface {
    {{METHOD}}
}
// Provide: func {{FACTORY}}
```

Rail profile: **{{RAIL_PROFILE}}**.

Task: {{TASK_DESCRIPTION}}

Output the complete Go file now.
