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
import json
import os
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
# Leak audit
# ---------------------------------------------------------------------------

def audit_session(scaffold, run: AgentRun) -> dict:
    """Did the agent stay inside the scaffold?

    The scaffold makes the withheld material unreachable by construction; this
    checks the construction actually held. Three independent signals:

      1. the session transcript, searched for the withheld strings — the hidden
         schedule ids, the oracle, the reference, and the paths to both;
      2. `permission_denials`, which records attempts the deny-list stopped;
      3. the scaffold's protected files, re-hashed — an agent that edited its
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
        "denied_tool_attempts": len(denied),
        "denied_sample": denied[:10],
        "findings": findings,
    }
