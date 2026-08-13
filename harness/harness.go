// Package harness implements a single-process, seeded, deterministic-simulation
// (DST) kernel for evaluating the retry-safety of generated payment services.
//
// The kernel gives each candidate implementation injected Store / Provider /
// Clock / Rand interfaces. Candidate code never touches the real clock, real
// randomness, real networks, real threads, or real payment rails: every
// externally observable interaction is routed through the kernel, which drives
// execution one operation at a time according to a serializable Schedule. This
// is what makes a failing run reproducible and reducible to a short witness.
//
// Determinism notes (see paper Sec. "Fault-Injection Harness", threat T7):
//   - Exactly one candidate goroutine is ever runnable at a time. Every injected
//     operation parks the calling goroutine until the scheduler releases it, so
//     Go's goroutine scheduler cannot introduce nondeterminism.
//   - The effects ledger is an ordered slice (never a map) so iteration order is
//     stable. Where maps are unavoidable (durable store keyed by string) they are
//     only ever accessed by explicit key, and any observation that iterates is
//     sorted before use.
//   - Randomness is a seeded math/rand source; logical time is a monotonic
//     counter. Neither reads the wall clock.
package harness

import (
	"errors"
	"fmt"
	"math/rand"
	"sort"
)

// ErrTimeout models an unknown outcome: the candidate's call to the provider did
// not return a definitive answer. The external effect may or may not have
// happened; the candidate must not assume either way.
var ErrTimeout = errors.New("provider: unknown outcome (timeout)")

// ErrPartition models a network partition between the candidate and the rail.
var ErrPartition = errors.New("provider: partition")

// Money is a currency amount in minor units (e.g. cents).
type Money struct {
	Amount   int64
	Currency string
}

// Identity is the *full* operation identity. The paper (and threat T22) require
// that "same identity" be unambiguous. A raw caller idempotency key is not
// enough; identity must scope the merchant/account, the operation type, and the
// resource, so that two different merchants reusing the same client key are not
// falsely deduplicated.
type Identity struct {
	Merchant  string
	Op        string
	Resource  string
	CallerKey string
}

// Key returns the canonical string form used for provider-side deduplication.
func (id Identity) Key() string {
	return id.Merchant + "|" + id.Op + "|" + id.Resource + "|" + id.CallerKey
}

// Response is the observable outcome the candidate returns to its caller.
type Response struct {
	Status string // OK | CONFLICT | IN_PROGRESS | FAILED | ERROR
	Ref    string
	Err    string
}

// Record is a durable store record. Durable state survives crashes; anything a
// candidate holds in Go memory does not.
type Record struct {
	State       string // "" | reserved | completed | failed
	Fingerprint string
	Ref         string
	ErrCode     string
}

// Store is the durable, crash-surviving key/value state injected into candidates.
type Store interface {
	Get(key string) (Record, bool)
	// Reserve atomically creates a "reserved" record iff none exists. It returns
	// true only to the caller that created the reservation. This single atomic
	// primitive is what lets a retry-safe candidate claim an identity before
	// producing an external effect.
	Reserve(key, fingerprint string) bool
	Complete(key, ref string)
	Fail(key, errCode string)
	// Put is a low-level unconditional write, used by deliberately naive
	// (buggy) reference implementations.
	Put(key string, rec Record)
}

// Provider is the external effects log (the "payment rail"). Every externally
// effecting operation — a capture debit, a refund credit, an outbox publish, a
// saga step, a compensation — is a Charge on this log, keyed by Identity. The
// observer reconstructs financial truth from this log, independently of whatever
// the candidate stored about itself.
type Provider interface {
	// Charge produces (or, under an idempotent profile, deduplicates) one effect
	// for the given identity. Returns a stable reference on success.
	Charge(id Identity, amt Money) (ref string, err error)
	// Query returns the reference of an existing effect for id, if any. This is
	// how a recovery pass reconciles an unknown outcome without risking a
	// duplicate.
	Query(id Identity) (ref string, found bool, err error)
}

// Clock is injected logical time (monotonic counter, never the wall clock).
type Clock interface{ Now() int64 }

// Rand is injected deterministic randomness (seeded per schedule).
type Rand interface{ Intn(n int) int }

// Env is handed to a candidate program and is bound to one instance (one
// process). Two instances of the same service share the durable Store and the
// Provider effects log but have independent volatile memory.
type Env interface {
	Store() Store
	Provider() Provider
	Clock() Clock
	Rand() Rand
	// SetResponse records the candidate's observable response for this instance.
	SetResponse(Response)
}

// Program is one candidate execution: a request handler or a recovery pass.
type Program func(env Env)

// RailProfile selects the provider's deduplication semantics. The benchmark must
// state which profile it assumes (threat T13): the reserve-then-effect pattern
// is only *safe* under some of them.
type RailProfile int

const (
	// StrongIdempotency: the same identity stays deduplicated for the whole run.
	StrongIdempotency RailProfile = iota
	// Queryable: no automatic dedup, but the service can reconcile via Query.
	Queryable
	// WeakRail: no native provider dedup and no reliable query. A naive
	// re-charge on this profile double-charges.
	WeakRail
)

// Task binds a family's request payloads and recovery logic to the kernel.
type Task struct {
	Profile           RailProfile
	NewRequestProgram func(reqID string) Program
	NewRecoverProgram func() Program
}

// Event is one serializable scheduler transition.
type Event struct {
	Op    string `json:"op"`              // start|step|run|run_until|fault|crash|recover|faults_stop
	Inst  string `json:"inst,omitempty"`  // target instance id
	Req   string `json:"req,omitempty"`   // request id (for start)
	Fault string `json:"fault,omitempty"` // fault name (for fault)
	Arg   string `json:"arg,omitempty"`   // op-kind name (for run_until)
}

// Schedule is a serializable, replayable adversary.
type Schedule struct {
	ID     string  `json:"schedule_id"`
	Seed   int64   `json:"seed"`
	Hidden bool    `json:"hidden"`
	Note   string  `json:"note,omitempty"`
	Events []Event `json:"events"`
}

// Debit is one entry in the provider effects log.
type Debit struct {
	ID  Identity
	Amt Money
	Ref string
}

// Observation is the independent view the oracle scores against.
type Observation struct {
	Store            map[string]Record
	Debits           []Debit
	Responses        map[string]Response
	NonConvergent    bool // a recovery pass failed to terminate within the bound
	StepsAfterFaults int
}

// DebitsByIdentity groups the effects log by identity key (deterministic).
func (o Observation) DebitsByIdentity() map[string][]Debit {
	m := map[string][]Debit{}
	for _, d := range o.Debits {
		m[d.ID.Key()] = append(m[d.ID.Key()], d)
	}
	return m
}

// SortedIdentityKeys returns identity keys in a stable order.
func (o Observation) SortedIdentityKeys() []string {
	seen := map[string]bool{}
	var out []string
	for _, d := range o.Debits {
		if !seen[d.ID.Key()] {
			seen[d.ID.Key()] = true
			out = append(out, d.ID.Key())
		}
	}
	sort.Strings(out)
	return out
}

// ---------------------------------------------------------------------------
// Kernel internals
// ---------------------------------------------------------------------------

type crashSignal struct{}

type opKind int

const (
	kStoreGet opKind = iota
	kStoreReserve
	kStoreComplete
	kStoreFail
	kStorePut
	kProviderCharge
	kProviderQuery
)

type pendingOp struct {
	kind    opKind
	key     string
	fp      string
	rec     Record
	id      Identity
	amt     Money
	applied bool     // effect already applied (drop_response), result buffered
	res     opResult // buffered result
}

type opResult struct {
	rec   Record
	ok    bool
	found bool
	ref   string
	err   error
	crash bool
}

type instance struct {
	id      string
	resume  chan opResult
	yielded chan *pendingOp
	pending *pendingOp
	env     *env
	crashed bool
	done    bool
}

// Kernel is one simulation run. Not safe for concurrent use across schedules;
// construct a fresh Kernel per schedule via Run.
type Kernel struct {
	profile   RailProfile
	store     map[string]Record
	ledger    []Debit
	clock     int64
	rng       *rand.Rand
	instances map[string]*instance
	order     []string
	responses map[string]Response

	faultsDone       bool
	steps            int
	nonConvergent    bool
	recSeq           int
	trace            []string
}

// Run executes a schedule against a task and returns the independent observation.
func Run(sch Schedule, task Task) Observation {
	k := &Kernel{
		profile:   task.Profile,
		store:     map[string]Record{},
		rng:       rand.New(rand.NewSource(sch.Seed)),
		instances: map[string]*instance{},
		responses: map[string]Response{},
	}
	for _, ev := range sch.Events {
		k.apply(ev, task)
	}
	// Drain any instances still parked (e.g. left waiting after a dropped
	// response with no subsequent crash) so their goroutines exit cleanly.
	for _, id := range k.order {
		inst := k.instances[id]
		if inst != nil && !inst.done && !inst.crashed {
			k.crash(inst)
		}
	}
	return k.observe()
}

func (k *Kernel) apply(ev Event, task Task) {
	switch ev.Op {
	case "start":
		k.trace = append(k.trace, "start "+ev.Inst+" "+ev.Req)
		k.start(ev.Inst, task.NewRequestProgram(ev.Req))
	case "step":
		k.trace = append(k.trace, "step "+ev.Inst)
		k.step(ev.Inst)
	case "run":
		k.trace = append(k.trace, "run "+ev.Inst)
		k.run(ev.Inst)
	case "run_until":
		k.trace = append(k.trace, "run_until "+ev.Inst+" "+ev.Arg)
		k.runUntil(ev.Inst, ev.Arg)
	case "fault":
		k.trace = append(k.trace, "fault "+ev.Inst+" "+ev.Fault)
		k.fault(ev.Inst, ev.Fault)
	case "crash":
		k.trace = append(k.trace, "crash "+ev.Inst)
		k.crash(k.instances[ev.Inst])
	case "faults_stop":
		k.faultsDone = true
	case "recover":
		k.faultsDone = true
		k.trace = append(k.trace, "recover")
		if task.NewRecoverProgram != nil {
			k.runToCompletion(task.NewRecoverProgram())
		}
	}
	if k.faultsDone {
		k.steps++
	}
}

func (k *Kernel) start(id string, p Program) {
	inst := &instance{
		id:      id,
		resume:  make(chan opResult),
		yielded: make(chan *pendingOp),
	}
	inst.env = &env{k: k, inst: inst}
	k.instances[id] = inst
	k.order = append(k.order, id)
	go k.runInstance(inst, p)
	next := <-inst.yielded
	if next == nil {
		if !inst.crashed {
			inst.done = true
		}
	} else {
		inst.pending = next
	}
}

func (k *Kernel) runInstance(inst *instance, p Program) {
	defer func() {
		if r := recover(); r != nil {
			if _, ok := r.(crashSignal); !ok {
				panic(r)
			}
		}
		inst.yielded <- nil
	}()
	p(inst.env)
}

func (k *Kernel) step(id string) {
	inst := k.instances[id]
	if inst == nil || inst.done || inst.crashed || inst.pending == nil {
		return
	}
	op := inst.pending
	var res opResult
	if op.applied {
		res = op.res
	} else {
		res = k.resolve(op, false, "")
	}
	k.advance(inst, res)
}

func (k *Kernel) fault(id, fault string) {
	inst := k.instances[id]
	if inst == nil || inst.done || inst.crashed || inst.pending == nil {
		return
	}
	op := inst.pending
	if fault == "drop_response" {
		// The effect commits on the rail, but the acknowledgement is lost. The
		// candidate is left parked (it never learns the outcome) until a later
		// crash or step. This is the canonical unknown-outcome hazard.
		op.res = k.resolve(op, true, "drop_response")
		op.applied = true
		return
	}
	res := k.resolve(op, true, fault)
	k.advance(inst, res)
}

func (k *Kernel) crash(inst *instance) {
	if inst == nil || inst.done || inst.crashed {
		return
	}
	inst.crashed = true
	inst.resume <- opResult{crash: true}
	<-inst.yielded
}

func (k *Kernel) advance(inst *instance, res opResult) {
	inst.resume <- res
	next := <-inst.yielded
	if next == nil {
		if !inst.crashed {
			inst.done = true
		}
		inst.pending = nil
	} else {
		inst.pending = next
	}
}

func (k *Kernel) runToCompletion(p Program) {
	k.recSeq++
	id := fmt.Sprintf("rec-%d", k.recSeq)
	k.start(id, p)
	inst := k.instances[id]
	const maxSteps = 500
	n := 0
	for inst != nil && !inst.done && !inst.crashed {
		if n > maxSteps {
			// Recovery never terminates: this is a non-convergence failure
			// (bounded-recovery invariant). Kill it so the run can finish.
			k.nonConvergent = true
			k.crash(inst)
			return
		}
		k.step(id)
		n++
	}
}

// run steps an instance with normal resolutions until it terminates. If it does
// not terminate within a generous bound, the candidate is diverging (e.g. an
// infinite retry loop): mark non-convergence and stop it.
func (k *Kernel) run(id string) {
	inst := k.instances[id]
	if inst == nil {
		return
	}
	const maxSteps = 1000
	n := 0
	for !inst.done && !inst.crashed && inst.pending != nil {
		if n > maxSteps {
			k.nonConvergent = true
			k.crash(inst)
			return
		}
		k.step(id)
		n++
	}
}

// runUntil steps an instance normally until its next pending operation is of the
// named kind (e.g. "charge"), leaving it parked *before* that operation so a
// fault can be injected at that semantic point. This keeps schedules robust to
// differences in candidate control flow.
func (k *Kernel) runUntil(id, kindName string) {
	inst := k.instances[id]
	if inst == nil {
		return
	}
	target, ok := kindFromName(kindName)
	if !ok {
		return
	}
	const maxSteps = 1000
	n := 0
	for !inst.done && !inst.crashed && inst.pending != nil {
		if inst.pending.kind == target && !inst.pending.applied {
			return
		}
		if n > maxSteps {
			return
		}
		k.step(id)
		n++
	}
}

func kindFromName(name string) (opKind, bool) {
	switch name {
	case "get":
		return kStoreGet, true
	case "reserve":
		return kStoreReserve, true
	case "complete":
		return kStoreComplete, true
	case "fail":
		return kStoreFail, true
	case "put":
		return kStorePut, true
	case "charge":
		return kProviderCharge, true
	case "query":
		return kProviderQuery, true
	}
	return 0, false
}

func (k *Kernel) resolve(op *pendingOp, faulted bool, fault string) opResult {
	switch op.kind {
	case kStoreGet:
		r, ok := k.store[op.key]
		return opResult{rec: r, ok: ok}
	case kStoreReserve:
		cur, ok := k.store[op.key]
		if ok && cur.State != "" {
			return opResult{ok: false}
		}
		k.store[op.key] = Record{State: "reserved", Fingerprint: op.fp}
		if faulted && fault == "store_ack_lost" {
			// Reservation persisted but the ack was lost.
			return opResult{ok: true, err: ErrTimeout}
		}
		return opResult{ok: true}
	case kStoreComplete:
		cur := k.store[op.key]
		cur.State = "completed"
		cur.Ref = op.rec.Ref
		k.store[op.key] = cur
		return opResult{}
	case kStoreFail:
		cur := k.store[op.key]
		cur.State = "failed"
		cur.ErrCode = op.rec.ErrCode
		k.store[op.key] = cur
		return opResult{}
	case kStorePut:
		k.store[op.key] = op.rec
		return opResult{}
	case kProviderCharge:
		return k.charge(op, faulted, fault)
	case kProviderQuery:
		ref, found := k.findDebit(op.id)
		if k.profile == WeakRail {
			// A weak rail offers no reliable query.
			return opResult{found: false}
		}
		return opResult{ref: ref, found: found}
	}
	return opResult{}
}

func (k *Kernel) charge(op *pendingOp, faulted bool, fault string) opResult {
	// Fault: crash/timeout *before* the effect commits — no debit is produced.
	if faulted && fault == "provider_timeout_no_commit" {
		return opResult{err: ErrTimeout}
	}
	// Idempotent profiles deduplicate by identity.
	if k.profile == StrongIdempotency || k.profile == Queryable {
		if ref, found := k.findDebit(op.id); found {
			if faulted && (fault == "drop_response" || fault == "provider_timeout_after_commit") {
				return opResult{ref: ref, err: ErrTimeout}
			}
			return opResult{ref: ref}
		}
	}
	ref := fmt.Sprintf("ref-%d", len(k.ledger)+1)
	k.ledger = append(k.ledger, Debit{ID: op.id, Amt: op.amt, Ref: ref})
	if faulted && (fault == "drop_response" || fault == "provider_timeout_after_commit") {
		return opResult{ref: ref, err: ErrTimeout}
	}
	return opResult{ref: ref}
}

func (k *Kernel) findDebit(id Identity) (string, bool) {
	for _, d := range k.ledger {
		if d.ID.Key() == id.Key() {
			return d.Ref, true
		}
	}
	return "", false
}

func (k *Kernel) observe() Observation {
	store := map[string]Record{}
	for key, rec := range k.store {
		store[key] = rec
	}
	debits := make([]Debit, len(k.ledger))
	copy(debits, k.ledger)
	resp := map[string]Response{}
	for id, r := range k.responses {
		resp[id] = r
	}
	return Observation{
		Store:            store,
		Debits:           debits,
		Responses:        resp,
		NonConvergent:    k.nonConvergent,
		StepsAfterFaults: k.steps,
	}
}

// Trace returns the ordered list of scheduler transitions actually executed.
// Two runs with identical (candidate, schedule, seed) must produce identical
// traces and observations — this is the determinism contract.
func (k *Kernel) Trace() []string { return k.trace }

// RunWithTrace is like Run but also returns the executed trace, for the
// determinism tests and the trace shrinker.
func RunWithTrace(sch Schedule, task Task) (Observation, []string) {
	k := &Kernel{
		profile:   task.Profile,
		store:     map[string]Record{},
		rng:       rand.New(rand.NewSource(sch.Seed)),
		instances: map[string]*instance{},
		responses: map[string]Response{},
	}
	for _, ev := range sch.Events {
		k.apply(ev, task)
	}
	for _, id := range k.order {
		inst := k.instances[id]
		if inst != nil && !inst.done && !inst.crashed {
			k.crash(inst)
		}
	}
	return k.observe(), k.trace
}

// ---------------------------------------------------------------------------
// env: per-instance handles that route every call through the kernel.
// ---------------------------------------------------------------------------

type env struct {
	k    *Kernel
	inst *instance
}

func (e *env) Store() Store       { return storeH{e} }
func (e *env) Provider() Provider { return provH{e} }
func (e *env) Clock() Clock       { return clockH{e.k} }
func (e *env) Rand() Rand         { return randH{e.k} }
func (e *env) SetResponse(r Response) {
	e.k.responses[e.inst.id] = r
}

func (k *Kernel) op(inst *instance, op *pendingOp) opResult {
	inst.pending = op
	inst.yielded <- op
	r := <-inst.resume
	if r.crash {
		panic(crashSignal{})
	}
	inst.pending = nil
	return r
}

type storeH struct{ e *env }

func (s storeH) Get(key string) (Record, bool) {
	r := s.e.k.op(s.e.inst, &pendingOp{kind: kStoreGet, key: key})
	return r.rec, r.ok
}
func (s storeH) Reserve(key, fp string) bool {
	r := s.e.k.op(s.e.inst, &pendingOp{kind: kStoreReserve, key: key, fp: fp})
	return r.ok
}
func (s storeH) Complete(key, ref string) {
	s.e.k.op(s.e.inst, &pendingOp{kind: kStoreComplete, key: key, rec: Record{Ref: ref}})
}
func (s storeH) Fail(key, errCode string) {
	s.e.k.op(s.e.inst, &pendingOp{kind: kStoreFail, key: key, rec: Record{ErrCode: errCode}})
}
func (s storeH) Put(key string, rec Record) {
	s.e.k.op(s.e.inst, &pendingOp{kind: kStorePut, key: key, rec: rec})
}

type provH struct{ e *env }

func (p provH) Charge(id Identity, amt Money) (string, error) {
	r := p.e.k.op(p.e.inst, &pendingOp{kind: kProviderCharge, id: id, amt: amt})
	return r.ref, r.err
}
func (p provH) Query(id Identity) (string, bool, error) {
	r := p.e.k.op(p.e.inst, &pendingOp{kind: kProviderQuery, id: id})
	return r.ref, r.found, r.err
}

type clockH struct{ k *Kernel }

func (c clockH) Now() int64 { c.k.clock++; return c.k.clock }

type randH struct{ k *Kernel }

func (r randH) Intn(n int) int {
	if n <= 0 {
		return 0
	}
	return r.k.rng.Intn(n)
}
