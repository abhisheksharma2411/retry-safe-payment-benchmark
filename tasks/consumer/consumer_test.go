package consumer

import (
	"testing"

	"t4bench/harness"
)

// TestCorrectReferencePassesAll is the primary oracle-validity direction: the
// dedup-safe reference must satisfy every invariant on every schedule.
func TestCorrectReferencePassesAll(t *testing.T) {
	for _, c := range Cases() {
		obs := harness.Run(c.Sch, TaskFor(Correct))
		res := harness.CheckFinancial(obs, c.Exp)
		if !res.OK() {
			t.Errorf("correct reference FAILED on %s: violations=%v detail=%v",
				c.Sch.ID, res.Violations(), res.Detail)
		}
	}
}

// TestMutantsAreCaught is the other direction: every seeded bug must be detected,
// and on at least one schedule the detected violation must include the invariant
// the mutant was designed to break (no accidental / off-target detection only).
func TestMutantsAreCaught(t *testing.T) {
	for _, cand := range Candidates() {
		if cand.Correct {
			continue
		}
		everCaught := false
		targetHit := false
		for _, c := range Cases() {
			obs := harness.Run(c.Sch, TaskFor(cand.Factory))
			res := harness.CheckFinancial(obs, c.Exp)
			if !res.OK() {
				everCaught = true
				for _, v := range res.Violations() {
					if v == cand.Bug {
						targetHit = true
					}
				}
			}
		}
		if !everCaught {
			t.Errorf("mutant %s was never caught by any schedule", cand.Name)
		}
		if !targetHit {
			t.Errorf("mutant %s was caught, but never via its target invariant %q", cand.Name, cand.Bug)
		}
	}
}

// TestDeterminism runs each (candidate, schedule) twice and requires identical
// observations and traces. This is the reproducibility contract (threat T7).
func TestDeterminism(t *testing.T) {
	for _, cand := range Candidates() {
		for _, c := range Cases() {
			o1, tr1 := harness.RunWithTrace(c.Sch, TaskFor(cand.Factory))
			o2, tr2 := harness.RunWithTrace(c.Sch, TaskFor(cand.Factory))
			if len(tr1) != len(tr2) {
				t.Fatalf("%s/%s: trace length differs %d vs %d", cand.Name, c.Sch.ID, len(tr1), len(tr2))
			}
			for i := range tr1 {
				if tr1[i] != tr2[i] {
					t.Fatalf("%s/%s: trace differs at %d: %q vs %q", cand.Name, c.Sch.ID, i, tr1[i], tr2[i])
				}
			}
			if len(o1.Debits) != len(o2.Debits) {
				t.Fatalf("%s/%s: debit count differs %d vs %d", cand.Name, c.Sch.ID, len(o1.Debits), len(o2.Debits))
			}
		}
	}
}
