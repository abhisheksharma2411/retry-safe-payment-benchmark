"""Build the sealed scaffold a CLI coding agent iterates inside.

A CLI agent (`claude -p`, and any future `codex exec`) differs from a
single-shot API call in one way that matters here: it has a filesystem and a
shell. Handing it the repository would hand it `HiddenCases()`, `Correct`, the
five mutants, and `harness.CheckFinancial` — that is, the answer key and the
scoring set. The hidden-set commitment in ARTIFACT_EVALUATION.md would be void,
and the resulting numbers would measure nothing.

So the agent never sees the repository. It gets a freshly built scaffold
containing exactly three things:

  (a) the family's `Service` interface — package doc, `Request`, `Service`,
      `Factory`, sliced verbatim out of the family source;
  (b) the injected-environment API — `Env`, `Store`, `Provider`, `Clock`,
      `Rand` and their value types, sliced verbatim out of `harness/harness.go`
      as a types-only package (`API.md` restates it in prose);
  (c) `public_test.go`, wired to that family's PUBLIC schedules only.

and nothing else. What is deliberately absent:

  * hidden schedules — `cases.go` is never copied; the scorer resolves
    `PublicCases()` in a private workspace the scaffold has no path to;
  * oracle internals — `harness/oracle.go` is never copied. The scaffold's
    harness package is types only: no `CheckFinancial`, no `Spec`, not even the
    kernel's `Run`;
  * the correct reference and the mutants — the slice in (a) stops at the
    `Factory` declaration, above `Correct` and `M1`..`M5`.

`go test` still returns real oracle verdicts, because `public_test.go` shells
out to `bin/t4public`: a per-scaffold binary with the workspace path linked in
at build time (`-X main.workspaceDir=...`), so the scaffold source names no
path into the benchmark. The binary compiles the candidate against the full
module and runs `PublicCases()` — the same oracle the pilot uses, never a
second one — and refuses to resolve anything else.

Isolation is structural first and audited second. `AgentScaffold.integrity()`
hashes every file the agent is not supposed to touch, so a candidate that
"passes" by rewriting its own test is caught rather than believed.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile

from .evaluate import BUILD_TIMEOUT, Workspace
from .families import FACTORY_NAME, HELPER_PREFIX, RAIL_PROFILE, Family

HARNESS_SRC = "harness.go"

# The injected-environment surface, in declaration order. Everything else in
# harness.go — the kernel, Schedule/Event, Observation, the fault machinery — is
# how the benchmark *runs* a candidate, which is none of the candidate's
# business and is withheld along with the oracle.
ENV_API = (
    "ErrTimeout",
    "ErrPartition",
    "Money",
    "Identity",
    "Identity.Key",
    "Response",
    "Record",
    "Store",
    "Provider",
    "Clock",
    "Rand",
    "Env",
)

# Files the agent may not modify. `candidate.go` is deliberately not in the set.
PROTECTED = ("go.mod", "API.md", "README.md")


def _decl_name(head: str) -> str:
    """The lookup name for a top-level Go declaration line."""
    method = re.match(r"func\s*\(\s*\w+\s+\*?(\w+)\s*\)\s*(\w+)", head)
    if method:
        return f"{method.group(1)}.{method.group(2)}"
    simple = re.match(r"(?:var|type|func)\s+(\w+)", head)
    return simple.group(1) if simple else ""


def _top_level_decls(src: str) -> dict:
    """name -> verbatim source (doc comment included) for every top-level decl.

    A hand-rolled scan rather than a Go parser: the inputs are two files in this
    repository whose shape `check_families()` already pins, and shelling out to
    `go/ast` for them would buy nothing.
    """
    lines = src.split("\n")
    out, i = {}, 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith(("var ", "type ", "func ")):
            i += 1
            continue
        start = i
        while start > 0 and lines[start - 1].startswith("//"):
            start -= 1
        end = i
        if line.rstrip().endswith("{"):
            while end < len(lines) and lines[end] != "}":
                end += 1
        name = _decl_name(line)
        if name:
            out[name] = "\n".join(lines[start : end + 1])
        i = end + 1
    return out


def _env_api_source(root: str) -> str:
    """The types-only harness package the scaffold compiles against."""
    with open(os.path.join(root, "harness", HARNESS_SRC), encoding="utf-8") as fh:
        decls = _top_level_decls(fh.read())
    missing = [name for name in ENV_API if name not in decls]
    if missing:
        raise RuntimeError(
            f"harness/{HARNESS_SRC} no longer declares {', '.join(missing)}; "
            "the agent scaffold cannot be built without the injected-environment API."
        )
    body = "\n\n".join(decls[name] for name in ENV_API)
    return (
        "// Package harness is the environment injected into a candidate.\n"
        "//\n"
        "// This is the *interface* half of the benchmark harness, sliced verbatim from\n"
        "// the harness the scorer runs. The kernel, the fault schedules, and the oracle\n"
        "// are not here and are not yours to see: you are being scored on held-out\n"
        "// schedules, so writing to the test is not a strategy that survives.\n"
        "//\n"
        "// Candidate code never touches the real clock, real randomness, real networks,\n"
        "// real threads, or a real payment rail. Every externally observable action goes\n"
        "// through the interfaces below.\n"
        "package harness\n\n"
        'import "errors"\n\n' + body + "\n"
    )


def _service_source(fam: Family) -> str:
    """The family's public interface: package doc, Request, Service, Factory.

    Sliced from the family source, which continues on into the correct reference
    and the five mutants — so the slice is taken by declaration name, never by
    line offset, and a missing declaration is a hard error rather than a quietly
    truncated file.
    """
    with open(os.path.join(fam.dir, f"{fam.name}.go"), encoding="utf-8") as fh:
        decls = _top_level_decls(fh.read())
    wanted = ("Request", "Service", "Factory")
    missing = [name for name in wanted if name not in decls]
    if missing:
        raise RuntimeError(
            f"tasks/{fam.name}/{fam.name}.go no longer declares {', '.join(missing)}"
        )
    body = "\n\n".join(decls[name] for name in wanted)
    return (
        f"// Package {fam.name} is the task family under evaluation.\n"
        "//\n"
        f"// {_wrap_comment(fam.task_description)}\n"
        "//\n"
        f"// Rail profile: {RAIL_PROFILE} — the provider can be asked whether an effect\n"
        "// already exists for an identity, so an unknown outcome can be reconciled\n"
        "// rather than guessed at.\n"
        f"package {fam.name}\n\n"
        'import "t4bench/harness"\n\n' + body + "\n"
    )


def _wrap(text: str, width: int = 74) -> list:
    words, lines, cur = text.split(), [], ""
    for word in words:
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines


def _wrap_comment(text: str, width: int = 74) -> str:
    return "\n// ".join(_wrap(text, width))


def _wrap_plain(text: str, width: int = 76) -> str:
    return "\n".join(_wrap(text, width))


_CANDIDATE_STUB = """package {family}

import "t4bench/harness"

// {factory} is the entry point the scorer binds. Replace the body below with a
// retry-safe implementation; keep the signature exactly as it is.
//
// Every unexported top-level identifier you declare in this file MUST begin
// with `{prefix}` — see README.md. The scorer compiles this file alongside other
// files in the package, and an unprefixed helper collides with them.
func {factory}(env harness.Env) Service {{
	return &{prefix}Service{{env: env}}
}}

type {prefix}Service struct{{ env harness.Env }}

func (s *{prefix}Service) {method_name}(req Request) harness.Response {{
	// TODO: implement. This stub compiles and fails; that is the starting point.
	return harness.Response{{Status: "ERROR", Err: "not implemented"}}
}}
"""

_PUBLIC_TEST = '''package {family}

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestPublicSchedules scores candidate.go against this family's PUBLIC fault
// schedules, through the benchmark's own oracle.
//
// The schedules, the oracle, and the reference implementation are not in this
// directory and cannot be reached from it. The scorer binary holds the only
// path to them, resolves the PUBLIC set and nothing else, and reports one
// verdict per schedule.
//
// Passing every public schedule is necessary, not sufficient: scoring happens
// on held-out schedules you cannot run. Code for the contract in README.md, not
// for this test.
func TestPublicSchedules(t *testing.T) {{
	scorer := os.Getenv("T4_PUBLIC_SCORER")
	if scorer == "" {{
		scorer = filepath.Join("..", "..", "bin", "t4public")
	}}
	abs, err := filepath.Abs(scorer)
	if err != nil {{
		t.Fatalf("resolving the scorer path: %v", err)
	}}
	if _, err := os.Stat(abs); err != nil {{
		t.Fatalf("scorer binary not found at %s: %v", abs, err)
	}}

	cmd := exec.Command(abs, "-candidate", "candidate.go")
	out, err := cmd.CombinedOutput()
	report := strings.TrimRight(string(out), "\\n")
	if report != "" {{
		t.Log("\\n" + report)
	}}
	if err != nil {{
		t.Fatalf("public schedules did not pass (%v)", err)
	}}
}}
'''

# The per-scaffold scorer. Built into the private workspace and copied into the
# scaffold as an opaque binary: the workspace path is linked in with -ldflags, so
# no file the agent can read names a route back to the benchmark sources.
_SCORER = '''// Command t4public scores one scaffold candidate against the PUBLIC fault
// schedules of a single family. Built per scaffold with the workspace path and
// family linked in, so the scaffold contains no path into the benchmark tree.
//
// It resolves PublicCases() and nothing else. There is no flag, environment
// variable, or argument that makes it run a hidden schedule.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// Linked in at build time by generation/runner/scaffold.py.
var (
	workspaceDir string
	familyName   string
	semanticID   string
)

var factoryRe = regexp.MustCompile(`(?m)^func\\s+([A-Za-z_]\\w*)\\s*\\(\\s*\\w+\\s+harness\\.Env\\s*\\)\\s*Service\\b`)

const driverTemplate = `// Code generated by t4public. DO NOT EDIT.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"t4bench/harness"
	"t4bench/tasks/__FAMILY__"
)

func main() {
	out := []harness.Result{}
	for _, c := range __FAMILY__.PublicCases() {
		out = append(out, score(c))
	}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, "encode:", err)
		os.Exit(1)
	}
}

func score(c __FAMILY__.Case) (res harness.Result) {
	defer func() {
		if r := recover(); r != nil {
			fmt.Fprintf(os.Stderr, "panic on %s: %v\\n", c.Sch.ID, r)
			res = harness.Result{
				Family: __FAMILY__.Family, SemanticTask: "__SEMANTIC__",
				Candidate: "agent-scaffold", CorrectRef: false,
				ScheduleID: c.Sch.ID, Seed: c.Sch.Seed, Hidden: c.Sch.Hidden,
				CompileStatus: "success", RuntimeStatus: "crashed",
				Invariants: map[string]bool{}, OK: false,
			}
		}
	}()
	obs := harness.Run(c.Sch, __FAMILY__.TaskFor(__FAMILY__.__FACTORY__))
	verdict := harness.CheckFinancial(obs, c.Exp)
	runtime := "completed"
	if obs.NonConvergent {
		runtime = "nonconvergent"
	}
	return harness.NewResult(__FAMILY__.Family, "__SEMANTIC__", "agent-scaffold", false, c.Sch, runtime, verdict)
}
`

type result struct {
	ScheduleID    string   `json:"schedule_id"`
	RuntimeStatus string   `json:"runtime_status"`
	Violations    []string `json:"violations"`
	OK            bool     `json:"ok"`
}

func main() {
	candidate := flag.String("candidate", "candidate.go", "path to the candidate Go file")
	flag.Parse()

	src, err := os.ReadFile(*candidate)
	if err != nil {
		fmt.Fprintf(os.Stderr, "cannot read %s: %v\\n", *candidate, err)
		os.Exit(2)
	}
	match := factoryRe.FindStringSubmatch(string(src))
	if match == nil {
		fmt.Fprintf(os.Stderr,
			"no factory matching `func Name(env harness.Env) Service` found in %s\\n", *candidate)
		os.Exit(2)
	}

	release, err := lock(filepath.Join(workspaceDir, ".t4public.lock"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	defer release()

	candPath := filepath.Join(workspaceDir, "tasks", familyName, "candidate_llm.go")
	driverDir := filepath.Join(workspaceDir, "cmd", "t4eval")
	if err := os.MkdirAll(driverDir, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	driver := strings.NewReplacer(
		"__FAMILY__", familyName,
		"__SEMANTIC__", semanticID,
		"__FACTORY__", match[1],
	).Replace(driverTemplate)

	if err := os.WriteFile(candPath, src, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	defer os.Remove(candPath)
	if err := os.WriteFile(filepath.Join(driverDir, "main.go"), []byte(driver), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	defer os.Remove(filepath.Join(driverDir, "main.go"))

	build := exec.Command("go", "build", "-o", "t4eval", "./cmd/t4eval")
	build.Dir = workspaceDir
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := build.CombinedOutput(); err != nil {
		fmt.Println("COMPILE ERROR\\n")
		fmt.Println(scrub(string(out)))
		os.Exit(1)
	}

	run := exec.Command(filepath.Join(workspaceDir, "t4eval"))
	run.Dir = workspaceDir
	out, err := run.Output()
	if err != nil && len(out) == 0 {
		fmt.Printf("the candidate did not produce verdicts: %v\\n", err)
		os.Exit(1)
	}
	var results []result
	if err := json.Unmarshal(out, &results); err != nil {
		fmt.Printf("unparseable scorer output: %v\\n", err)
		os.Exit(1)
	}

	failed := 0
	fmt.Printf("PUBLIC schedules for %s (%d):\\n", familyName, len(results))
	for _, r := range results {
		status := "pass"
		if !r.OK {
			status = "FAIL"
			failed++
		}
		line := fmt.Sprintf("  %-4s %-38s runtime=%s", status, r.ScheduleID, r.RuntimeStatus)
		if len(r.Violations) > 0 {
			line += "  violated: " + strings.Join(r.Violations, ", ")
		}
		fmt.Println(line)
	}
	fmt.Printf("\\n%d/%d public schedules passed.\\n", len(results)-failed, len(results))
	if failed > 0 {
		fmt.Println("Hidden schedules were not run and will not be shown.")
		os.Exit(1)
	}
}

// scrub keeps workspace paths out of compiler output, so a build error cannot
// hand the agent a route to the sources the scaffold withholds.
func scrub(s string) string {
	s = strings.ReplaceAll(s, workspaceDir+string(os.PathSeparator), "")
	s = strings.ReplaceAll(s, workspaceDir, "")
	s = strings.ReplaceAll(s, "candidate_llm.go", "candidate.go")
	var keep []string
	for _, line := range strings.Split(s, "\\n") {
		if strings.Contains(line, "cmd/t4eval") {
			continue
		}
		keep = append(keep, line)
	}
	return strings.Join(keep, "\\n")
}

func lock(path string) (func(), error) {
	deadline := time.Now().Add(120 * time.Second)
	for {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
		if err == nil {
			f.Close()
			return func() { os.Remove(path) }, nil
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("timed out waiting for the scorer lock at %s", path)
		}
		time.Sleep(200 * time.Millisecond)
	}
}
'''


class AgentScaffold:
    """One family's sealed working directory, plus the private workspace that
    scores it. The two are separate temp trees: the scaffold is the agent's
    working directory, and the workspace is not reachable by walking up from it.
    """

    def __init__(self, fam: Family, scratch_root=None):
        from .families import ROOT

        self.fam = fam
        self.dir = tempfile.mkdtemp(prefix=f"t4scaffold-{fam.name}-", dir=scratch_root)
        self.workspace = Workspace(scratch_root=scratch_root)
        # The agent's working directory is its permission boundary, so a scaffold
        # created inside the checkout would silently place the whole repository —
        # hidden schedules, oracle, reference, mutants — inside the boundary.
        # Asserted rather than assumed, because `scratch_root` is a parameter and
        # a future caller could point it anywhere.
        for label, path in (("scaffold", self.dir), ("workspace", self.workspace.root)):
            real, repo = os.path.realpath(path), os.path.realpath(ROOT)
            if real == repo or real.startswith(repo + os.sep):
                raise RuntimeError(
                    f"the agent {label} was created inside the repository checkout "
                    f"({real}); that would put the hidden schedules, the oracle and "
                    "the correct reference inside the agent's permission boundary"
                )
        self.pkg_dir = os.path.join(self.dir, "tasks", fam.name)
        self.candidate_path = os.path.join(self.pkg_dir, "candidate.go")
        self._baseline = {}

    # -- construction ------------------------------------------------------

    def build(self):
        from .families import ROOT

        os.makedirs(os.path.join(self.dir, "harness"), exist_ok=True)
        os.makedirs(self.pkg_dir, exist_ok=True)
        os.makedirs(os.path.join(self.dir, "bin"), exist_ok=True)

        self._write("go.mod", "module t4bench\n\ngo 1.24\n")
        self._write(os.path.join("harness", "api.go"), _env_api_source(ROOT))
        self._write(
            os.path.join("tasks", self.fam.name, f"{self.fam.name}.go"),
            _service_source(self.fam),
        )
        self._write(
            os.path.join("tasks", self.fam.name, "candidate.go"),
            _CANDIDATE_STUB.format(
                family=self.fam.name,
                factory=FACTORY_NAME,
                prefix=HELPER_PREFIX,
                method_name=self.fam.method_name,
            ),
        )
        self._write(
            os.path.join("tasks", self.fam.name, "public_test.go"),
            _PUBLIC_TEST.format(family=self.fam.name),
        )
        self._write("API.md", self._api_doc())
        self._write("README.md", self._readme())
        self._build_scorer()
        self._verify()
        self._baseline = self._hashes()

    def _write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _build_scorer(self):
        src_dir = os.path.join(self.workspace.module, "cmd", "t4public")
        os.makedirs(src_dir, exist_ok=True)
        with open(os.path.join(src_dir, "main.go"), "w", encoding="utf-8") as fh:
            fh.write(_SCORER)
        out = os.path.join(self.dir, "bin", "t4public")
        ldflags = " ".join(
            [
                f"-X main.workspaceDir={self.workspace.module}",
                f"-X main.familyName={self.fam.name}",
                f"-X main.semanticID={self.fam.semantic_task_id}",
            ]
        )
        build = subprocess.run(
            ["go", "build", "-ldflags", ldflags, "-o", out, "./cmd/t4public"],
            cwd=self.workspace.module,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            env={**os.environ, "CGO_ENABLED": "0"},
        )
        if build.returncode != 0:
            raise RuntimeError(f"failed to build the public scorer:\n{build.stderr}")
        os.chmod(out, 0o755)

    def _verify(self):
        """Fail loudly if the scaffold does not build, or if it leaks.

        A scaffold that silently shipped the oracle or the hidden schedules would
        invalidate every number produced from it, so both are asserted here
        rather than assumed.
        """
        build = subprocess.run(
            ["go", "build", "./..."],
            cwd=self.dir,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            env={**os.environ, "CGO_ENABLED": "0"},
        )
        if build.returncode != 0:
            raise RuntimeError(f"the generated scaffold does not compile:\n{build.stderr}")
        leaks = self.audit_contents()
        if leaks:
            raise RuntimeError(
                "the generated scaffold leaks withheld material: " + "; ".join(leaks)
            )

    # -- audit -------------------------------------------------------------

    def hidden_ids(self) -> list:
        """This family's hidden schedule ids — the exact strings that must never
        appear in the scaffold, and must never appear in the agent's transcript."""
        return [
            s["schedule_id"]
            for s in self.workspace.schedules()[self.fam.name]
            if s["hidden"]
        ]

    def audit_contents(self) -> list:
        """Withheld material found in readable scaffold files, if any.

        Checked against the real hidden schedule ids rather than a keyword, so
        this catches an actual leak instead of an unlucky choice of words.
        """
        banned = {
            "HiddenCases": "the hidden-schedule accessor",
            "CheckFinancial": "the oracle entry point",
            "OracleResult": "oracle internals",
            "harness.Spec": "oracle expectations",
            "func Correct(": "the correct reference",
            "func M1(": "a seeded-bug mutant",
        }
        banned.update({sid: "a hidden schedule id" for sid in self.hidden_ids()})
        bindir = os.path.join(self.dir, "bin")
        found = []
        for path in self.files():
            if path.startswith(bindir + os.sep):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            rel = os.path.relpath(path, self.dir)
            for needle, why in banned.items():
                if needle in text:
                    found.append(f"{rel} contains {needle!r} ({why})")
        return found

    def files(self) -> list:
        out = []
        for dirpath, _dirs, names in os.walk(self.dir):
            for name in names:
                out.append(os.path.join(dirpath, name))
        return sorted(out)

    def _hashes(self) -> dict:
        out = {}
        for rel in self._protected_rel():
            path = os.path.join(self.dir, rel)
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
        return out

    def _protected_rel(self) -> list:
        return list(PROTECTED) + [
            os.path.join("harness", "api.go"),
            os.path.join("tasks", self.fam.name, f"{self.fam.name}.go"),
            os.path.join("tasks", self.fam.name, "public_test.go"),
        ]

    def integrity(self) -> list:
        """Protected files the agent modified or deleted.

        A non-empty list does not invalidate the score — scoring happens outside
        the scaffold against the untouched originals — but it does mean the
        agent's own `go test` was measuring something other than the contract,
        so it is recorded with the result.
        """
        changed = []
        for rel, digest in self._baseline.items():
            path = os.path.join(self.dir, rel)
            if not os.path.exists(path):
                changed.append(f"{rel} (deleted)")
                continue
            with open(path, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() != digest:
                    changed.append(f"{rel} (modified)")
        return changed

    def inventory(self) -> dict:
        """What the agent was handed, in files, bytes, and estimated tokens.

        `est_tokens` is bytes/4 — a standard rough ratio, not a tokenizer count.
        It is reported so a scaffold that quietly grows is visible in the record;
        the authoritative context figure is the CLI's own reported usage.
        """
        readable = [p for p in self.files() if not p.startswith(os.path.join(self.dir, "bin"))]
        total = 0
        for path in readable:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return {
            "files": len(readable),
            "bytes": total,
            "est_tokens": round(total / 4),
            "note": "est_tokens is bytes/4, an approximation; bin/ is excluded (opaque binary)",
        }

    def read_candidate(self) -> str:
        if not os.path.exists(self.candidate_path):
            return ""
        with open(self.candidate_path, encoding="utf-8") as fh:
            return fh.read()

    def cleanup(self):
        self.workspace.cleanup()
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- docs --------------------------------------------------------------

    def _api_doc(self) -> str:
        from .families import ROOT

        return (
            "# The injected environment\n\n"
            "Your service is constructed with a `harness.Env` and may use nothing else\n"
            "to reach the outside world. There is no real clock, no real randomness, no\n"
            "network, and no goroutine scheduling: every call below is driven by a\n"
            "deterministic kernel one operation at a time.\n\n"
            "This is the interface half of the benchmark harness, reproduced verbatim.\n"
            "It is also on disk at `harness/api.go`, which is what your code compiles\n"
            "against.\n\n"
            "```go\n" + _env_api_source(ROOT) + "```\n\n"
            "## What each piece is for\n\n"
            "- **`Store`** is durable and survives a crash. Anything you keep in Go\n"
            "  memory does not: a new process gets a fresh `Service` value.\n"
            "- **`Store.Reserve(key, fingerprint)`** is the only atomic primitive. It\n"
            "  creates a `reserved` record if and only if none exists, and returns true\n"
            "  to exactly one caller. It is how an identity is claimed before an\n"
            "  external effect is produced.\n"
            "- **`Provider.Charge`** produces the external, money-moving effect.\n"
            "- **`Provider.Query`** reports whether an effect already exists for an\n"
            "  identity. This is what makes an unknown outcome recoverable without\n"
            "  guessing.\n"
            "- **`ErrTimeout`** means the outcome is genuinely unknown: the effect may\n"
            "  or may not have happened. Assuming either way is a bug.\n"
            "- **`Identity.Key()`** is the full identity — merchant, operation,\n"
            "  resource, caller key. Two merchants may legitimately send the same\n"
            "  `CallerKey`.\n"
        )

    def _readme(self) -> str:
        fam = self.fam
        return f"""# Task: retry-safe `{fam.name}`

Implement `{FACTORY_NAME}` in `tasks/{fam.name}/candidate.go` so that the service is
correct under retries, crashes, and unknown provider outcomes.

## The contract

{_wrap_plain(fam.task_description)}

Rail profile: **{RAIL_PROFILE}** — `Provider.Query` can tell you whether an effect
already exists for an identity, so an unknown outcome can be reconciled instead
of guessed at.

## What you must satisfy

Six invariants are checked. All six must hold on every schedule:

| Invariant | Meaning |
|---|---|
| `at_most_one` | Never more than one external effect per operation identity. |
| `no_lost_effect` | An accepted operation always ends with its effect produced. |
| `no_false_dedup` | Distinct identities are never collapsed into one effect. |
| `payload_consistency` | Reusing an identity with a different payload is a `CONFLICT`, and produces no additional effect. |
| `reproducible` | Same schedule, same seed, same behaviour. |
| `recovery` | A recovery pass after a crash reconciles rather than repeating or abandoning. |

`no_lost_effect` and `at_most_one` pull against each other; that tension is the
task. Refusing to act is not safety — a payment that never completes is a lost
payment, and it is scored as a failure.

## Working here

```sh
go build ./...   # fast syntax/type check
go test ./...    # the PUBLIC fault schedules, through the real oracle
```

`go test` reports one verdict per public schedule, naming the invariants any
failing schedule violated.

## Rules

1. **Edit `tasks/{fam.name}/candidate.go` only.** `{fam.name}.go`, `harness/api.go`,
   and `public_test.go` are fixed. They are hashed before and after this session,
   and edits to them are recorded with your result.
2. **Keep the signature**: `func {FACTORY_NAME}(env harness.Env) Service`.
3. **Prefix every unexported top-level identifier you declare with
   `{HELPER_PREFIX}`** — `{HELPER_PREFIX}Fingerprint`, `{HELPER_PREFIX}State`, and so on.
   Scoring compiles your file alongside other files in this package, and an
   unprefixed helper collides with them and fails the build.
4. **Import only `t4bench/harness` and the Go standard library.**
5. **Do not add or modify test files.**

## How this is scored

Passing every public schedule is necessary and not sufficient. Your final
`candidate.go` is scored on a **held-out set of fault schedules you cannot run
and will not see**, using the same oracle. The public schedules here are for
development; they are not the exam.

Write for the contract above, not for the four schedules you can see.
"""
