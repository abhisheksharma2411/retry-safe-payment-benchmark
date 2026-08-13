package harness

import "sort"

// Invariant names the financial-safety properties the oracle checks. They are
// reconstructed from the independent Observation (provider effects log + durable
// store + observable responses), never from the candidate's internal status.
type Invariant string

const (
	// InvAtMostOne: no duplicate external effect — the number of effects for the
	// intended operation never exceeds what the schedule expects, and no single
	// identity is charged more than once.
	InvAtMostOne Invariant = "at_most_one"
	// InvNoLostEffect: a valid request that eventually stabilized produced the
	// required effect (liveness). Guards against vacuous success.
	InvNoLostEffect Invariant = "no_lost_effect"
	// InvNoFalseDedup: distinct valid identities are not collapsed into one.
	InvNoFalseDedup Invariant = "no_false_dedup"
	// InvPayloadConsistency: reusing an identity with a different payload is
	// rejected with a conflict and produces no additional effect.
	InvPayloadConsistency Invariant = "payload_consistency"
	// InvReproducible: a completed operation reports a stable reference.
	InvReproducible Invariant = "reproducible"
	// InvRecover: after faults stop, the service reaches a terminal, consistent
	// state within a bounded number of steps (guards against IN_PROGRESS-forever).
	InvRecover Invariant = "recovery"
)

// AllInvariants is the canonical ordered list, most-severe first.
var AllInvariants = []Invariant{
	InvAtMostOne, InvNoLostEffect, InvNoFalseDedup,
	InvPayloadConsistency, InvReproducible, InvRecover,
}

// OracleResult records, per invariant, whether the candidate satisfied it under
// one schedule, with a short human-readable reason on failure.
type OracleResult struct {
	Passed map[Invariant]bool
	Detail map[Invariant]string
}

// NewOracleResult returns a result with every invariant defaulted to passing.
func NewOracleResult() OracleResult {
	p := map[Invariant]bool{}
	for _, inv := range AllInvariants {
		p[inv] = true
	}
	return OracleResult{Passed: p, Detail: map[Invariant]string{}}
}

// Fail marks an invariant as violated (idempotent: the first reason is kept).
func (r OracleResult) Fail(inv Invariant, why string) {
	if r.Passed[inv] {
		r.Passed[inv] = false
		r.Detail[inv] = why
	}
}

// OK reports whether every checked invariant passed.
func (r OracleResult) OK() bool {
	for _, inv := range AllInvariants {
		if v, ok := r.Passed[inv]; ok && !v {
			return false
		}
	}
	return true
}

// Violations returns the failed invariants in canonical (severity) order.
func (r OracleResult) Violations() []Invariant {
	var out []Invariant
	for _, inv := range AllInvariants {
		if v, ok := r.Passed[inv]; ok && !v {
			out = append(out, inv)
		}
	}
	return out
}

// Spec encodes the per-schedule ground truth the oracle scores against. It is
// authored by hand from a schedule's semantics — never derived from a candidate
// — so scoring stays independent of the implementation under test. Every task
// family reuses this one spec + checker so scoring is uniform across families.
type Spec struct {
	// TotalEffects is the exact number of external effects that must exist on the
	// rail after the schedule stabilizes (faults stopped, recovery run).
	TotalEffects int
	// Eligible identities must each end resolved after recovery (informational;
	// convergence itself is judged from the observation).
	Eligible []Identity
	// Conflicts lists instance ids whose observable response must be CONFLICT.
	Conflicts []string
	// DistinctIdentities is the number of distinct effect-identities expected
	// (used to detect false deduplication / collapse).
	DistinctIdentities int
}

// CheckFinancial reconstructs the outcome from the independent Observation and
// evaluates every financial-safety invariant, returning one OracleResult per
// schedule. All task families use this checker.
func CheckFinancial(obs Observation, spec Spec) OracleResult {
	r := NewOracleResult()
	byID := obs.DebitsByIdentity()
	total := len(obs.Debits)

	// InvAtMostOne: no extra effect overall, and no identity charged twice.
	if total > spec.TotalEffects {
		r.Fail(InvAtMostOne, "observed more effects than expected")
	}
	for _, k := range obs.SortedIdentityKeys() {
		if len(byID[k]) > 1 {
			r.Fail(InvAtMostOne, "duplicate effect for identity "+k)
		}
	}
	// InvNoLostEffect: the required effects were actually produced.
	if total < spec.TotalEffects {
		r.Fail(InvNoLostEffect, "fewer effects than expected (possible lost or vacuous)")
	}
	// InvNoFalseDedup: distinct valid identities were not collapsed. This requires
	// at least one observed effect. A run that produced no effects at all has
	// already failed InvNoLostEffect, and its distinct-identity count is trivially
	// zero — reporting a "collapse" there would charge one underlying failure
	// (nothing happened) to two independent invariants and overstate the fault
	// count for vacuous-success candidates.
	if spec.DistinctIdentities > 0 && total > 0 &&
		DistinctIdentityCount(obs.Debits) < spec.DistinctIdentities {
		r.Fail(InvNoFalseDedup, "distinct valid identities were collapsed")
	}
	// InvPayloadConsistency: conflicting replays were rejected with CONFLICT.
	for _, inst := range spec.Conflicts {
		if obs.Responses[inst].Status != "CONFLICT" {
			r.Fail(InvPayloadConsistency, "instance "+inst+" did not return CONFLICT")
		}
	}
	// InvReproducible: a completed store record agrees with the effect reference.
	for _, id := range spec.Eligible {
		rec, ok := obs.Store[id.Key()]
		if ok && rec.State == "completed" {
			if ds := byID[id.Key()]; len(ds) == 1 && ds[0].Ref != rec.Ref {
				r.Fail(InvReproducible, "store reference != effect reference for "+id.Key())
			}
		}
	}
	// InvRecover: converged within the step bound (guards IN_PROGRESS-forever).
	if obs.NonConvergent {
		r.Fail(InvRecover, "service did not converge within the step bound")
	}
	return r
}

// DistinctIdentityCount returns how many distinct identities appear in the
// effects log (deterministic).
func DistinctIdentityCount(debits []Debit) int {
	seen := map[string]bool{}
	for _, d := range debits {
		seen[d.ID.Key()] = true
	}
	return len(seen)
}

// SortedKeys is a small deterministic helper for oracles that must iterate a map.
func SortedKeys[T any](m map[string]T) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
