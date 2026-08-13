package harness

import (
	"fmt"
	"testing"
)

// helper: a task whose request program is chosen by request id, for exercising
// kernel primitives directly (independent of any task family).
func primTask(programs map[string]Program) Task {
	return Task{
		Profile:           Queryable,
		NewRequestProgram: func(id string) Program { return programs[id] },
	}
}

// Reserve must be atomic: exactly one of two racing instances wins.
func TestReserveAtomicity(t *testing.T) {
	p := func(env Env) {
		ok := env.Store().Reserve("k", "fp")
		env.SetResponse(Response{Status: fmt.Sprintf("%v", ok)})
	}
	sch := Schedule{ID: "reserve", Seed: 1, Events: []Event{
		{Op: "start", Inst: "a", Req: "r"},
		{Op: "start", Inst: "b", Req: "r"},
		{Op: "run", Inst: "a"},
		{Op: "run", Inst: "b"},
	}}
	obs := Run(sch, primTask(map[string]Program{"r": p}))
	if obs.Responses["a"].Status != "true" {
		t.Fatalf("first reserver should win, got %q", obs.Responses["a"].Status)
	}
	if obs.Responses["b"].Status != "false" {
		t.Fatalf("second reserver should lose, got %q", obs.Responses["b"].Status)
	}
}

// Under an idempotent profile, charging the same identity twice yields one debit;
// charging distinct identities yields two.
func TestProviderDedupAndDistinct(t *testing.T) {
	id := Identity{Merchant: "m", Op: "capture", Resource: "r", CallerKey: "k"}
	same := func(env Env) {
		env.Provider().Charge(id, Money{Amount: 100, Currency: "USD"})
	}
	sch := Schedule{ID: "dedup", Seed: 1, Events: []Event{
		{Op: "start", Inst: "a", Req: "r"}, {Op: "run", Inst: "a"},
		{Op: "start", Inst: "b", Req: "r"}, {Op: "run", Inst: "b"},
	}}
	obs := Run(sch, primTask(map[string]Program{"r": same}))
	if len(obs.Debits) != 1 {
		t.Fatalf("idempotent profile should dedup to 1 debit, got %d", len(obs.Debits))
	}

	id2 := id
	id2.Merchant = "m2"
	i := 0
	distinct := func(env Env) {
		if i == 0 {
			env.Provider().Charge(id, Money{Amount: 100, Currency: "USD"})
		} else {
			env.Provider().Charge(id2, Money{Amount: 100, Currency: "USD"})
		}
		i++
	}
	obs2 := Run(sch, primTask(map[string]Program{"r": distinct}))
	if len(obs2.Debits) != 2 {
		t.Fatalf("distinct identities should produce 2 debits, got %d", len(obs2.Debits))
	}
}

// A crash after a durable reservation loses volatile memory but keeps the
// durable record.
func TestCrashKeepsDurableLosesVolatile(t *testing.T) {
	p := func(env Env) {
		env.Store().Reserve("k", "fp")
		// park at a provider charge; the schedule crashes us here.
		env.Provider().Charge(Identity{CallerKey: "k"}, Money{Amount: 1, Currency: "USD"})
		// never reached
		env.Store().Complete("k", "should-not-happen")
	}
	sch := Schedule{ID: "crash", Seed: 1, Events: []Event{
		{Op: "start", Inst: "a", Req: "r"},
		{Op: "run_until", Inst: "a", Arg: "charge"},
		{Op: "crash", Inst: "a"},
	}}
	obs := Run(sch, primTask(map[string]Program{"r": p}))
	rec, ok := obs.Store["k"]
	if !ok || rec.State != "reserved" {
		t.Fatalf("durable reservation should survive crash, got %+v ok=%v", rec, ok)
	}
	if len(obs.Debits) != 0 {
		t.Fatalf("charge was never allowed to execute, expected 0 debits, got %d", len(obs.Debits))
	}
}

// drop_response commits the effect on the rail but leaves the candidate parked
// without an acknowledgement.
func TestDropResponseCommitsButNoAck(t *testing.T) {
	got := ""
	p := func(env Env) {
		env.Store().Reserve("k", "fp")
		_, err := env.Provider().Charge(Identity{CallerKey: "k"}, Money{Amount: 1, Currency: "USD"})
		got = fmt.Sprintf("%v", err) // only runs if resumed after the drop
		env.SetResponse(Response{Status: got})
	}
	sch := Schedule{ID: "drop", Seed: 1, Events: []Event{
		{Op: "start", Inst: "a", Req: "r"},
		{Op: "run_until", Inst: "a", Arg: "charge"},
		{Op: "fault", Inst: "a", Fault: "drop_response"},
		// no resume: the effect committed, the ack is lost.
	}}
	obs := Run(sch, primTask(map[string]Program{"r": p}))
	if len(obs.Debits) != 1 {
		t.Fatalf("dropped response should still commit the effect, got %d debits", len(obs.Debits))
	}
	if _, ok := obs.Responses["a"]; ok {
		t.Fatalf("candidate should not have produced a response after a dropped ack")
	}
}
