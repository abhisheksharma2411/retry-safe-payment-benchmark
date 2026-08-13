package harness

import "math/big"

// Result is the structured record produced for one (candidate, schedule) run.
// These records are the raw material for every metric and figure; they are
// archived so results can be regenerated without re-running the harness.
type Result struct {
	Family        string           `json:"family"`
	SemanticTask  string           `json:"semantic_task_id"`
	Candidate     string           `json:"candidate"`
	CorrectRef    bool             `json:"correct_ref"`
	ScheduleID    string           `json:"schedule_id"`
	Seed          int64            `json:"seed"`
	Hidden        bool             `json:"hidden"`
	CompileStatus string           `json:"compile_status"` // success | error
	RuntimeStatus string           `json:"runtime_status"` // completed | nonconvergent | crashed
	Invariants    map[string]bool  `json:"invariants"`
	Violations    []string         `json:"violations"`
	OK            bool             `json:"ok"`
}

// NewResult builds a Result from an OracleResult.
func NewResult(family, semantic, candidate string, correct bool, sch Schedule, runtime string, res OracleResult) Result {
	inv := map[string]bool{}
	for k, v := range res.Passed {
		inv[string(k)] = v
	}
	var viol []string
	for _, v := range res.Violations() {
		viol = append(viol, string(v))
	}
	return Result{
		Family: family, SemanticTask: semantic, Candidate: candidate, CorrectRef: correct,
		ScheduleID: sch.ID, Seed: sch.Seed, Hidden: sch.Hidden,
		CompileStatus: "success", RuntimeStatus: runtime,
		Invariants: inv, Violations: viol, OK: res.OK(),
	}
}

// SurvivalRk computes R_k = C(m,k)/C(n,k): the probability that a candidate which
// passed m of n schedules survives k schedules sampled without replacement. This
// is the reliability measure the paper adopts (renamed from pass^k), and it does
// NOT assume schedules are independent — see review issue T4.
func SurvivalRk(m, n, k int) float64 {
	if k <= 0 {
		return 1
	}
	if k > n || k > m {
		if k > m {
			return 0
		}
	}
	num := choose(m, k)
	den := choose(n, k)
	if den.Sign() == 0 {
		return 0
	}
	q := new(big.Rat).SetFrac(num, den)
	f, _ := q.Float64()
	return f
}

func choose(n, k int) *big.Int {
	if k < 0 || k > n {
		return big.NewInt(0)
	}
	return new(big.Int).Binomial(int64(n), int64(k))
}
