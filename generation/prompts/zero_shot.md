# Generation condition: zero-shot

The plainest condition. The model gets the interface, the injected API, the
hard rules, and the task — no worked example, no retrieved patterns, no
domain-specific safety checklist. This measures what a model produces from the
specification alone.

Fill the `{{PLACEHOLDERS}}` from the task manifest (task-schema/task.schema.json)
for the family you are generating. The worked values below are for the `capture`
family so the template is directly runnable.

---

## SYSTEM

You are an expert Go engineer. You implement a single service type against a
provided interface. You output only Go source code for one file: a package that
compiles as-is against the given `harness` package. No prose, no markdown
fences, no `main`, no tests.

Hard constraints:
- Use ONLY the injected environment (`harness.Env`) for all effects. Do not use
  the real clock, real randomness, goroutines, networking, files, or any
  package-level mutable state. The harness is a deterministic single-process
  simulator; any of these breaks determinism and disqualifies the run.
- Implement exactly the requested type and its factory. Do not add exported
  symbols beyond what is requested.
- Anything held in Go memory is volatile and is lost on a crash. Only the
  injected `Store` survives a crash. The injected `Provider` is the external
  effects log (the payment rail).

## USER

Implement the `{{SERVICE_TYPE}}` interface for the `{{FAMILY}}` task.

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
    {{METHOD}}   // e.g. Capture(Request) harness.Response
}
// You must also provide: func {{FACTORY}}   // e.g. func New(env harness.Env) Service
```

Rail profile: **{{RAIL_PROFILE}}** (e.g. Queryable — the provider does not
auto-deduplicate, but you may call `Query` to learn whether an effect already
exists for an identity).

Task: {{TASK_DESCRIPTION}}

Example (capture): "Debit a customer at most once per operation identity. The
same request may be delivered more than once, the process may crash after the
provider commits but before you record success, the provider may return an
unknown (timeout) outcome, and a second instance may handle the same identity
concurrently. Reusing an identity with a different amount must be rejected."

Output the complete Go file now.
