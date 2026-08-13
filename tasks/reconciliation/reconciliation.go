// Package reconciliation is the idempotent/monotonic reconciliation task family:
// a service that applies one provider reconciliation update at most once per
// update identity, even under delayed, duplicate, and reordered updates, crashes
// after the external effect, unknown (dropped) provider outcomes, and concurrent
// retries on another instance.
//
// It ships a correct reference implementation, five seeded-bug mutants (each
// failing exactly one invariant), an independent oracle, and public/hidden fault
// schedules. The rail profile assumed here is Queryable: a service can reconcile
// an unknown outcome by asking the provider whether an effect already exists.
// This assumption is stated explicitly, as the benchmark contract requires.
//
// Tier-1 (this family) models idempotent at-most-once application with
// reconcile-first recovery: a given update is applied exactly once regardless of
// delivery duplication or delay, and reusing an update identity with changed
// content is rejected. Full monotonicity across stale/reordered versions —
// version-ordering so that a newer update supersedes an older one and stale
// updates are dropped rather than applied — is emphasized in the schedule notes
// but is a Tier-2 expansion, not enforced by the Tier-1 oracle here.
package reconciliation

import (
	"fmt"

	"t4bench/harness"
)

// Request is one reconciliation-update request. Identity.Resource is the update
// id; Amount carries the update's content fingerprint.
type Request struct {
	ID       string
	Identity harness.Identity
	Amount   harness.Money
}

// Service is the candidate interface the model/agent must implement.
type Service interface {
	Apply(Request) harness.Response
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
// Correct reference: reserve-then-apply with reconcile-first recovery.
// ---------------------------------------------------------------------------

type correct struct{ env harness.Env }

// Correct is the retry-safe reference implementation: it reserves the update id
// before applying and reconciles first on recovery.
func Correct(env harness.Env) Service { return &correct{env} }

func (c *correct) Apply(req Request) harness.Response {
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
		// Lost the reservation race: another delivery of this update owns the id.
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
	// Reconcile-first: if this update was already applied to the provider (e.g. by
	// a previous crashed attempt), adopt that effect instead of applying twice.
	if ref, found, _ := prov.Query(req.Identity); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(req.Identity, req.Amount)
	if err != nil {
		// Unknown outcome. Leave the reservation in place and defer to a later
		// recovery pass. Do NOT mark applied and do NOT apply again here.
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// ---------------------------------------------------------------------------
// Seeded-bug mutants. Each violates a specific invariant under the Queryable
// profile; the oracle must detect it with the expected invariant.
// ---------------------------------------------------------------------------

// M1 unstable_identity: derives the provider apply key from the injected clock,
// so it differs across deliveries. A delayed/duplicate delivery of the same
// update then applies a second time because the provider cannot deduplicate it.
// Violates AtMostOne.
type m1 struct{ env harness.Env }

// M1 constructs the unstable-identity mutant.
func M1(env harness.Env) Service { return &m1{env} }

func (m *m1) Apply(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Amount))
	// BUG: time-based apply key is not stable across a duplicate/recovery delivery.
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

// M2 no_payload_check: ignores the content fingerprint on replay, so the same
// update id arriving with changed content is accepted rather than rejected.
// Violates PayloadConsistency.
type m2 struct{ env harness.Env }

// M2 constructs the missing-payload-check mutant.
func M2(env harness.Env) Service { return &m2{env} }

func (m *m2) Apply(req Request) harness.Response {
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

// M3 raw_key_only: scopes the apply identity to the raw caller key only,
// dropping account/op/update-id. Two distinct updates from different accounts
// that share a caller key collapse into one effect. Violates NoFalseDedup.
type m3 struct{ env harness.Env }

// M3 constructs the under-scoped-identity mutant.
func M3(env harness.Env) Service { return &m3{env} }

func (m *m3) Apply(req Request) harness.Response {
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

// M4 vacuous_no_apply: reserves and immediately marks the update applied with a
// fabricated reference, never calling the provider. It never double-applies, but
// never applies either. Violates NoLostEffect.
type m4 struct{ env harness.Env }

// M4 constructs the vacuous-success mutant.
func M4(env harness.Env) Service { return &m4{env} }

func (m *m4) Apply(req Request) harness.Response {
	store := m.env.Store()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Amount))
	store.Complete(key, "fake-ref") // BUG: no external effect
	return okResp("fake-ref")
}

// M5 recovery_loops: on an unknown outcome it spins forever waiting for a
// definitive answer, so it never converges within the bound. Violates Recover.
type m5 struct{ env harness.Env }

// M5 constructs the non-terminating-recovery mutant.
func M5(env harness.Env) Service { return &m5{env} }

func (m *m5) Apply(req Request) harness.Response {
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
