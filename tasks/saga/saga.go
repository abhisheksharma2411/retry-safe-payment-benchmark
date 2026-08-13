// Package saga is the idempotent saga-step task family: a service that executes
// one saga step at most once per step identity, even under retries, crashes
// after the external effect, unknown (dropped) provider outcomes, and concurrent
// retries on another instance.
//
// Scope (honesty note): Tier-1 models a *single* idempotent saga step with
// reserve-before-effect and reconcile-first recovery — the same at-most-once
// hazard the capture family exercises, themed to "crash mid-step then recovery".
// Multi-step orchestration (running a sequence of steps in order) and
// compensation (running inverse steps to unwind a partial saga on failure) are
// deliberately NOT modeled here; they are a Tier-2/Tier-3 expansion. This family
// only guarantees that the one step it runs is executed exactly once.
//
// It ships a correct reference implementation, five seeded-bug mutants (each
// failing exactly one invariant), an independent oracle, and public/hidden fault
// schedules. The rail profile assumed here is Queryable: a service can reconcile
// an unknown outcome by asking the provider whether an effect already exists.
// This assumption is stated explicitly, as the benchmark contract requires.
package saga

import (
	"fmt"

	"t4bench/harness"
)

// Request is one saga-step execution request.
type Request struct {
	ID       string
	Identity harness.Identity
	Amount   harness.Money
}

// Service is the candidate interface the model/agent must implement.
type Service interface {
	Execute(Request) harness.Response
}

// Factory constructs a fresh candidate instance (one process) with an injected
// environment. A new Factory call models a new process; volatile fields do not
// survive a crash.
type Factory func(env harness.Env) Service

func fingerprint(a harness.Money) string { return fmt.Sprintf("%d-%s", a.Amount, a.Currency) }

func okResp(ref string) harness.Response { return harness.Response{Status: "OK", Ref: ref} }
func conflict() harness.Response          { return harness.Response{Status: "CONFLICT"} }
func inProgress() harness.Response        { return harness.Response{Status: "IN_PROGRESS"} }

// ---------------------------------------------------------------------------
// Correct reference: reserve-the-step-then-effect with reconcile-first recovery.
// ---------------------------------------------------------------------------

type correct struct{ env harness.Env }

// Correct is the retry-safe reference implementation.
func Correct(env harness.Env) Service { return &correct{env} }

func (c *correct) Execute(req Request) harness.Response {
	store, prov := c.env.Store(), c.env.Provider()
	key := req.Identity.Key()
	fp := fingerprint(req.Amount)

	if rec, ok := store.Get(key); ok && rec.State != "" {
		switch rec.State {
		case "completed":
			if rec.Fingerprint != fp {
				return conflict()
			}
			return okResp(rec.Ref)
		case "failed":
			return harness.Response{Status: "FAILED", Err: rec.ErrCode}
		case "reserved":
			if rec.Fingerprint != fp {
				return conflict()
			}
			return c.finish(store, prov, key, req)
		}
	}

	if !store.Reserve(key, fp) {
		// Lost the reservation race: another attempt owns this step identity.
		rec, _ := store.Get(key)
		if rec.Fingerprint != fp {
			return conflict()
		}
		if rec.State == "completed" {
			return okResp(rec.Ref)
		}
		return inProgress()
	}
	return c.finish(store, prov, key, req)
}

func (c *correct) finish(store harness.Store, prov harness.Provider, key string, req Request) harness.Response {
	// Reconcile-first: if the step effect already exists for this identity (e.g.
	// from a previous crashed attempt), adopt it instead of risking a second run.
	if ref, found, _ := prov.Query(req.Identity); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(req.Identity, req.Amount)
	if err != nil {
		// Unknown outcome. Leave the reservation in place and defer to a later
		// recovery pass. Do NOT mark completed and do NOT execute the step again.
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// ---------------------------------------------------------------------------
// Seeded-bug mutants. Each violates a specific invariant under the Queryable
// profile; the oracle must detect it with the expected invariant.
// ---------------------------------------------------------------------------

// M1 unstable_identity: derives the step's provider idempotency key from the
// injected clock, so it differs across attempts. After a dropped response the
// provider cannot deduplicate the retry, so the step is executed a second time.
// Violates AtMostOne.
type m1 struct{ env harness.Env }

// M1 constructs the unstable-identity mutant.
func M1(env harness.Env) Service { return &m1{env} }

func (m *m1) Execute(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Amount))
	// BUG: time-based step key is not stable across a retry/recovery.
	unstable := harness.Identity{
		Merchant:  req.Identity.Merchant,
		Op:        req.Identity.Op,
		Resource:  req.Identity.Resource,
		CallerKey: fmt.Sprintf("%s-%d", req.Identity.CallerKey, m.env.Clock().Now()),
	}
	ref, err := prov.Charge(unstable, req.Amount)
	if err != nil {
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// M2 no_payload_check: ignores the step-arg fingerprint on replay, so reusing a
// step identity with changed step args is accepted rather than rejected.
// Violates PayloadConsistency.
type m2 struct{ env harness.Env }

// M2 constructs the missing-payload-check mutant.
func M2(env harness.Env) Service { return &m2{env} }

func (m *m2) Execute(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		return okResp(rec.Ref) // BUG: no fingerprint comparison
	}
	store.Reserve(key, fingerprint(req.Amount))
	if ref, found, _ := prov.Query(req.Identity); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(req.Identity, req.Amount)
	if err != nil {
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// M3 raw_key_only: scopes the step identity to the raw caller key only, dropping
// merchant/op/resource. Two distinct sagas reusing the same caller key collapse
// into one step effect. Violates NoFalseDedup.
type m3 struct{ env harness.Env }

// M3 constructs the under-scoped-identity mutant.
func M3(env harness.Env) Service { return &m3{env} }

func (m *m3) Execute(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	rawID := harness.Identity{CallerKey: req.Identity.CallerKey} // BUG: under-scoped
	key := rawID.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Amount))
	if ref, found, _ := prov.Query(rawID); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(rawID, req.Amount)
	if err != nil {
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// M4 vacuous_no_step: reserves and immediately marks the step done with a
// fabricated reference, never calling the provider. It never runs the step
// twice, but never runs it at all. Violates NoLostEffect.
type m4 struct{ env harness.Env }

// M4 constructs the vacuous-success mutant.
func M4(env harness.Env) Service { return &m4{env} }

func (m *m4) Execute(req Request) harness.Response {
	store := m.env.Store()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Amount))
	store.Complete(key, "fake-ref") // BUG: step is never executed
	return okResp("fake-ref")
}

// M5 recovery_loops: on an unknown outcome it spins forever waiting for a
// definitive answer, so it never converges within the bound. Violates Recover.
type m5 struct{ env harness.Env }

// M5 constructs the non-terminating-recovery mutant.
func M5(env harness.Env) Service { return &m5{env} }

func (m *m5) Execute(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Amount))
	if ref, found, _ := prov.Query(req.Identity); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(req.Identity, req.Amount)
	if err != nil {
		for { // BUG: busy-wait forever instead of a bounded recovery
			_, _ = store.Get(key) // injected op so the kernel can observe the spin
		}
	}
	store.Complete(key, ref)
	return okResp(ref)
}
