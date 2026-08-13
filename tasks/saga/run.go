package saga

import "t4bench/harness"

// Family is the family identifier used in result records.
const Family = "saga"

// RunAll executes every candidate against every schedule and returns one Result
// per (candidate, schedule). This is the family's contribution to the pilot.
func RunAll() []harness.Result {
	var out []harness.Result
	for _, cand := range Candidates() {
		for _, c := range Cases() {
			obs := harness.Run(c.Sch, TaskFor(cand.Factory))
			res := harness.CheckFinancial(obs, c.Exp)
			runtime := "completed"
			if obs.NonConvergent {
				runtime = "nonconvergent"
			}
			out = append(out, harness.NewResult(Family, "saga-t1", cand.Name, cand.Correct, c.Sch, runtime, res))
		}
	}
	return out
}

// ShrinkWitness returns a minimal sub-schedule that still causes the named
// candidate to violate the given invariant on the given case, using the
// harness delta-debugging shrinker.
func ShrinkWitness(cand Factory, c Case, target harness.Invariant) harness.Schedule {
	fails := func(sch harness.Schedule) bool {
		obs := harness.Run(sch, TaskFor(cand))
		res := harness.CheckFinancial(obs, c.Exp)
		return !res.Passed[target]
	}
	return harness.Shrink(c.Sch, fails)
}
