"""Shared model-call types for the generation runner.

Vendor-specific code lives in `providers.py`; this module holds only what every
backend shares — the `Completion` record, the error type, and the offline stub.
Keeping these here (and importing nothing from `providers`) is what avoids a
circular import, since every backend depends on `Completion`.
"""

from dataclasses import dataclass, field

from .families import FACTORY_NAME

DEFAULT_MAX_TOKENS = 32000
DEFAULT_EFFORT = "high"


class ModelError(RuntimeError):
    """A model call failed in a way the runner cannot work around."""


@dataclass
class Completion:
    """One model response plus its accounting.

    `output_tokens` is the billed output count for the provider in question and
    always includes reasoning/thinking tokens; `reasoning_tokens` records how
    much of that was reasoning, for auditing.
    """

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str = ""
    dropped_params: list = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def token_dict(self) -> dict:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_creation": self.cache_creation_tokens,
            "reasoning": self.reasoning_tokens,
            "total": self.total_tokens,
        }


class StubClient:
    """Offline stand-in that returns the family's shipped correct reference.

    This is the acceptance check P5 specifies for the runner: it exercises the
    whole pipeline — prompt rendering, response archiving, extraction, the
    compile gate, oracle scoring, record and config.json emission — with no API
    key, no network, and no cost. Because the reference is retry-safe, a stub
    run should score 10/10 on every family; anything less means the harness
    wiring regressed, not the model.
    """

    provider = "stub"
    model_id = "stub/correct-reference"
    pricing_known = True
    resolved_model = "stub/correct-reference"

    def __init__(self, family_getter):
        self._family_for = family_getter

    def complete(self, system: str, messages: list) -> Completion:
        fam = self._family_for()
        source = (
            f"package {fam.name}\n\n"
            'import "t4bench/harness"\n\n'
            "// Stub candidate: delegates to the family's shipped correct reference.\n"
            f"func {FACTORY_NAME}(env harness.Env) Service {{ return Correct(env) }}\n"
        )
        return Completion(text=f"```go\n{source}```\n", stop_reason="end_turn")

    def cost_of(self, comp: Completion) -> float:
        return 0.0

    def config_snapshot(self) -> dict:
        return {
            "stub": True,
            "provider": "stub",
            "model_id": self.model_id,
            "resolved_model": self.model_id,
            "temperature": None,
            "note": "offline stub returning the shipped correct reference; no API calls",
        }
