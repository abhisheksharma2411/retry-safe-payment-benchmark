"""Provider backends for the generation runner.

The benchmark is provider-agnostic: a candidate is just Go source, and the
oracle does not care which vendor produced it. Everything vendor-specific is
confined to this module. Each client exposes the same two-method surface —

    complete(system: str, messages: list) -> Completion
    config_snapshot() -> dict

— where `messages` is a canonical `[{"role": "user"|"assistant", "content": str}]`
list that each backend adapts to its own wire format. Nothing in prompt
rendering, the compile gate, oracle scoring, or record emission knows which
provider answered.

Every backend negotiates capabilities rather than assuming them: it starts from
its preferred request shape and drops whatever the target model rejects,
recording what it dropped. This is what lets one runner sweep across model
generations and vendors whose parameter support differs (OpenAI's reasoning
models reject `temperature` and rename `max_tokens`; the current Claude models
reject `temperature` outright).
"""

import os
import sys

from .model import Completion, ModelError

# ---------------------------------------------------------------------------
# List price in USD per million tokens: (input, output).
#
# A convenience table only. It is not authoritative and will go stale as vendors
# reprice; entries are omitted rather than guessed. An unlisted model records
# cost_usd = 0.0 and warns. Pass --price-in/--price-out to record real cost.
# ---------------------------------------------------------------------------
PRICING = {
    "anthropic": {
        "claude-fable-5": (10.0, 50.0),
        "claude-mythos-5": (10.0, 50.0),
        "claude-opus-5": (5.0, 25.0),
        "claude-opus-4-8": (5.0, 25.0),
        "claude-opus-4-7": (5.0, 25.0),
        "claude-opus-4-6": (5.0, 25.0),
        "claude-sonnet-5": (3.0, 15.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
    },
    "openai": {
        "gpt-5": (1.25, 10.0),
        "gpt-5-codex": (1.25, 10.0),
        "gpt-5-mini": (0.25, 2.0),
        "gpt-5-nano": (0.05, 0.40),
        "gpt-4.1": (2.0, 8.0),
        "gpt-4.1-mini": (0.40, 1.60),
        "gpt-4.1-nano": (0.10, 0.40),
    },
    "gemini": {
        # Gemini 3 pricing is deliberately absent rather than guessed.
        "gemini-2.5-pro": (1.25, 10.0),
        "gemini-2.5-flash": (0.30, 2.50),
    },
}

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5",
    "gemini": "gemini-3.1-pro-preview",
}

API_KEY_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}

PROVIDERS = tuple(DEFAULT_MODELS)

_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25


class _BaseClient:
    """Shared pricing, accounting, and capability-negotiation bookkeeping."""

    provider = ""

    def __init__(self, model_id=None, temperature=None, max_tokens=32000,
                 effort="high", price_in=None, price_out=None):
        self.model_id = model_id or os.environ.get("T4_MODEL_ID") or DEFAULT_MODELS[self.provider]
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.effort = effort

        table = PRICING.get(self.provider, {})
        listed = table.get(self.model_id)
        self.price_in = listed[0] if listed and price_in is None else (price_in or 0.0)
        self.price_out = listed[1] if listed and price_out is None else (price_out or 0.0)
        self.pricing_known = listed is not None or price_in is not None

        self._use_temperature = temperature is not None
        self._dropped = []
        self.resolved_model = ""

    def _note(self, param, message):
        if param not in self._dropped:
            self._dropped.append(param)
            print(
                f"  [model] {self.model_id} rejected `{param}`; retrying without it "
                f"({str(message).strip()[:110]})",
                file=sys.stderr,
            )

    def cost_of(self, comp: Completion) -> float:
        return round(
            (comp.input_tokens / 1e6) * self.price_in
            + (comp.output_tokens / 1e6) * self.price_out
            + (comp.cache_read_tokens / 1e6) * self.price_in * _CACHE_READ_MULT
            + (comp.cache_creation_tokens / 1e6) * self.price_in * _CACHE_WRITE_MULT,
            6,
        )

    def config_snapshot(self) -> dict:
        return {
            "stub": False,
            "provider": self.provider,
            "model_id": self.model_id,
            "resolved_model": self.resolved_model or self.model_id,
            "temperature": self.temperature if self._use_temperature else None,
            "temperature_requested": self.temperature,
            "max_tokens": self.max_tokens,
            "effort": self.effort,
            "dropped_params": list(self._dropped),
            "price_per_mtok_in": self.price_in,
            "price_per_mtok_out": self.price_out,
            "pricing_known": self.pricing_known,
        }


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicClient(_BaseClient):
    provider = "anthropic"

    def __init__(self, **kw):
        super().__init__(**kw)
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise ModelError(
                "the `anthropic` package is not installed; "
                "pip install -r generation/runner/requirements.txt"
            ) from exc
        self._sdk = anthropic
        self.client = anthropic.Anthropic()
        self._use_thinking = True
        self._use_effort = True

    def _params(self, system, messages):
        params = {
            "model": self.model_id,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if self._use_thinking:
            params["thinking"] = {"type": "adaptive"}
        if self._use_effort:
            params["output_config"] = {"effort": self.effort}
        if self._use_temperature:
            params["temperature"] = self.temperature
        return params

    def _narrow(self, message):
        low = str(message).lower()
        for attr, name, token in (
            ("_use_temperature", "temperature", "temperature"),
            ("_use_effort", "output_config.effort", "effort"),
            ("_use_thinking", "thinking", "thinking"),
        ):
            if getattr(self, attr) and token in low:
                setattr(self, attr, False)
                self._note(name, message)
                return True
        for attr, name in (
            ("_use_temperature", "temperature"),
            ("_use_effort", "output_config.effort"),
            ("_use_thinking", "thinking"),
        ):
            if getattr(self, attr):
                setattr(self, attr, False)
                self._note(name, message)
                return True
        return False

    def complete(self, system, messages) -> Completion:
        last = None
        for _ in range(4):
            try:
                with self.client.messages.stream(**self._params(system, messages)) as stream:
                    msg = stream.get_final_message()
            except self._sdk.BadRequestError as exc:
                last = exc
                if self._narrow(exc):
                    continue
                raise ModelError(f"{self.model_id}: {exc}") from exc
            except self._sdk.APIStatusError as exc:
                raise ModelError(f"{self.model_id}: HTTP {exc.status_code}: {exc}") from exc
            except self._sdk.APIConnectionError as exc:
                raise ModelError(f"{self.model_id}: connection failed: {exc}") from exc

            if msg.stop_reason == "refusal":
                raise ModelError(f"{self.model_id} declined the request (stop_reason=refusal)")

            self.resolved_model = getattr(msg, "model", "") or self.model_id
            usage = msg.usage
            comp = Completion(
                text="".join(b.text for b in msg.content if getattr(b, "type", "") == "text"),
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                stop_reason=msg.stop_reason or "",
                dropped_params=list(self._dropped),
            )
            comp.cost_usd = self.cost_of(comp)
            return comp
        raise ModelError(f"{self.model_id}: no accepted request shape: {last}")


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class OpenAIClient(_BaseClient):
    """Chat Completions backend.

    The reasoning models (gpt-5 family, o-series) rename `max_tokens` to
    `max_completion_tokens` and accept only the default temperature, so both are
    negotiated rather than assumed.
    """

    provider = "openai"

    def __init__(self, **kw):
        super().__init__(**kw)
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise ModelError(
                "the `openai` package is not installed; "
                "pip install -r generation/runner/requirements.txt"
            ) from exc
        self._sdk = openai
        self.client = openai.OpenAI()
        self._token_param = "max_completion_tokens"
        self._use_effort = True

    def _params(self, system, messages):
        params = {
            "model": self.model_id,
            "messages": [{"role": "system", "content": system}, *messages],
            self._token_param: self.max_tokens,
        }
        if self._use_effort:
            params["reasoning_effort"] = self.effort
        if self._use_temperature:
            params["temperature"] = self.temperature
        return params

    def _narrow(self, message):
        low = str(message).lower()
        if self._token_param == "max_completion_tokens" and "max_completion_tokens" in low:
            self._token_param = "max_tokens"
            self._note("max_completion_tokens", message)
            return True
        if self._token_param == "max_tokens" and "max_tokens" in low and "max_completion" in low:
            self._token_param = "max_completion_tokens"
            self._note("max_tokens", message)
            return True
        if self._use_temperature and "temperature" in low:
            self._use_temperature = False
            self._note("temperature", message)
            return True
        if self._use_effort and ("reasoning_effort" in low or "reasoning" in low):
            self._use_effort = False
            self._note("reasoning_effort", message)
            return True
        for attr, name in (("_use_temperature", "temperature"), ("_use_effort", "reasoning_effort")):
            if getattr(self, attr):
                setattr(self, attr, False)
                self._note(name, message)
                return True
        return False

    def complete(self, system, messages) -> Completion:
        last = None
        for _ in range(5):
            try:
                resp = self.client.chat.completions.create(**self._params(system, messages))
            except self._sdk.BadRequestError as exc:
                last = exc
                if self._narrow(exc):
                    continue
                raise ModelError(f"{self.model_id}: {exc}") from exc
            except self._sdk.RateLimitError as exc:
                raise ModelError(f"{self.model_id}: rate limited / out of credits: {exc}") from exc
            except self._sdk.APIStatusError as exc:
                raise ModelError(f"{self.model_id}: HTTP {exc.status_code}: {exc}") from exc
            except self._sdk.APIConnectionError as exc:
                raise ModelError(f"{self.model_id}: connection failed: {exc}") from exc

            choice = resp.choices[0]
            self.resolved_model = getattr(resp, "model", "") or self.model_id
            usage = resp.usage
            details = getattr(usage, "prompt_tokens_details", None)
            comp = Completion(
                text=choice.message.content or "",
                # completion_tokens already includes reasoning tokens
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cache_read_tokens=getattr(details, "cached_tokens", 0) or 0 if details else 0,
                reasoning_tokens=(
                    getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
                ),
                stop_reason=choice.finish_reason or "",
                dropped_params=list(self._dropped),
            )
            comp.cost_usd = self.cost_of(comp)
            return comp
        raise ModelError(f"{self.model_id}: no accepted request shape: {last}")


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


class GeminiClient(_BaseClient):
    """Google Gemini backend via the `google-genai` SDK.

    Gemini reports thinking tokens (`thoughts_token_count`) separately from
    `candidates_token_count`, and both are billed as output — so the two are
    summed into `output_tokens` and the thinking share is also recorded on its
    own for auditing.
    """

    provider = "gemini"

    def __init__(self, **kw):
        super().__init__(**kw)
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover
            raise ModelError(
                "the `google-genai` package is not installed; "
                "pip install -r generation/runner/requirements.txt"
            ) from exc
        self._genai, self._types = genai, types
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.client = genai.Client(api_key=key) if key else genai.Client()

    def _config(self, system):
        kwargs = {"system_instruction": system, "max_output_tokens": self.max_tokens}
        if self._use_temperature:
            kwargs["temperature"] = self.temperature
        return self._types.GenerateContentConfig(**kwargs)

    def _contents(self, messages):
        # Gemini names the assistant role "model".
        return [
            self._types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[self._types.Part(text=m["content"])],
            )
            for m in messages
        ]

    def complete(self, system, messages) -> Completion:
        last = None
        for _ in range(3):
            try:
                resp = self.client.models.generate_content(
                    model=self.model_id,
                    contents=self._contents(messages),
                    config=self._config(system),
                )
            except Exception as exc:  # google-genai raises ClientError/ServerError
                last = exc
                low = str(exc).lower()
                if self._use_temperature and "temperature" in low:
                    self._use_temperature = False
                    self._note("temperature", exc)
                    continue
                raise ModelError(f"{self.model_id}: {str(exc)[:400]}") from exc

            usage = resp.usage_metadata
            thoughts = getattr(usage, "thoughts_token_count", 0) or 0
            candidates = getattr(usage, "candidates_token_count", 0) or 0
            self.resolved_model = getattr(resp, "model_version", "") or self.model_id
            comp = Completion(
                text=resp.text or "",
                input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                output_tokens=candidates + thoughts,
                cache_read_tokens=getattr(usage, "cached_content_token_count", 0) or 0,
                reasoning_tokens=thoughts,
                stop_reason=str(getattr(resp.candidates[0], "finish_reason", "")) if resp.candidates else "",
                dropped_params=list(self._dropped),
            )
            comp.cost_usd = self.cost_of(comp)
            return comp
        raise ModelError(f"{self.model_id}: no accepted request shape: {last}")


CLIENTS = {
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "gemini": GeminiClient,
}


def build_client(provider, **kw):
    if provider not in CLIENTS:
        raise ModelError(f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}")
    return CLIENTS[provider](**kw)


def missing_credentials(provider) -> str:
    """Return a human-readable problem string, or '' if credentials are present."""
    names = API_KEY_ENV[provider]
    if any(os.environ.get(n) for n in names):
        return ""
    return f"{names[0]} is not set (required for --provider {provider})"
