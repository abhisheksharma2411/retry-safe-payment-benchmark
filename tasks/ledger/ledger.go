// Package ledger is the idempotent double-entry journal-posting task family: a
// service that posts a journal entry at most once per posting identity, even
// under retries, crashes after the external effect, unknown (dropped) provider
// outcomes, and concurrent retries on another instance. No duplicate journal is
// ever posted, and no accepted posting is ever lost.
//
// It ships a correct reference implementation, six seeded-bug mutants (each
// failing exactly one invariant), an independent oracle, and public/hidden fault
// schedules. The rail profile assumed here is Queryable: a service can reconcile
// an unknown outcome by asking the provider whether a posting already exists.
// This assumption is stated explicitly, as the benchmark contract requires.
//
// Tier-1 scope: a journal entry is modeled as ONE atomic posting effect
// (provider.Charge with Op "post", Resource = journal id). The paired
// debit/credit legs are treated as a single indivisible posting here; splitting
// the debit/credit atomicity into two coordinated legs (so that a partial
// posting of one leg without the other is itself a modeled hazard) is a Tier-2
// expansion of this family and is intentionally out of scope at Tier-1.
package ledger

import (
	"fmt"

	"t4bench/harness"
)

// Request is one journal-posting request.
type Request struct {
	ID       string
	Identity harness.Identity
	Amount   harness.Money
}

// Service is the candidate interface the model/agent must implement.
type Service interface {
	Post(Request) harness.Response
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
// Correct reference: reserve-then-post with reconcile-first recovery.
// ---------------------------------------------------------------------------

type correct struct{ env harness.Env }

// Correct is the retry-safe reference implementation. It reserves the journal id
// BEFORE posting, and on recovery reconciles first (queries the rail) so a
// previously committed posting is adopted rather than re-posted.
func Correct(env harness.Env) Service { return &correct{env} }

func (c *correct) Post(req Request) harness.Response {
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
		// Lost the reservation race: another attempt owns this journal id.
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
	// Reconcile-first: if a posting already exists for this journal id (e.g. from
	// a previous crashed attempt), adopt it instead of risking a second posting.
	if ref, found, _ := prov.Query(req.Identity); found {
		store.Complete(key, ref)
		return okResp(ref)
	}
	ref, err := prov.Charge(req.Identity, req.Amount)
	if err != nil {
		// Unknown outcome. Leave the reservation in place and defer to a later
		// recovery pass. Do NOT mark posted and do NOT post again here.
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}

// ---------------------------------------------------------------------------
// Seeded-bug mutants. Each violates a specific invariant under the Queryable
// profile; the oracle must detect it with the expected invariant.
// ---------------------------------------------------------------------------

// M1 unstable_identity: derives the provider idempotency key from the injected
// clock, so it differs across attempts. After a dropped response the provider
// cannot deduplicate the retry, double-posting the journal. Violates AtMostOne.
type m1 struct{ env harness.Env }

// M1 constructs the unstable-identity mutant.
func M1(env harness.Env) Service { return &m1{env} }

func (m *m1) Post(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		// Reusing this identity with a different payload is a conflict, not a
		// replay. Checked here so this mutant carries only its own seeded fault.
		if rec.Fingerprint != fingerprint(req.Amount) {
			return conflict()
		}
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Amount))
	// BUG: time-based journal key is not stable across a retry/recovery.
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

// M2 no_payload_check: ignores the posting fingerprint on replay, so reusing a
// journal id with a changed amount is accepted rather than rejected. Violates
// PayloadConsistency.
type m2 struct{ env harness.Env }

// M2 constructs the missing-payload-check mutant.
func M2(env harness.Env) Service { return &m2{env} }

func (m *m2) Post(req Request) harness.Response {
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

// M3 raw_key_only: scopes the posting identity to the raw caller key only,
// dropping book/op/journal id. Two books reusing the same client key collapse
// into one posting. Violates NoFalseDedup.
type m3 struct{ env harness.Env }

// M3 constructs the under-scoped-identity mutant.
func M3(env harness.Env) Service { return &m3{env} }

func (m *m3) Post(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	rawID := harness.Identity{CallerKey: req.Identity.CallerKey} // BUG: under-scoped
	key := rawID.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		// Reusing this identity with a different payload is a conflict, not a
		// replay. Checked here so this mutant carries only its own seeded fault.
		if rec.Fingerprint != fingerprint(req.Amount) {
			return conflict()
		}
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

// M4 vacuous_no_post: reserves and immediately marks the journal posted with a
// fabricated reference, never calling the provider. It never double-posts, but
// never posts either. Violates NoLostEffect.
type m4 struct{ env harness.Env }

// M4 constructs the vacuous-success mutant.
func M4(env harness.Env) Service { return &m4{env} }

func (m *m4) Post(req Request) harness.Response {
	store := m.env.Store()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		// Reusing this identity with a different payload is a conflict, not a
		// replay. Checked here so this mutant carries only its own seeded fault.
		if rec.Fingerprint != fingerprint(req.Amount) {
			return conflict()
		}
		return okResp(rec.Ref)
	}
	store.Reserve(key, fingerprint(req.Amount))
	store.Complete(key, "fake-ref") // BUG: no external posting
	return okResp("fake-ref")
}

// M5 recovery_loops: on an unknown outcome it spins forever waiting for a
// definitive answer, so it never converges within the bound. Violates Recover.
type m5 struct{ env harness.Env }

// M5 constructs the non-terminating-recovery mutant.
func M5(env harness.Env) Service { return &m5{env} }

func (m *m5) Post(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	if rec, ok := store.Get(key); ok && rec.State == "completed" {
		// Reusing this identity with a different payload is a conflict, not a
		// replay. Checked here so this mutant carries only its own seeded fault.
		if rec.Fingerprint != fingerprint(req.Amount) {
			return conflict()
		}
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

func (m *m6) Post(req Request) harness.Response {
	store, prov := m.env.Store(), m.env.Provider()
	key := req.Identity.Key()
	fp := fingerprint(req.Amount)

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
	ref, err := prov.Charge(req.Identity, req.Amount)
	if err != nil {
		return inProgress()
	}
	store.Complete(key, ref)
	return okResp(ref)
}
