// Package outbox is the transactional-outbox task family: a service that
// publishes a domain event effectively-once per event identity, even under
// retries, crashes after the external publish, unknown (dropped) provider
// outcomes, and concurrent retries on another instance.
//
// It ships a correct reference implementation, five seeded-bug mutants (each
// failing exactly one invariant), an independent oracle, and public/hidden fault
// schedules. The rail profile assumed here is Queryable: a service can reconcile
// an unknown outcome by asking the message bus whether an event was already
// published. This assumption is stated explicitly, as the benchmark contract
// requires.
//
// The core hazard is a crash between the business commit (reserving the outbox
// record) and the publish of the event: a naive relay either loses the event or
// publishes it twice. The retry-safe reference commits the outbox record first
// and reconciles-first on recovery, adopting an already-published event via
// Query rather than re-publishing it.
package outbox

import (
	"fmt"

	"t4bench/harness"
)

// Request is one publish request for a single outbox event.
type Request struct {
	ID       string
	Identity harness.Identity
	Payload  harness.Money
}

// Service is the candidate interface the model/agent must implement.
type Service interface {
	Publish(Request) harness.Response
}

// Factory constructs a fresh candidate instance (one process) with an injected
// environment. A new Factory call models a new process; volatile fields do not
// survive a crash.
type Factory func(env harness.Env) Service

func fingerprint(p harness.Money) string { return fmt.Sprintf("%d-%s", p.Amount, p.Currency) }

func okResp(ref string) harness.Response { return harness.Response{Status: "OK", Ref: ref} }
func conflict() harness.Response          { return harness.Response{Status: "CONFLICT"} }
func inProgress() harness.Response        { return harness.Response{Status: "IN_PROGRESS"} }

// ---------------------------------------------------------------------------
// Correct reference: commit-then-publish with reconcile-first recovery.
// ---------------------------------------------------------------------------

type correct struct{ env harness.Env }

// Correct is the retry-safe reference implementation.
func Correct(env harness.Env) Service { return &correct{env} }

func (c *correct) Publish(req Request) harness.Response {
	store, prov := c.env.Store(), c.env.Provider()
	key := req.Identity.Key()
	fp := fingerprint(req.Payload)

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
		// Lost the reservation race: another attempt owns this event identity.
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
	// Reconcile-first: if the event was already published for this identity (e.g.
	// from a previous crashed attempt), adopt it instead of risking a duplicate
	// publish.
	if ref, found, _ := prov.Query(req.Identity); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(req.Identity, req.Payload)
	if err != nil {
		// Unknown outcome. Leave the outbox record reserved and defer to a later
		// recovery pass. Do NOT mark completed and do NOT re-publish here.
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// ---------------------------------------------------------------------------
// Seeded-bug mutants. Each violates a specific invariant under the Queryable
// profile; the oracle must detect it with the expected invariant.
// ---------------------------------------------------------------------------

// M1 unstable_identity: derives the event id from the injected clock, so it
// differs across attempts. After a dropped response the bus cannot deduplicate
// the retry, publishing the event twice. Violates AtMostOne.
type m1 struct{ env harness.Env }

// M1 constructs the unstable-identity mutant.
func M1(env harness.Env) Service { return &m1{env} }

func (m *m1) Publish(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		// Reusing this identity with a different payload is a conflict, not a
		// replay. Checked here so this mutant carries only its own seeded fault.
		if rec.Fingerprint != fingerprint(req.Payload) {
			return conflict()
		}
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Payload))
	// BUG: time-based event id is not stable across a retry/recovery.
	unstable := harness.Identity{
		Merchant:  req.Identity.Merchant,
		Op:        req.Identity.Op,
		Resource:  fmt.Sprintf("%s-%d", req.Identity.Resource, m.env.Clock().Now()),
		CallerKey: req.Identity.CallerKey,
	}
	ref, err := prov.Charge(unstable, req.Payload)
	if err != nil {
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// M2 no_payload_check: ignores the payload fingerprint on replay, so reusing an
// event identity with a changed payload is accepted rather than rejected.
// Violates PayloadConsistency.
type m2 struct{ env harness.Env }

// M2 constructs the missing-payload-check mutant.
func M2(env harness.Env) Service { return &m2{env} }

func (m *m2) Publish(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		return okResp(rec.Ref) // BUG: no fingerprint comparison
	}
	store.Reserve(key, fingerprint(req.Payload))
	if ref, found, _ := prov.Query(req.Identity); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(req.Identity, req.Payload)
	if err != nil {
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// M3 raw_key_only: scopes the publish identity to the raw caller key only,
// dropping stream/op/event-id. Two streams reusing the same client key collapse
// into one published event. Violates NoFalseDedup.
type m3 struct{ env harness.Env }

// M3 constructs the under-scoped-identity mutant.
func M3(env harness.Env) Service { return &m3{env} }

func (m *m3) Publish(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	rawID := harness.Identity{CallerKey: req.Identity.CallerKey} // BUG: under-scoped
	key := rawID.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		// Reusing this identity with a different payload is a conflict, not a
		// replay. Checked here so this mutant carries only its own seeded fault.
		if rec.Fingerprint != fingerprint(req.Payload) {
			return conflict()
		}
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Payload))
	if ref, found, _ := prov.Query(rawID); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(rawID, req.Payload)
	if err != nil {
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// M4 vacuous_no_publish: reserves and immediately marks the outbox record
// completed with a fabricated reference, never publishing the event. It never
// double-publishes, but never publishes either. Violates NoLostEffect.
type m4 struct{ env harness.Env }

// M4 constructs the vacuous-success mutant.
func M4(env harness.Env) Service { return &m4{env} }

func (m *m4) Publish(req Request) harness.Response {
	store := m.env.Store()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		// Reusing this identity with a different payload is a conflict, not a
		// replay. Checked here so this mutant carries only its own seeded fault.
		if rec.Fingerprint != fingerprint(req.Payload) {
			return conflict()
		}
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Payload))
	store.Complete(key, "fake-ref") // BUG: no external publish
	return okResp("fake-ref")
}

// M5 recovery_loops: on an unknown outcome it spins forever waiting for a
// definitive answer, so it never converges within the bound. Violates Recover.
type m5 struct{ env harness.Env }

// M5 constructs the non-terminating-recovery mutant.
func M5(env harness.Env) Service { return &m5{env} }

func (m *m5) Publish(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		// Reusing this identity with a different payload is a conflict, not a
		// replay. Checked here so this mutant carries only its own seeded fault.
		if rec.Fingerprint != fingerprint(req.Payload) {
			return conflict()
		}
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Payload))
	if ref, found, _ := prov.Query(req.Identity); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(req.Identity, req.Payload)
	if err != nil {
		for { // BUG: busy-wait forever instead of a bounded recovery
			_, _ = store.Get(key) // injected op so the kernel can observe the spin
		}
	}
	store.Complete(key, ref)
	return okResp(ref)
}
