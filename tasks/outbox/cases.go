package outbox

import "t4bench/harness"

// Rail profile assumed by this family (stated explicitly per the benchmark
// contract). See package doc and paper Sec. "Fault-Injection Harness".
const Profile = harness.Queryable

// Identities and requests used by the schedules. An outbox identity scopes the
// emitting stream (Merchant), the operation (Op "publish"), the event id
// (Resource), and the caller's idempotency key (CallerKey).
var (
	identE1 = harness.Identity{Merchant: "svc_orders", Op: "publish", Resource: "evt_1001", CallerKey: "ck_aaa"}
	identA  = harness.Identity{Merchant: "svc_orders", Op: "publish", Resource: "evt_2001", CallerKey: "ck_shared"}
	identB  = harness.Identity{Merchant: "svc_billing", Op: "publish", Resource: "evt_3001", CallerKey: "ck_shared"}
)

func requests() map[string]Request {
	return map[string]Request{
		"r1":  {ID: "r1", Identity: identE1, Payload: harness.Money{Amount: 1000, Currency: "USD"}},
		"r1b": {ID: "r1b", Identity: identE1, Payload: harness.Money{Amount: 2000, Currency: "USD"}}, // changed payload
		"rA":  {ID: "rA", Identity: identA, Payload: harness.Money{Amount: 1000, Currency: "USD"}},
		"rB":  {ID: "rB", Identity: identB, Payload: harness.Money{Amount: 1500, Currency: "USD"}},
	}
}

func program(f Factory, req Request) harness.Program {
	return func(env harness.Env) {
		svc := f(env)
		env.SetResponse(svc.Publish(req))
	}
}

// TaskFor binds a candidate factory to the kernel for this family.
func TaskFor(f Factory) harness.Task {
	reqs := requests()
	return harness.Task{
		Profile:           Profile,
		NewRequestProgram: func(id string) harness.Program { return program(f, reqs[id]) },
		// A recovery pass models an outbox relay re-scanning the in-flight record.
		// For a retry-safe candidate this reconciles without a duplicate publish.
		NewRecoverProgram: func() harness.Program { return program(f, reqs["r1"]) },
	}
}

// Candidate names a factory and, for mutants, the invariant it is designed to
// violate (used by the mutation / oracle-validity test).
type Candidate struct {
	Name    string
	Factory Factory
	Correct bool
	Bug     harness.Invariant
}

// Candidates is the full set shipped with the family: one correct reference and
// five seeded-bug mutants.
func Candidates() []Candidate {
	return []Candidate{
		{"correct", Correct, true, ""},
		{"m1_unstable_identity", M1, false, harness.InvAtMostOne},
		{"m2_no_payload_check", M2, false, harness.InvPayloadConsistency},
		{"m3_raw_key_only", M3, false, harness.InvNoFalseDedup},
		{"m4_vacuous_no_publish", M4, false, harness.InvNoLostEffect},
		{"m5_recovery_loops", M5, false, harness.InvRecover},
	}
}

// Case pairs a schedule with its expected outcome (a shared harness.Spec).
type Case struct {
	Sch harness.Schedule
	Exp harness.Spec
}

// Cases returns the public and hidden fault schedules for the family.
func Cases() []Case {
	return []Case{
		{
			Sch: harness.Schedule{
				ID: "outbox-happy", Seed: 1, Hidden: false,
				Note: "single publish, no faults",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run", Inst: "s1"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{identE1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "outbox-concurrent-retry", Seed: 2, Hidden: false,
				Note: "two relays handle the same event identity concurrently",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "start", Inst: "s2", Req: "r1"},
					{Op: "step", Inst: "s1"},
					{Op: "step", Inst: "s2"},
					{Op: "run", Inst: "s1"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{identE1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "outbox-payload-conflict", Seed: 3, Hidden: false,
				Note: "replay reuses the event id with a changed payload",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run", Inst: "s1"},
					{Op: "start", Inst: "s2", Req: "r1b"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{identE1}, Conflicts: []string{"s2"}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "outbox-false-dedup", Seed: 4, Hidden: false,
				Note: "two streams reuse the same caller key (distinct events)",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "rA"},
					{Op: "run", Inst: "s1"},
					{Op: "start", Inst: "s2", Req: "rB"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 2, Eligible: []harness.Identity{identA, identB}, DistinctIdentities: 2},
		},
		{
			Sch: harness.Schedule{
				ID: "outbox-crash-after-effect", Seed: 5, Hidden: true,
				Note: "event published, response dropped, process crashes, then recovery",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run_until", Inst: "s1", Arg: "charge"},
					{Op: "fault", Inst: "s1", Fault: "drop_response"},
					{Op: "crash", Inst: "s1"},
					{Op: "recover"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{identE1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "outbox-unknown-timeout-recover", Seed: 6, Hidden: true,
				Note: "bus commits the publish but returns an unknown outcome, then recovery",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run_until", Inst: "s1", Arg: "charge"},
					{Op: "fault", Inst: "s1", Fault: "provider_timeout_after_commit"},
					{Op: "run", Inst: "s1"},
					{Op: "recover"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{identE1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "outbox-crash-before-reserve", Seed: 7, Hidden: true,
				Note: "process crashes before the business commit (reserving the record), then recovery",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run_until", Inst: "s1", Arg: "reserve"},
					{Op: "crash", Inst: "s1"},
					{Op: "recover"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{identE1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "outbox-crash-before-charge", Seed: 8, Hidden: true,
				Note: "process crashes after the business commit but before the publish, then recovery",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run_until", Inst: "s1", Arg: "charge"},
					{Op: "crash", Inst: "s1"},
					{Op: "recover"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{identE1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "outbox-payload-conflict-hidden", Seed: 9, Hidden: true,
				Note: "hidden payload-conflict coverage (changed payload on replay)",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run", Inst: "s1"},
					{Op: "start", Inst: "s2", Req: "r1b"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{identE1}, Conflicts: []string{"s2"}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "outbox-false-dedup-hidden", Seed: 10, Hidden: true,
				Note: "hidden false-deduplication coverage (two streams, shared caller key)",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "rA"},
					{Op: "run", Inst: "s1"},
					{Op: "start", Inst: "s2", Req: "rB"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 2, Eligible: []harness.Identity{identA, identB}, DistinctIdentities: 2},
		},
	}
}

// PublicCases and HiddenCases partition the schedules the way the benchmark
// contract does: public schedules are for development; hidden ones score.
func PublicCases() []Case {
	var out []Case
	for _, c := range Cases() {
		if !c.Sch.Hidden {
			out = append(out, c)
		}
	}
	return out
}

// HiddenCases returns only the hidden (scoring) schedules.
func HiddenCases() []Case {
	var out []Case
	for _, c := range Cases() {
		if c.Sch.Hidden {
			out = append(out, c)
		}
	}
	return out
}
