// Command t4run executes the benchmark pilot: it runs every candidate against
// every schedule in every registered family, writes the raw result records to
// results/pilot_results.json, and prints a human-readable summary including the
// survival curve R_k over the hidden schedules.
//
// The raw JSON is the input to analysis/make_figures.py, which regenerates the
// paper figures. Because the harness is deterministic, re-running this command
// reproduces identical results.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	"t4bench/harness"
	"t4bench/tasks/capture"
	"t4bench/tasks/consumer"
	"t4bench/tasks/ledger"
	"t4bench/tasks/outbox"
	"t4bench/tasks/reconciliation"
	"t4bench/tasks/refund"
	"t4bench/tasks/saga"
)

func main() {
	var results []harness.Result
	results = append(results, capture.RunAll()...)
	results = append(results, refund.RunAll()...)
	results = append(results, outbox.RunAll()...)
	results = append(results, consumer.RunAll()...)
	results = append(results, ledger.RunAll()...)
	results = append(results, saga.RunAll()...)
	results = append(results, reconciliation.RunAll()...)

	if err := os.MkdirAll("results", 0o755); err != nil {
		fmt.Fprintln(os.Stderr, "mkdir results:", err)
		os.Exit(1)
	}
	out := filepath.Join("results", "pilot_results.json")
	f, err := os.Create(out)
	if err != nil {
		fmt.Fprintln(os.Stderr, "create results:", err)
		os.Exit(1)
	}
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	if err := enc.Encode(results); err != nil {
		fmt.Fprintln(os.Stderr, "encode:", err)
		os.Exit(1)
	}
	f.Close()

	fmt.Printf("wrote %d result records to %s\n\n", len(results), out)
	summarize(results)
	demoShrink()
}

// summarize prints, per candidate, the hidden-schedule pass rate and the R_k
// survival curve — the real, measured analogue of the paper's reliability curve.
func summarize(results []harness.Result) {
	type agg struct {
		correct           bool
		hiddenPass, hidden int
		allPass, all       int
	}
	byCand := map[string]*agg{}
	var order []string
	for _, r := range results {
		a, ok := byCand[r.Candidate]
		if !ok {
			a = &agg{correct: r.CorrectRef}
			byCand[r.Candidate] = a
			order = append(order, r.Candidate)
		}
		a.all++
		if r.OK {
			a.allPass++
		}
		if r.Hidden {
			a.hidden++
			if r.OK {
				a.hiddenPass++
			}
		}
	}
	sort.Strings(order)

	fmt.Println("candidate                    all      hidden   survival R_k over hidden set (k=1..n)")
	fmt.Println("---------------------------------------------------------------------------------------")
	for _, name := range order {
		a := byCand[name]
		curve := ""
		for k := 1; k <= a.hidden; k++ {
			curve += fmt.Sprintf(" %0.2f", harness.SurvivalRk(a.hiddenPass, a.hidden, k))
		}
		tag := "mutant "
		if a.correct {
			tag = "CORRECT"
		}
		fmt.Printf("%-28s %2d/%-2d    %2d/%-2d  %s  [%s]\n",
			name, a.allPass, a.all, a.hiddenPass, a.hidden, curve, tag)
	}
	fmt.Println()
}

// demoShrink shows the delta-debugging shrinker turning a failing schedule into a
// minimal counterexample.
func demoShrink() {
	var crashCase capture.Case
	for _, c := range capture.Cases() {
		if c.Sch.ID == "capture-crash-after-effect" {
			crashCase = c
		}
	}
	min := capture.ShrinkWitness(capture.M1, crashCase, harness.InvAtMostOne)
	fmt.Printf("trace shrinking (m1_unstable_identity, duplicate-effect witness):\n")
	fmt.Printf("  original schedule: %d events\n", len(crashCase.Sch.Events))
	fmt.Printf("  minimized witness: %d events\n", len(min.Events))
	for _, e := range min.Events {
		line := "    " + e.Op
		if e.Inst != "" {
			line += " " + e.Inst
		}
		if e.Req != "" {
			line += " " + e.Req
		}
		if e.Arg != "" {
			line += " " + e.Arg
		}
		if e.Fault != "" {
			line += " [" + e.Fault + "]"
		}
		fmt.Println(line)
	}
}
