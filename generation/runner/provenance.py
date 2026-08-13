"""Model snapshot provenance, for the training-contamination argument.

The paper wants to claim that a model evaluated here could not have trained on
this benchmark. That claim rests on one comparison — the model snapshot predates
the repository's publication — so the two dates have to be *measured*, not
asserted, and every record has to carry them.

What each API actually exposes differs, and the gaps are recorded as gaps:

  * **Gemini** exposes a dated snapshot in `models.get(...).version`
    (`3.1-pro-preview-01-2026`), and the served version in the generate
    response (`model_version`), which is what resolves a `-latest` alias.
  * **Anthropic** exposes `created_at` on `models.retrieve(...)`.
  * **OpenAI** exposes a `created` unix timestamp on `models.retrieve(...)`.
  * **None of them expose a training cutoff.** It is therefore recorded as
    `null` with the reason, never guessed — a fabricated cutoff in a
    contamination argument would be worse than an absent one.

The snapshot date is a *lower* bound on the training cutoff: a model published
in January cannot have trained on anything after January. So
`snapshot_predates_repo_publication` is sound in the direction the paper needs,
and it is the only claim made here.

What this does NOT establish, and the paper should not claim: that the model has
no knowledge of idempotency, reserve-then-effect, or retry-safe payment design.
Those are widely documented and are certainly in every model's training data.
The claim is narrower and checkable — the model cannot have memorised *this
repository*, its hidden schedules, or its reference solutions.
"""

import datetime
import re

# First public commit of this repository. The contamination boundary: a model
# snapshot from before this date cannot have seen the benchmark.
REPO_PUBLISHED_AT = "2026-08-13"

# Trailing MM-YYYY / YYYY-MM in a Gemini version string, e.g. "3.1-pro-preview-01-2026".
_MONTH_YEAR = re.compile(r"(?:^|[-_])(\d{2})-(\d{4})$")
_YEAR_MONTH = re.compile(r"(?:^|[-_])(\d{4})-(\d{2})$")


def _snapshot_date(version: str) -> str:
    """`3.1-pro-preview-01-2026` -> `2026-01`, or '' when undated."""
    if not version:
        return ""
    m = _MONTH_YEAR.search(version)
    if m:
        return f"{m.group(2)}-{m.group(1)}"
    m = _YEAR_MONTH.search(version)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


def _predates(date_str: str) -> bool:
    """Is `date_str` (YYYY-MM or YYYY-MM-DD) strictly before publication?"""
    if not date_str:
        return None
    normalized = date_str if len(date_str) > 7 else f"{date_str}-01"
    try:
        when = datetime.date.fromisoformat(normalized[:10])
    except ValueError:
        return None
    return when < datetime.date.fromisoformat(REPO_PUBLISHED_AT)


def _blank(requested, resolved, provider, reason):
    return {
        "requested_model": requested,
        "resolved_model": resolved or requested,
        "alias_requested": bool(resolved and resolved != requested),
        "model_snapshot": "",
        "model_display_name": "",
        "snapshot_date": "",
        "release_date": None,
        "training_cutoff": None,
        "training_cutoff_source": f"not exposed by the {provider} API",
        "repo_published_at": REPO_PUBLISHED_AT,
        "snapshot_predates_repo_publication": None,
        "note": reason,
    }


def _gemini(client, requested, resolved):
    info = _blank(requested, resolved, "Gemini", "")
    # Prefer the model actually served: a `-latest` alias resolves to a concrete
    # snapshot only in the generate response, and the alias's own metadata
    # carries no date at all.
    target = resolved or requested
    try:
        meta = client.client.models.get(model=target)
    except Exception as exc:  # noqa: BLE001 - provenance must never break a run
        info["note"] = f"models.get({target}) failed: {str(exc)[:160]}"
        return info
    info["model_snapshot"] = getattr(meta, "version", "") or ""
    info["model_display_name"] = getattr(meta, "display_name", "") or ""
    info["snapshot_date"] = _snapshot_date(info["model_snapshot"])
    info["snapshot_predates_repo_publication"] = _predates(info["snapshot_date"])
    if not info["snapshot_date"]:
        info["note"] = (
            f"the Gemini API reports version {info['model_snapshot']!r} for this "
            "model, which carries no date; the contamination boundary cannot be "
            "checked from the API for this snapshot"
        )
    return info


def _anthropic(client, requested, resolved):
    info = _blank(requested, resolved, "Anthropic", "")
    target = resolved or requested
    try:
        meta = client.client.models.retrieve(target)
    except Exception as exc:  # noqa: BLE001
        info["note"] = f"models.retrieve({target}) failed: {str(exc)[:160]}"
        return info
    created = getattr(meta, "created_at", None)
    info["model_snapshot"] = getattr(meta, "id", "") or target
    info["model_display_name"] = getattr(meta, "display_name", "") or ""
    if created is not None:
        text = created.isoformat() if hasattr(created, "isoformat") else str(created)
        info["release_date"] = text
        info["snapshot_date"] = text[:10]
        info["snapshot_predates_repo_publication"] = _predates(text[:10])
    return info


def _openai(client, requested, resolved):
    info = _blank(requested, resolved, "OpenAI", "")
    target = resolved or requested
    try:
        meta = client.client.models.retrieve(target)
    except Exception as exc:  # noqa: BLE001
        info["note"] = f"models.retrieve({target}) failed: {str(exc)[:160]}"
        return info
    info["model_snapshot"] = getattr(meta, "id", "") or target
    created = getattr(meta, "created", None)
    if created:
        text = datetime.datetime.fromtimestamp(
            int(created), tz=datetime.timezone.utc
        ).date().isoformat()
        info["release_date"] = text
        info["snapshot_date"] = text
        info["snapshot_predates_repo_publication"] = _predates(text)
    return info


_LOOKUPS = {"gemini": _gemini, "anthropic": _anthropic, "openai": _openai}


def model_provenance(client, requested: str, resolved: str = "") -> dict:
    """Snapshot provenance for one model, cached on the client.

    Called after the first completion so a `-latest` alias has resolved to the
    concrete snapshot that actually served the request.
    """
    cached = getattr(client, "_provenance", None)
    if cached and cached.get("resolved_model") == (resolved or requested):
        return cached
    lookup = _LOOKUPS.get(getattr(client, "provider", ""))
    info = (
        lookup(client, requested, resolved)
        if lookup
        else _blank(requested, resolved, getattr(client, "provider", "unknown"),
                    "no model-metadata endpoint is wired for this provider")
    )
    client._provenance = info
    return info


def agent_provenance(resolved: str, cli_version: str) -> dict:
    """Provenance for a CLI coding agent.

    The CLI reports which model served the session but exposes no model
    metadata endpoint, so the snapshot date is unavailable unless an Anthropic
    API key is present. Recorded as unavailable rather than inferred from the
    model name, which would be a guess dressed as a measurement.
    """
    info = _blank(resolved or "cli-default", resolved, "Claude Code CLI",
                  "the CLI reports the serving model but exposes no model-metadata "
                  "endpoint; set ANTHROPIC_API_KEY to resolve the snapshot date")
    info["alias_requested"] = False
    info["cli_version"] = cli_version
    return info
