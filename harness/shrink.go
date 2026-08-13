package harness

// Shrink reduces a failing schedule to a locally minimal sub-sequence of events
// that still reproduces the failure, using delta debugging (greedy
// 1-minimality: no single remaining event can be removed without the failure
// disappearing). The predicate reports whether a schedule still exhibits the
// failure of interest (e.g. a specific violated invariant).
//
// A minimized schedule is the short counterexample a maintainer replays, instead
// of a thousand-line trace.
func Shrink(sch Schedule, fails func(Schedule) bool) Schedule {
	if !fails(sch) {
		return sch
	}
	events := make([]Event, len(sch.Events))
	copy(events, sch.Events)

	changed := true
	for changed {
		changed = false
		for i := 0; i < len(events); i++ {
			trial := make([]Event, 0, len(events)-1)
			trial = append(trial, events[:i]...)
			trial = append(trial, events[i+1:]...)
			cand := sch
			cand.Events = trial
			if fails(cand) {
				events = trial
				changed = true
				break
			}
		}
	}
	out := sch
	out.Events = events
	return out
}
