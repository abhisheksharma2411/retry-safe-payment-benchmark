"""Render the four generation-condition prompts into concrete (system, user)
message pairs.

The templates in `generation/prompts/` are fill-in-the-blank documents written
for a human prompt author: they carry `{{PLACEHOLDERS}}`, worked `capture`
values, and a few author-facing parentheticals ("include the Env definitions
verbatim, as in the zero-shot condition"). This module turns one of those
documents into something a model can actually be sent, and records exactly what
was sent so a reviewer can audit it.

The templates are self-contained: each one carries the full injected-environment
interface and its own `Request` definition, so it is correct read standalone.
Three expansions remain, documented in docs/EVAL_RUNNER.md:

  1. The templates hardcode `capture`'s `Amount` payload field. Families that
     carry a `Payload` instead (`outbox`, `consumer`) get the field rewritten,
     so every condition describes that family's actual interface.

  2. Every condition gets the same output contract appended (one Go file, fixed
     factory name, `llm`-prefixed unexported helpers). Applied uniformly, so it
     cannot bias one condition against another.

  3. `agentic` assumes an interactive shell the runner does not grant. It gets
     an adapter note explaining that the runner performs the build-and-run loop
     against the PUBLIC schedules on its behalf, plus the interface inline
     (that condition's premise is a repo checkout it cannot actually read here).

`_DEFERRED_ENV` and the `Request`-injection below are retained as defensive
no-ops: they fire only if a template regresses to deferring its definitions to
another condition, which would otherwise silently ship that condition with no
environment description and corrupt the cross-condition comparison.
"""

import hashlib
import os
import re

from .families import FACTORY_NAME, HELPER_PREFIX, RAIL_PROFILE, Family

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROMPT_DIR = os.path.join(ROOT, "generation", "prompts")

CONDITIONS = ("zero_shot", "retrieval", "agentic", "domain_guided")

# An author-facing aside deferring the environment definitions to the zero-shot
# condition. Matches both the retrieval and domain_guided phrasings.
_DEFERRED_ENV = re.compile(r"\([^()]*?zero-shot condition[^()]*?\)", re.S)

# Scaffolding line in retrieval.md that introduces the shipped illustrative
# corpus. The passages themselves are the corpus; this line is a note to the
# prompt author and must not be sent to the model.
_RETRIEVER_ASIDE = re.compile(
    r"^Illustrative passages \(replace with retriever output\):\s*$", re.M
)

# `{{METHOD}}   // e.g. Capture(Request) harness.Response` renders as a
# self-referential duplicate once substituted; drop the trailing aside.
_EG_COMMENT = re.compile(r"[ \t]*// e\.g\. .*$", re.M)


def _read(condition: str) -> str:
    path = os.path.join(PROMPT_DIR, f"{condition}.md")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _env_block() -> str:
    """The canonical Env/Store/Provider block, lifted from zero_shot.md."""
    text = _read("zero_shot")
    match = re.search(r"```go\n(type Env interface.*?)```", text, re.S)
    if not match:
        raise RuntimeError(
            "generation/prompts/zero_shot.md no longer contains the `type Env interface` "
            "code block; prompts.py cannot build the shared environment definition."
        )
    return match.group(1).rstrip()


def _request_struct(fam: Family) -> str:
    """The family's `Request` declaration, as the model should see it."""
    return (
        "type Request struct {\n"
        "    ID       string\n"
        "    Identity harness.Identity\n"
        f"    {fam.payload_field:<8} harness.Money\n"
        "}\n"
    )


def _split_sections(text: str) -> tuple[str, str]:
    """Return (system, user) from a `## SYSTEM` / `## USER` template."""
    sys_at = text.find("\n## SYSTEM")
    usr_at = text.find("\n## USER")
    if sys_at == -1 or usr_at == -1 or usr_at < sys_at:
        raise RuntimeError("prompt template is missing a `## SYSTEM` / `## USER` pair")
    system = text[sys_at + len("\n## SYSTEM") : usr_at]
    user = text[usr_at + len("\n## USER") :]
    return system.strip(), user.strip()


def _output_contract(fam: Family) -> str:
    return f"""
---

### Output contract

Return exactly one Go file and nothing else:

- `package {fam.name}` — the file is compiled inside the family package.
- Provide `func {FACTORY_NAME}(env harness.Env) Service`.
- **`Request`, `Service`, and `Factory` already exist in this package.** They are
  shown above so you know their shape — do NOT declare them again. A second
  `type Request struct` or `type Service interface` is a redeclaration and fails
  the build.
- Every unexported top-level identifier you declare (func, type, var, const)
  MUST begin with `{HELPER_PREFIX}` — for example `{HELPER_PREFIX}Fingerprint`,
  `{HELPER_PREFIX}State`. The file is compiled alongside the other files in this
  package, and unprefixed helper names collide with them and fail the build.
- Import only `t4bench/harness` and the Go standard library.
- No `main`, no tests, no markdown fences. Any rationale must be a Go comment
  inside the file.
""".rstrip()


def _agentic_adapter(fam: Family) -> str:
    return f"""
---

### Runner adapter (agentic condition)

You do not have an interactive shell here; the runner performs the iteration
loop on your behalf, exactly as the rules above require:

1. It writes your file to `tasks/{fam.name}/candidate_llm.go`.
2. It runs the Go compiler over the module.
3. If it builds, it executes the **PUBLIC** schedules only
   (`{fam.name}.PublicCases()`) through the shared oracle.
4. It returns the compiler output and the per-schedule oracle verdicts to you,
   and you may then return a revised file.

Hidden schedules are never executed during iteration and never shown to you, so
the hidden-set commitment in ARTIFACT_EVALUATION.md holds by construction.

Because you cannot read the repository, the interface, environment, and task
semantics you need are reproduced in full below.

The injected environment:

```go
{_env_block()}
```

The interface you must implement:

```go
type Request struct {{
    ID       string
    Identity harness.Identity
    {fam.payload_field:<8} harness.Money
}}
type Service interface {{
    {fam.method}
}}
```

Task: {fam.task_description}
""".rstrip()


def _scaffold_adapter(fam: Family) -> str:
    """The agentic adapter for a real CLI agent, which does have a shell.

    Same condition, opposite premise from `_agentic_adapter`: that one tells a
    single-shot model the runner will iterate on its behalf, because it cannot.
    A CLI agent can, so it is told where it is and what it may run instead —
    telling it otherwise would be false and would suppress the very iteration
    the agentic condition is meant to measure.
    """
    return f"""
---

### Your working directory

You are in a prepared working directory for this task. It contains the
interface, the injected-environment API, a candidate file to edit, and a test
wired to the **PUBLIC** fault schedules.

- `README.md` — the contract and the rules. Read it first.
- `API.md`, `harness/api.go` — the injected environment.
- `tasks/{fam.name}/{fam.name}.go` — `Request`, `Service`, `Factory`. Fixed.
- `tasks/{fam.name}/candidate.go` — **the only file you edit.**
- `tasks/{fam.name}/public_test.go` — the public schedules. Fixed.

Iterate however you like:

```sh
go build ./...   # type check
go test ./...    # PUBLIC fault schedules, through the benchmark's own oracle
```

`go test` names the invariants each failing schedule violated. Use it.

The hidden schedules, the oracle implementation, and the reference solution are
not in this directory and cannot be reached from it. Your final `candidate.go`
is scored on the **held-out** schedules, so a green `go test` is a floor, not a
finish line: the public schedules cover retries, concurrency, payload conflict,
and false dedup, and the held-out ones additionally cover crash-and-recover and
unknown provider outcomes. Reason about the contract, not about the four tests
you can see.
""".rstrip()


def _scaffold_contract(fam: Family) -> str:
    """Output contract for an agent that edits a file rather than returning one."""
    return f"""
---

### Output contract

Your deliverable is the contents of `tasks/{fam.name}/candidate.go` when you stop.
Nothing you write in chat is collected.

- Keep `package {fam.name}` and `func {FACTORY_NAME}(env harness.Env) Service`.
- **`Request`, `Service`, and `Factory` already exist in `{fam.name}.go`.** Do NOT
  declare them again in `candidate.go`; a redeclaration fails the build.
- Every unexported top-level identifier you declare (func, type, var, const)
  MUST begin with `{HELPER_PREFIX}` — for example `{HELPER_PREFIX}Fingerprint`,
  `{HELPER_PREFIX}State`. Scoring compiles your file alongside the other files in
  this package, and an unprefixed helper collides with them and fails the build.
- Import only `t4bench/harness` and the Go standard library.
- Do not edit any other file, and do not add or change tests. Every other file
  is hashed before and after this session.
- Leave the file compiling. A file that does not build scores zero on every
  schedule.
""".rstrip()


def _substitute(text: str, fam: Family) -> str:
    text = _DEFERRED_ENV.sub(
        "The injected environment:\n\n```go\n" + _env_block() + "\n```", text
    )
    text = _RETRIEVER_ASIDE.sub("", text)
    text = text.replace("{{RETRIEVED_PASSAGES}}", "")
    text = text.replace("{{FAMILY}}", fam.name)
    text = text.replace("{{SERVICE_TYPE}}", "Service")
    text = text.replace("{{METHOD}}", fam.method)
    text = text.replace("{{FACTORY}}", f"{FACTORY_NAME}(env harness.Env) Service")
    text = text.replace("{{RAIL_PROFILE}}", RAIL_PROFILE)
    text = text.replace("{{TASK_DESCRIPTION}}", fam.task_description)
    text = _EG_COMMENT.sub("", text)

    # The templates hardcode the `capture` payload field; families that carry a
    # `Payload` instead of an `Amount` would otherwise be handed a Request
    # struct that does not match their own package.
    if fam.payload_field != "Amount":
        text = re.sub(
            r"^(\s*)Amount(\s+)harness\.Money\s*$",
            rf"\g<1>{fam.payload_field}\g<2>harness.Money",
            text,
            flags=re.M,
        )

    # retrieval.md names the Service method but never defines `Request`, so that
    # condition alone would leave the model guessing the payload field name.
    # Inject the struct so all four conditions describe the same interface.
    if "type Request struct" not in text and "type Service interface" in text:
        text = text.replace(
            "type Service interface",
            _request_struct(fam) + "type Service interface",
            1,
        )

    leftover = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if leftover:
        raise RuntimeError(f"unsubstituted placeholders remain: {sorted(set(leftover))}")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


ADAPTERS = ("runner", "scaffold")


def render(condition: str, fam: Family, adapter: str = "runner") -> tuple[str, str]:
    """Render (system, user) for one condition and family.

    `adapter` selects who runs the build-and-test loop in the agentic condition:
    "runner" for a single-shot API model (the runner iterates for it), "scaffold"
    for a CLI coding agent with its own shell. The condition's own template is
    identical either way — only the environment description and the delivery
    mechanism differ, because only those actually differ.
    """
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}; expected one of {CONDITIONS}")
    if adapter not in ADAPTERS:
        raise ValueError(f"unknown adapter {adapter!r}; expected one of {ADAPTERS}")
    if adapter == "scaffold" and condition != "agentic":
        raise ValueError(
            "the scaffold adapter exists for CLI coding agents, which are locked to "
            f"the agentic condition; got {condition!r}"
        )
    system, user = _split_sections(_read(condition))
    system = _substitute(system, fam)
    user = _substitute(user, fam)
    if condition == "agentic":
        user += "\n" + (
            _scaffold_adapter(fam) if adapter == "scaffold" else _agentic_adapter(fam)
        )
    user += "\n" + (
        _scaffold_contract(fam) if adapter == "scaffold" else _output_contract(fam)
    )
    return system, user


def prompt_hash(system: str, user: str) -> str:
    """Stable digest of the exact text sent, for the run's config.json."""
    h = hashlib.sha256()
    h.update(system.encode("utf-8"))
    h.update(b"\0")
    h.update(user.encode("utf-8"))
    return h.hexdigest()


def template_hashes() -> dict:
    """SHA-256 of each on-disk template, so a run records its prompt provenance."""
    out = {}
    for cond in CONDITIONS:
        digest = hashlib.sha256(_read(cond).encode("utf-8")).hexdigest()
        out[cond] = digest
    return out
