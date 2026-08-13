package consumer

import "t4bench/harness"

// Rail profile assumed by this family (stated explicitly per the benchmark
// contract). See package doc and paper Sec. "Fault-Injection Harness".
const Profile = harness.Queryable

// Identities and requests used by the schedules. Op is always "process" and
// Resource is the message id; the CallerKey models a client-supplied delivery
// dedup token.
var (
	msgI1 = harness.Identity{Merchant: "q_orders", Op: "process", Resource: "msg_1001", CallerKey: "ck_aaa"}
	msgA  = harness.Identity{Merchant: "q_orders", Op: "process", Resource: "msg_2001", CallerKey: "ck_shared"}
	msgB  = harness.Identity{Merchant: "q_billing", Op: "process", Resource: "msg_3001", CallerKey: "ck_shared"}
)

func requests() map[string]Request {
	return map[string]Request{
		"r1":  {ID: "r1", Identity: msgI1, Payload: harness.Money{Amount: 1000, Currency: "USD"}},
		"r1b": {ID: "r1b", Identity: msgI1, Payload: harness.Money{Amount: 2000, Currency: "USD"}}, // same message id, changed payload
		"rA":  {ID: "rA", Identity: msgA, Payload: harness.Money{Amount: 1000, Currency: "USD"}},
		"rB":  {ID: "rB", Identity: msgB, Payload: harness.Money{Amount: 1500, Currency: "USD"}},
	}
}

func program(f Factory, req Request) harness.Program {
	return func(env harness.Env) {
		svc := f(env)
		env.SetResponse(svc.Consume(req))
	}
}

// TaskFor binds a candidate factory to the kernel for this family.
func TaskFor(f Factory) harness.Task {
	reqs := requests()
	return harness.Task{
		Profile:           Profile,
		NewRequestProgram: func(id string) harness.Program { return program(f, reqs[id]) },
		// A recovery pass models the consumer restarting and re-handling the
		// in-flight delivery. For a dedup-safe candidate this reconciles without a
		// duplicate effect.
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
		{"m4_vacuous_no_effect", M4, false, harness.InvNoLostEffect},
		{"m5_recovery_loops", M5, false, harness.InvRecover},
	}
}

// Case pairs a schedule with its expected outcome (a shared harness.Spec).
type Case struct {
	Sch harness.Schedule
	Exp harness.Spec
}

// Cases returns the public and hidden fault schedules for the family, themed to
// duplicate / reordered delivery.
func Cases() []Case {
	return []Case{
		{
			Sch: harness.Schedule{
				ID: "consumer-happy", Seed: 1, Hidden: false,
				Note: "single delivery, no faults",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run", Inst: "s1"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{msgI1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "consumer-duplicate-delivery", Seed: 2, Hidden: false,
				Note: "the same message is delivered twice concurrently",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "start", Inst: "s2", Req: "r1"},
					{Op: "step", Inst: "s1"},
					{Op: "step", Inst: "s2"},
					{Op: "run", Inst: "s1"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{msgI1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "consumer-payload-conflict", Seed: 3, Hidden: false,
				Note: "redelivery reuses the message id with a changed payload",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run", Inst: "s1"},
					{Op: "start", Inst: "s2", Req: "r1b"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{msgI1}, Conflicts: []string{"s2"}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "consumer-false-dedup", Seed: 4, Hidden: false,
				Note: "two distinct messages share the same caller key (distinct identities)",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "rA"},
					{Op: "run", Inst: "s1"},
					{Op: "start", Inst: "s2", Req: "rB"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 2, Eligible: []harness.Identity{msgA, msgB}, DistinctIdentities: 2},
		},
		{
			Sch: harness.Schedule{
				ID: "consumer-crash-after-effect", Seed: 5, Hidden: true,
				Note: "provider commits the effect, response dropped, consumer crashes, then recovery",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run_until", Inst: "s1", Arg: "charge"},
					{Op: "fault", Inst: "s1", Fault: "drop_response"},
					{Op: "crash", Inst: "s1"},
					{Op: "recover"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{msgI1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "consumer-unknown-timeout-recover", Seed: 6, Hidden: true,
				Note: "provider commits but returns an unknown outcome, then recovery",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run_until", Inst: "s1", Arg: "charge"},
					{Op: "fault", Inst: "s1", Fault: "provider_timeout_after_commit"},
					{Op: "run", Inst: "s1"},
					{Op: "recover"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{msgI1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "consumer-crash-before-reserve", Seed: 7, Hidden: true,
				Note: "consumer crashes before writing the dedup marker, then recovery",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run_until", Inst: "s1", Arg: "reserve"},
					{Op: "crash", Inst: "s1"},
					{Op: "recover"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{msgI1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "consumer-crash-before-charge", Seed: 8, Hidden: true,
				Note: "consumer crashes after reserving but before the effect, then recovery",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run_until", Inst: "s1", Arg: "charge"},
					{Op: "crash", Inst: "s1"},
					{Op: "recover"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{msgI1}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "consumer-payload-conflict-hidden", Seed: 9, Hidden: true,
				Note: "hidden payload-conflict coverage (changed payload on redelivery)",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "r1"},
					{Op: "run", Inst: "s1"},
					{Op: "start", Inst: "s2", Req: "r1b"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 1, Eligible: []harness.Identity{msgI1}, Conflicts: []string{"s2"}, DistinctIdentities: 1},
		},
		{
			Sch: harness.Schedule{
				ID: "consumer-false-dedup-hidden", Seed: 10, Hidden: true,
				Note: "hidden false-deduplication coverage (two distinct messages, shared caller key)",
				Events: []harness.Event{
					{Op: "start", Inst: "s1", Req: "rA"},
					{Op: "run", Inst: "s1"},
					{Op: "start", Inst: "s2", Req: "rB"},
					{Op: "run", Inst: "s2"},
				},
			},
			Exp: harness.Spec{TotalEffects: 2, Eligible: []harness.Identity{msgA, msgB}, DistinctIdentities: 2},
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
