// Package consumer is the deduplicating-message-consumer task family: a consumer
// that produces at most one effect per message identity, even when the same
// message is delivered more than once (duplicate delivery), delivered out of
// order, crashes after the effect commits, receives an unknown (dropped)
// provider outcome, or is redelivered concurrently to another worker.
//
// Each delivery of a message is modeled as a request instance (a start event).
// A duplicate delivery is two start events for the same request id. The single
// externally observable effect is a provider.Charge with Op "process" and
// Resource set to the message id.
//
// It ships a correct reference implementation, six seeded-bug mutants (each
// failing exactly one invariant), the shared oracle, and public/hidden fault
// schedules. The rail profile assumed here is Queryable: a consumer can
// reconcile an unknown outcome by asking the provider whether an effect already
// exists. This assumption is stated explicitly, as the benchmark contract
// requires.
package consumer

import (
	"fmt"

	"t4bench/harness"
)

// Request is one delivery of a message. Duplicate delivery is two Requests with
// the same ID and Identity; a changed payload with the same message id is a
// redelivery with a conflicting body.
type Request struct {
	ID       string
	Identity harness.Identity
	Payload  harness.Money
}

// Service is the candidate interface the model/agent must implement.
type Service interface {
	Consume(Request) harness.Response
}

// Factory constructs a fresh candidate instance (one process) with an injected
// environment. A new Factory call models a restarted consumer; volatile fields
// do not survive a crash.
type Factory func(env harness.Env) Service

func fingerprint(p harness.Money) string { return fmt.Sprintf("%d-%s", p.Amount, p.Currency) }

func okResp(ref string) harness.Response { return harness.Response{Status: "OK", Ref: ref} }
func conflict() harness.Response          { return harness.Response{Status: "CONFLICT"} }
func inProgress() harness.Response        { return harness.Response{Status: "IN_PROGRESS"} }

// ---------------------------------------------------------------------------
// Correct reference: reserve (dedup marker) before the processing effect, with
// reconcile-first recovery.
// ---------------------------------------------------------------------------

type correct struct{ env harness.Env }

// Correct is the dedup-safe reference implementation.
func Correct(env harness.Env) Service { return &correct{env} }

func (c *correct) Consume(req Request) harness.Response {
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
		// Lost the dedup-marker race: another delivery owns this message.
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
	// Reconcile-first: if an effect already exists for this message (e.g. from a
	// previous crashed delivery), adopt it instead of risking a second effect.
	if ref, found, _ := prov.Query(req.Identity); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(req.Identity, req.Payload)
	if err != nil {
		// Unknown outcome. Leave the dedup marker in place and defer to a later
		// recovery pass. Do NOT mark completed and do NOT process again here.
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// ---------------------------------------------------------------------------
// Seeded-bug mutants. Each violates a specific invariant under the Queryable
// profile; the oracle must detect it with the expected invariant.
// ---------------------------------------------------------------------------

// M1 unstable_identity: derives the processing key from the injected clock, so
// it differs across deliveries. A duplicate delivery cannot be deduplicated and
// is processed a second time. Violates AtMostOne.
type m1 struct{ env harness.Env }

// M1 constructs the unstable-identity mutant.
func M1(env harness.Env) Service { return &m1{env} }

func (m *m1) Consume(req Request) harness.Response {
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
	// BUG: time-based processing key is not stable across a redelivery/recovery.
	unstable := harness.Identity{
		Merchant:  req.Identity.Merchant,
		Op:        req.Identity.Op,
		Resource:  req.Identity.Resource,
		CallerKey: fmt.Sprintf("%s-%d", req.Identity.CallerKey, m.env.Clock().Now()),
	}
	ref, err := prov.Charge(unstable, req.Payload)
	if err != nil {
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// M2 no_payload_check: ignores the payload fingerprint on redelivery, so the
// same message id arriving with a changed payload is accepted rather than
// rejected. Violates PayloadConsistency.
type m2 struct{ env harness.Env }

// M2 constructs the missing-payload-check mutant.
func M2(env harness.Env) Service { return &m2{env} }

func (m *m2) Consume(req Request) harness.Response {
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

// M3 raw_key_only: scopes the processing identity to the raw caller key only,
// dropping merchant/op/resource. Two distinct messages that share a caller key
// collapse into one effect. Violates NoFalseDedup.
type m3 struct{ env harness.Env }

// M3 constructs the under-scoped-identity mutant.
func M3(env harness.Env) Service { return &m3{env} }

func (m *m3) Consume(req Request) harness.Response {
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

// M4 vacuous_no_effect: reserves the dedup marker and immediately marks the
// message processed with a fabricated reference, never performing the effect.
// It never double-processes, but never processes either. Violates NoLostEffect.
type m4 struct{ env harness.Env }

// M4 constructs the vacuous-success mutant.
func M4(env harness.Env) Service { return &m4{env} }

func (m *m4) Consume(req Request) harness.Response {
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
	store.Complete(key, "fake-ref") // BUG: no external effect
	return okResp("fake-ref")
}

// M5 recovery_loops: on an unknown outcome it spins forever waiting for a
// definitive answer, so it never converges within the bound. Violates Recover.
type m5 struct{ env harness.Env }

// M5 constructs the non-terminating-recovery mutant.
func M5(env harness.Env) Service { return &m5{env} }

func (m *m5) Consume(req Request) harness.Response {
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

// M6 replay_new_reference: whenever this service reports an effect it did not
// itself create — a replay of an already-completed identity, or an effect
// adopted from the rail during recovery — it mints a fresh reference and
// rewrites the durable record with it, instead of reporting the reference the
// effect actually carries. At most one effect is still produced, none is lost,
// distinct identities stay distinct, a changed payload is still rejected, and
// recovery still terminates in bounds — but a completed operation no longer
// reports a stable reference. Violates Reproducible.
//
// The fault deliberately spans both paths. Confined to the completed-replay path
// alone it is caught only on the concurrent-retry schedule, which is public in
// every family, and so would be invisible to the hidden set the benchmark
// actually scores on.
type m6 struct{ env harness.Env }

// M6 constructs the unstable-replay-reference mutant.
func M6(env harness.Env) Service { return &m6{env} }

func (m *m6) Consume(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	fp := fingerprint(req.Payload)

	if rec, ok := store.Get(key); ok && rec.State != "" {
		switch rec.State {
		case "completed":
			if rec.Fingerprint != fp {
				return conflict()
			}
			return m.adopt(store, key, rec.Ref)
		case "failed":
			return harness.Response{Status: "FAILED", Err: rec.ErrCode}
		case "reserved":
			if rec.Fingerprint != fp {
				return conflict()
			}
			return m.finish(store, prov, key, req)
		}
	}

	if !store.Reserve(key, fp) {
		// Lost the reservation race: another attempt owns this identity.
		rec, _ := store.Get(key)
		if rec.Fingerprint != fp {
			return conflict()
		}
		if rec.State == "completed" {
			return m.adopt(store, key, rec.Ref)
		}
		return inProgress()
	}
	return m.finish(store, prov, key, req)
}

// adopt carries the seeded fault, and only it. The external effect is neither
// repeated nor lost — what changes is the reference reported for it. The durable
// record is rewritten to the minted reference, so the store and the effects log
// disagree about which reference the single effect carries, which is exactly
// what the reproducibility check reconstructs from the observation.
func (m *m6) adopt(store harness.Store, key, ref string) harness.Response {
	fresh := ref + "-replay" // BUG: a completed identity must report a stable reference
	store.Complete(key, fresh)
	return okResp(fresh)
}

// finish still reconciles first and still never produces a second effect. The
// only change from the reference is that an adopted effect is reported under a
// minted reference; an effect this attempt creates itself is reported correctly.
func (m *m6) finish(store harness.Store, prov harness.Provider, key string, req Request) harness.Response {
	if ref, found, _ := prov.Query(req.Identity); found {
		return m.adopt(store, key, ref)
	}
	ref, err := prov.Charge(req.Identity, req.Payload)
	if err != nil {
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}
