"""CLI coding agents as an *agentic-condition* backend.

`claude -p` is not a base model and must never be scored as one. It carries its
own system prompt, its own tools, and its own multi-turn loop; a number produced
from it is a measurement of that whole harness, not of the underlying model. Put
one in the `zero_shot` column and the comparison silently stops meaning
anything — the CLI would be credited with a prompt-condition effect that is
really a scaffolding effect.

So this backend is locked to `agentic`. `refuse_base_condition()` is the guard,
and it is called from argument parsing *and* again at the point of use, because
a guard that lives only in the CLI layer is one refactor away from being gone.

What the runner controls, and what it does not:

  * **Controlled** — the working directory (a sealed scaffold; see
    `scaffold.py`), the wall-clock budget, the spend cap, the model, and
    network tool access (`WebSearch`/`WebFetch` are denied: this benchmark's
    repository is public, so an agent that can search can read the hidden
    schedules out of GitHub).
  * **Recorded, not controlled** — turn count. The installed CLI exposes no
    `--max-turns`, so iteration is bounded by time and spend, and `num_turns`
    is reported per run rather than capped.
  * **Audited afterwards** — every session transcript is scanned for the
    withheld material, and the scaffold's protected files are re-hashed. Both
    land in the record, so contamination is visible in the data rather than
    assumed absent.
"""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from .model import ModelError

# Backends that are coding agents rather than base models. Locked to `agentic`.
AGENT_PROVIDERS = ("claude-cli", "codex-cli")
AGENT_CONDITION = "agentic"

# Declared so `--provider codex-cli` fails with an explanation rather than an
# argparse "invalid choice", but deliberately not implemented: `codex` was not
# installed when this was written, and shipping an untested backend that claims
# to work is worse than a backend that says it does not.
UNIMPLEMENTED = {
    "codex-cli": (
        "the `codex` CLI was not on PATH when this backend was written, so no "
        "codex-cli adapter was implemented rather than shipping one that had "
        "never been run. Implement it as a CLIAgent subclass in "
        "generation/runner/agent_cli.py (see ClaudeCLIAgent) and register it in "
        "AGENTS; the scaffold, guard, audit, and record path are provider-agnostic "
        "and need no changes."
    )
}


class AgentContaminated(ModelError):
    """The agent reached material the scaffold was supposed to withhold."""


# ---------------------------------------------------------------------------
# Config isolation
# ---------------------------------------------------------------------------
#
# `claude -p` loads more than the prompt: user settings, a global CLAUDE.md, and
# cwd-scoped memory. This benchmark was built in Claude Code on this machine, so
# that inherited context is a live contamination channel — an agent that has read
# notes about reserve-then-effect or the six invariants is recalling, not
# solving.
#
# Full relocation via CLAUDE_CONFIG_DIR was tested and **breaks subscription
# auth** ("Not logged in"), and `--bare` requires an API key. So isolation is
# achieved three ways instead, and the third is what makes it safe rather than
# lucky:
#
#   1. `--setting-sources ''` — user/project/local settings are not loaded at
#      all. This matters concretely: the operator's settings on this machine
#      allow `Bash(python3 -c ' *)`, `Bash(curl ...)` and
#      `WebFetch(domain:github.com)`, any of which would defeat the Go-only
#      allowlist and reach a public copy of this repository.
#   2. The scaffold cwd is a fresh mkdtemp path, and Claude Code scopes memory by
#      cwd — so the session's memory directory is empty *by construction*, not by
#      configuration.
#   3. Everything still reachable is inspected, hashed, and scanned before the
#      run, and a benchmark-relevant hit **refuses the run**.

# Terms that would indicate the inherited context knows about this benchmark.
# Deliberately broad: a false positive stops a run and asks a human to look,
# which is the cheap direction to be wrong in.
BENCHMARK_TERMS = (
    "t4bench",
    "t4-benchmark",
    "retry-saf",
    "idempoten",
    "reserve-then-effect",
    "reserve_then_effect",
    "at_most_one",
    "no_lost_effect",
    "no_false_dedup",
    "payload_consistency",
    "hidden schedule",
    "fault schedule",
    "fault injection",
    "seeded-bug",
    "mutant",
    "payment rail",
    "capture-t1",
)


def _digest(path: str) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read()
    return {"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _scan_terms(text: str) -> list:
    low = text.lower()
    return [t for t in BENCHMARK_TERMS if t in low]


def _memory_dirs(scaffold) -> list:
    """Claude Code's cwd-scoped memory directories for this scaffold.

    Matched by the scaffold's unique basename rather than by reimplementing the
    path-munging Claude Code uses, which would be one upstream change away from
    silently matching nothing and reporting a clean bill of health.
    """
    home = os.path.expanduser("~/.claude/projects")
    leaf = os.path.basename(scaffold.dir)
    return sorted(glob.glob(os.path.join(home, f"*{leaf}", "memory")))


def inspect_inherited_config(scaffold) -> dict:
    """Everything `claude -p` could inject beyond the scaffold, hashed and scanned.

    Recorded into every agentic result so the paper can disclose exactly what the
    agent's context contained beyond the task.
    """
    state = {
        "setting_sources": "",
        "user_settings_loaded": False,
        "config_dir_relocated": False,
        "config_dir_note": (
            "CLAUDE_CONFIG_DIR relocation was tested and breaks subscription auth "
            "('Not logged in'); --bare requires an API key. Isolation is achieved "
            "with --setting-sources '' plus a fresh-temp-cwd (memory is cwd-scoped) "
            "and enforced by the scan below."
        ),
        "inherited": [],
        "benchmark_relevant_hits": [],
    }

    # Disclosed but NOT loaded, so a reviewer can see what was excluded.
    user_settings = os.path.expanduser("~/.claude/settings.json")
    if os.path.exists(user_settings):
        info = _digest(user_settings)
        info["loaded"] = False
        info["kind"] = "user settings (excluded by --setting-sources '')"
        state["inherited"].append(info)

    # A global CLAUDE.md *would* still load: --setting-sources does not govern
    # it, and the config dir cannot be moved without losing auth. So it is
    # hashed and scanned, and a benchmark-relevant hit aborts the run.
    candidates = [os.path.expanduser("~/.claude/CLAUDE.md")]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates.append(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "CLAUDE.md"))
    # Any CLAUDE.md in the scaffold or an ancestor of it.
    node = os.path.realpath(scaffold.dir)
    while True:
        candidates.append(os.path.join(node, "CLAUDE.md"))
        parent = os.path.dirname(node)
        if parent == node:
            break
        node = parent

    for path in candidates:
        if not os.path.exists(path):
            continue
        info = _digest(path)
        info["loaded"] = True
        info["kind"] = "CLAUDE.md"
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                hits = _scan_terms(fh.read())
        except OSError:
            hits = []
        info["benchmark_terms"] = hits
        state["inherited"].append(info)
        state["benchmark_relevant_hits"].extend(f"{path}: {t}" for t in hits)

    mem_files = []
    for mdir in _memory_dirs(scaffold):
        for name in sorted(os.listdir(mdir)) if os.path.isdir(mdir) else []:
            path = os.path.join(mdir, name)
            if not os.path.isfile(path):
                continue
            info = _digest(path)
            info["loaded"] = True
            info["kind"] = "session memory"
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    hits = _scan_terms(fh.read())
            except OSError:
                hits = []
            info["benchmark_terms"] = hits
            mem_files.append(info)
            state["benchmark_relevant_hits"].extend(f"{path}: {t}" for t in hits)
    state["inherited"].extend(mem_files)
    state["session_memory_files"] = len(mem_files)
    state["clean"] = not state["benchmark_relevant_hits"]
    return state


def enforce_config_isolation(state: dict) -> None:
    """Refuse to run if the inherited context knows anything about this benchmark.

    Contamination here is unrecoverable after the fact: once the agent has read
    a note about reserve-then-effect, no audit can subtract it from the result.
    The only safe move is not to run.
    """
    if state["benchmark_relevant_hits"]:
        raise AgentContaminated(
            "the CLI's inherited context mentions this benchmark, so an agentic "
            "run from it would measure recall rather than reasoning:\n  - "
            + "\n  - ".join(state["benchmark_relevant_hits"])
            + "\nRemove or relocate the offending file before running the agentic "
            "condition."
        )


def refuse_base_condition(provider: str, conditions) -> None:
    """Hard guard: a CLI agent may only ever run the agentic condition.

    Raises rather than filtering. Quietly dropping the base conditions would
    produce a run that looks like a four-condition sweep and is not one.
    """
    if provider not in AGENT_PROVIDERS:
        return
    base = [c for c in conditions if c != AGENT_CONDITION]
    if base:
        raise ModelError(
            f"--provider {provider} cannot run the {', '.join(base)} condition"
            f"{'s' if len(base) > 1 else ''}.\n"
            f"  {provider} is a coding agent, not a base model: it brings its own "
            "system prompt, its own tools, and a multi-turn loop.\n"
            "  Scoring it as zero_shot/retrieval/domain_guided would attribute its "
            "scaffolding to the prompt condition and corrupt the comparison.\n"
            f"  Re-run with: --provider {provider} --conditions agentic"
        )


@dataclass
class AgentRun:
    """One CLI agent session, as reported by the agent itself."""

    text: str = ""
    num_turns: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    wall_clock_s: float = 0.0
    resolved_model: str = ""
    session_id: str = ""
    is_error: bool = False
    subtype: str = ""
    stop_reason: str = ""
    permission_denials: list = field(default_factory=list)
    tokens: dict = field(default_factory=dict)
    transcript_path: str = ""
    raw: dict = field(default_factory=dict)


class CLIAgent:
    """Common shape for a coding agent invoked as a subprocess."""

    provider = ""
    binary = ""
    _version = None

    def __init__(self, model=None, timeout=1800, max_usd=None):
        self.model = model
        self.timeout = timeout
        self.max_usd = max_usd
        self.resolved_model = ""
        self.pricing_known = True

    @classmethod
    def available(cls) -> bool:
        return shutil.which(cls.binary) is not None

    @classmethod
    def version(cls) -> str:
        """The CLI's own version string, resolved once per process.

        Cached because it lands in every record and in config.json, and shelling
        out per record would pay for the same answer dozens of times.
        """
        if cls._version is None:
            try:
                out = subprocess.run(
                    [cls.binary, "--version"], capture_output=True, text=True, timeout=60
                )
                cls._version = out.stdout.strip() or out.stderr.strip() or "unknown"
            except (OSError, subprocess.SubprocessError):
                cls._version = "unknown"
        return cls._version

    @property
    def model_id(self) -> str:
        return f"{self.provider}/{self.model or 'default'}"

    def run(self, scaffold, system: str, user: str) -> AgentRun:
        raise NotImplementedError

    def config_snapshot(self) -> dict:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "requested_model": self.model or "(CLI default)",
            "resolved_model": self.resolved_model,
            "cli_version": self.version(),
            "temperature": None,
            "timeout_s": self.timeout,
            "max_budget_usd": self.max_usd,
        }


class ClaudeCLIAgent(CLIAgent):
    """Claude Code in headless mode: `claude -p --output-format json`."""

    provider = "claude-cli"
    binary = "claude"

    def run(self, scaffold, system: str, user: str) -> AgentRun:
        settings = self._settings(scaffold)
        cmd = [
            self.binary,
            "-p",
            user,
            "--output-format",
            "json",
            "--append-system-prompt",
            system,
            # The agent must be able to edit candidate.go without a human to
            # approve each write; it still cannot leave its working directory,
            # and every protected file is re-hashed afterwards.
            "--permission-mode",
            "acceptEdits",
            # acceptEdits covers edits, NOT Bash — and headless has nobody to
            # approve, so every command is auto-denied. Without this the agent
            # cannot run `go test` even once: it writes blind and the agentic
            # condition silently degrades into an expensive zero-shot. Allowing
            # the Go toolchain and nothing else keeps the iteration loop real
            # while leaving every other command denied.
            "--allowedTools",
            "Bash(go:*)",
            "Bash(gofmt:*)",
            # This repository is public. An agent that can search the web can
            # read the hidden schedules out of GitHub, so the network tools go.
            "--disallowed-tools",
            "WebSearch",
            "WebFetch",
            "--settings",
            settings,
            "--strict-mcp-config",
            # Load NO user/project/local settings. Verified necessary, not
            # precautionary: the operator's settings on this machine allow
            # `Bash(python3 -c ' *)`, several `Bash(curl ...)` rules, and
            # `WebFetch(domain:github.com)` — and this repository is public, so
            # any of those is a route to the hidden schedules that the Go-only
            # allowlist would not stop. The deny-list passed via --settings is an
            # explicit file and still applies.
            "--setting-sources",
            "",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.max_usd:
            cmd += ["--max-budget-usd", str(self.max_usd)]

        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=scaffold.dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise ModelError(
                f"{self.binary} exceeded the {self.timeout}s budget for "
                f"{scaffold.fam.name}; no result recorded"
            )
        elapsed = time.time() - started

        if not proc.stdout.strip():
            raise ModelError(
                f"{self.binary} produced no output (exit {proc.returncode}): "
                f"{(proc.stderr or '').strip()[:500]}"
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ModelError(f"unparseable {self.binary} JSON output: {exc}")

        usage = payload.get("usage") or {}
        models = payload.get("modelUsage") or {}
        self.resolved_model = ", ".join(sorted(models)) or self.model or ""
        run = AgentRun(
            text=payload.get("result", "") or "",
            num_turns=int(payload.get("num_turns") or 0),
            cost_usd=round(float(payload.get("total_cost_usd") or 0.0), 6),
            duration_ms=int(payload.get("duration_ms") or 0),
            wall_clock_s=round(elapsed, 2),
            resolved_model=self.resolved_model,
            session_id=payload.get("session_id", "") or "",
            is_error=bool(payload.get("is_error")),
            subtype=payload.get("subtype", "") or "",
            stop_reason=payload.get("stop_reason", "") or "",
            permission_denials=payload.get("permission_denials") or [],
            tokens=_tokens(usage),
            raw={k: v for k, v in payload.items() if k != "result"},
        )
        run.transcript_path = _find_transcript(run.session_id)
        if run.is_error and not scaffold.read_candidate().strip():
            raise ModelError(
                f"{self.binary} reported an error ({run.subtype or 'unknown'}) "
                "and left no candidate"
            )
        return run

    def _settings(self, scaffold) -> str:
        """A deny-list written OUTSIDE the scaffold.

        It names the private workspace and the repository root, which is exactly
        why it cannot live in the scaffold: a deny-list the agent can read is a
        map of where to look.
        """
        from .families import ROOT

        path = os.path.join(scaffold.workspace.root, "agent_settings.json")
        deny = [f"Read(//{scaffold.workspace.root}/**)", f"Read(//{ROOT}/**)"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"permissions": {"deny": deny}}, fh, indent=2)
        return path


AGENTS = {ClaudeCLIAgent.provider: ClaudeCLIAgent}


def build_agent(provider: str, **kw) -> CLIAgent:
    if provider in UNIMPLEMENTED:
        raise ModelError(f"--provider {provider} is not implemented: {UNIMPLEMENTED[provider]}")
    if provider not in AGENTS:
        raise ModelError(
            f"unknown agent provider {provider!r}; expected one of {', '.join(AGENTS)}"
        )
    cls = AGENTS[provider]
    if not cls.available():
        raise ModelError(
            f"--provider {provider} requires `{cls.binary}` on PATH, and it was not found"
        )
    return cls(**kw)


def agent_unavailable(provider: str) -> str:
    """Preflight problem string, or '' if the backend can run."""
    if provider in UNIMPLEMENTED:
        return f"--provider {provider} is not implemented: {UNIMPLEMENTED[provider]}"
    cls = AGENTS.get(provider)
    if cls is None:
        return f"unknown agent provider {provider!r}"
    if not cls.available():
        return f"`{cls.binary}` was not found on PATH (required for --provider {provider})"
    return ""


def _tokens(usage: dict) -> dict:
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    made = int(usage.get("cache_creation_input_tokens") or 0)
    return {
        "input": inp,
        "output": out,
        "cache_read": read,
        "cache_creation": made,
        # The CLI does not break out thinking tokens in its usage block.
        "reasoning": 0,
        "total": inp + out + read + made,
    }


def _find_transcript(session_id: str) -> str:
    """Locate the session JSONL Claude Code persisted for this run.

    Session persistence is left on precisely so this file exists: it is the only
    complete record of what the agent read and ran, and therefore the only way
    to check the isolation claim instead of asserting it.
    """
    if not session_id:
        return ""
    root = os.path.expanduser("~/.claude/projects")
    hits = glob.glob(os.path.join(root, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else ""


# ---------------------------------------------------------------------------
# Filesystem confinement
# ---------------------------------------------------------------------------

# Tools that name a filesystem path, and the input keys that carry it.
_PATH_TOOLS = {
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path",),
    "Grep": ("path",),
}

# Absolute paths and ../ traversals appearing anywhere in a Bash command line.
_PATHISH = re.compile(r"""(?:^|[\s'"=(:])((?:/|\.\./)[^\s'";|&)]+)""")

# Basenames that identify benchmark source. A read of one of these from outside
# the scaffold is the failure this audit exists to catch: `cases.go` holds the
# hidden schedules, `oracle.go` the invariant checker, `<family>.go` the correct
# reference and the mutants.
T4_SOURCE_NAMES = (
    "cases.go",
    "oracle.go",
    "harness.go",
    "shrink.go",
    "result.go",
    "run.go",
    "pilot_results.json",
    "model_results.json",
)

_DENIED_MARKERS = ("permission", "denied", "not allowed", "haven't granted", "has not been granted")


def _transcript_tool_calls(path: str):
    """(tool, input, outcome) for every tool call in a session transcript.

    `outcome` is "denied" when the paired tool_result reports a permission
    failure, otherwise "completed" — the distinction between an agent that tried
    to leave the scaffold and one that succeeded.
    """
    calls, results = [], {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("message") or {}
        for chunk in message.get("content") or []:
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") == "tool_use":
                calls.append((chunk.get("id"), chunk.get("name", ""), chunk.get("input") or {}))
            elif chunk.get("type") == "tool_result":
                body = json.dumps(chunk.get("content"))
                results[chunk.get("tool_use_id")] = (
                    "denied"
                    if (chunk.get("is_error") or any(m in body.lower() for m in _DENIED_MARKERS))
                    else "completed"
                )
    return [(name, inp, results.get(cid, "unknown")) for cid, name, inp in calls]


def _candidate_paths(tool: str, inp: dict) -> list:
    out = []
    for key in _PATH_TOOLS.get(tool, ()):
        value = inp.get(key)
        if isinstance(value, str) and value:
            out.append(value)
    if tool == "Bash":
        out.extend(_PATHISH.findall(str(inp.get("command", ""))))
    return out


def audit_reads(scaffold, transcript_path: str, root: str) -> dict:
    """Every filesystem path the agent named, classified by where it points.

    Modification alone is not the risk — reading is. An agent that read
    `cases.go` has the scoring set whether or not it wrote a single byte, so
    this walks the transcript for paths rather than diffing the disk.
    """
    scaffold_real = os.path.realpath(scaffold.dir)
    root_real = os.path.realpath(root)
    outside, t4_reads = [], []

    for tool, inp, outcome in _transcript_tool_calls(transcript_path):
        for raw in _candidate_paths(tool, inp):
            resolved = os.path.realpath(
                raw if os.path.isabs(raw) else os.path.join(scaffold_real, raw)
            )
            if resolved == scaffold_real or resolved.startswith(scaffold_real + os.sep):
                continue
            record = {"tool": tool, "path": raw, "resolved": resolved, "outcome": outcome}
            outside.append(record)
            in_repo = resolved == root_real or resolved.startswith(root_real + os.sep)
            named_source = os.path.basename(resolved) in T4_SOURCE_NAMES
            if in_repo or named_source or "t4-benchmark" in resolved or "t4eval-" in resolved:
                t4_reads.append(record)

    succeeded = [r for r in t4_reads if r["outcome"] == "completed"]
    return {
        "out_of_scaffold_paths": outside,
        "t4_source_paths": t4_reads,
        # "completed" means the tool call was not denied — which includes calls
        # that only resolve or stat a path (`realpath`) and return no file
        # content. Reported conservatively either way; whether content actually
        # crossed the barrier is answered by the withheld-string scan of the same
        # transcript, which would surface a hidden schedule id verbatim.
        "t4_source_access_completed": succeeded,
        "clean": not succeeded,
    }


# ---------------------------------------------------------------------------
# Leak audit
# ---------------------------------------------------------------------------

def audit_session(scaffold, run: AgentRun) -> dict:
    """Did the agent stay inside the scaffold?

    The scaffold makes the withheld material unreachable by construction; this
    checks the construction actually held. Four independent signals:

      1. the session transcript, searched for the withheld strings — the hidden
         schedule ids, the oracle, the reference, and the paths to both;
      2. every filesystem path the agent named, resolved and classified — a
         *read* of `cases.go` is the failure, whether or not anything was
         written, so disk state alone would not catch it;
      3. `permission_denials`, which records attempts the deny-list stopped;
      4. the scaffold's protected files, re-hashed — an agent that edited its
         own test was not measuring the contract.

    A finding never silently discards the result. It is recorded with it, so a
    contaminated run is visible in `model_results.json` rather than absent from
    it.
    """
    from .families import ROOT

    findings = []
    withheld = {sid: "hidden schedule id" for sid in scaffold.hidden_ids()}
    withheld.update(
        {
            "HiddenCases": "hidden-schedule accessor",
            "CheckFinancial": "oracle entry point",
            "func Correct(": "correct reference",
            "oracle.go": "oracle source",
            scaffold.workspace.root: "private workspace path",
            ROOT: "benchmark repository path",
        }
    )

    transcript_checked = False
    if run.transcript_path and os.path.exists(run.transcript_path):
        transcript_checked = True
        try:
            with open(run.transcript_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for needle, why in withheld.items():
                if needle and needle in text:
                    findings.append(f"transcript mentions {needle!r} ({why})")
        except OSError as exc:
            findings.append(f"transcript unreadable: {exc}")

    modified = scaffold.integrity()
    if modified:
        findings.append("protected scaffold files changed: " + ", ".join(modified))

    reads = audit_reads(scaffold, run.transcript_path, ROOT)
    for hit in reads["t4_source_access_completed"]:
        findings.append(
            "benchmark source outside the scaffold was accessed without being "
            f"denied: {hit['tool']} {hit['resolved']}"
        )

    # Denials are reported separately from contamination, and deliberately do
    # not set `clean`. They mean the sandbox held, not that the run is tainted —
    # and conflating the two would either cry wolf on an incidental `ls` or,
    # worse, train the reader to ignore the flag that does matter.
    denied = [
        f"{d.get('tool_name', '?')}: {str(d.get('tool_input', ''))[:120]}"
        for d in run.permission_denials
    ]
    return {
        "clean": not findings,
        "contamination": findings,
        "transcript_checked": transcript_checked,
        "transcript": os.path.basename(run.transcript_path) if run.transcript_path else "",
        "scaffold_contents": scaffold.audit_contents(),
        "protected_files_modified": modified,
        "read_audit": {
            "out_of_scaffold_path_refs": len(reads["out_of_scaffold_paths"]),
            "t4_source_path_refs": len(reads["t4_source_paths"]),
            "t4_source_access_completed": reads["t4_source_access_completed"],
            "attempts": reads["t4_source_paths"],
            "clean": reads["clean"],
        },
        "denied_tool_attempts": len(denied),
        "denied_sample": denied[:10],
        "findings": findings,
    }
