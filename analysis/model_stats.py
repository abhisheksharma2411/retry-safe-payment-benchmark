"""Statistics helpers for the model-evaluation analysis.

The one methodological decision that matters here is the **resampling unit**.

A record is one (candidate, schedule) pair, and schedules are nested inside
candidates: 10 schedules per candidate, 3 samples per family, 7 families. Records
are therefore very far from independent — a candidate that gets the recovery
logic wrong fails several schedules at once, for one underlying reason. Bootstrapping
over records would treat those as independent evidence and produce intervals that
are far too narrow.

So every interval in this analysis resamples **semantic task families** (n=7)
with replacement, and recomputes the metric by pooling all records of the drawn
families. That treats "which task families we happened to write" as the source of
sampling error, which is the generalisation the paper actually wants to claim.

Consequences worth stating plainly:

  * n=7 is small. The intervals are wide, and they should be.
  * A metric at a boundary (every family perfect) has a degenerate bootstrap:
    every resample gives 1.0, so the interval is [1.0, 1.0]. That is an artifact
    of the estimator at the boundary, not evidence of precision, and
    `saturated` marks those cells.
"""

import math

import numpy as np

# Fixed so the intervals are reproducible; the analysis is a paper artifact.
SEED = 20260813
BOOTSTRAP = 10000
ALPHA = (2.5, 97.5)


def ratio(cells, num, den):
    """Pooled ratio of two counters across a collection of per-family stats."""
    a = sum(c[num] for c in cells)
    b = sum(c[den] for c in cells)
    return a / b if b else float("nan")


def bootstrap_indices(n_families, rng, replicates=BOOTSTRAP):
    """One resample matrix reused across metrics so deltas stay paired.

    A delta between two conditions must be computed on the *same* resampled
    families in each replicate, or the two marginals are drawn independently and
    the interval on their difference is wrong.
    """
    return rng.integers(0, n_families, size=(replicates, n_families))


def ci_from_counts(nums, dens, idx):
    """Percentile CI for a pooled ratio, given per-family numerators/denominators."""
    nums = np.asarray(nums, dtype=float)
    dens = np.asarray(dens, dtype=float)
    if dens.sum() == 0:
        return (float("nan"), float("nan"), float("nan"))
    a = nums[idx].sum(axis=1)
    b = dens[idx].sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = np.where(b > 0, a / np.where(b > 0, b, 1), np.nan)
    vals = vals[~np.isnan(vals)]
    if vals.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    point = nums.sum() / dens.sum()
    lo, hi = np.percentile(vals, ALPHA)
    return (point, float(lo), float(hi))


def ci_from_delta(nums_a, dens_a, nums_b, dens_b, idx):
    """Percentile CI for (ratio_a - ratio_b), paired on the same resamples."""
    nums_a, dens_a = np.asarray(nums_a, float), np.asarray(dens_a, float)
    nums_b, dens_b = np.asarray(nums_b, float), np.asarray(dens_b, float)
    if dens_a.sum() == 0 or dens_b.sum() == 0:
        return (float("nan"), float("nan"), float("nan"))
    aa, ab = nums_a[idx].sum(axis=1), dens_a[idx].sum(axis=1)
    ba, bb = nums_b[idx].sum(axis=1), dens_b[idx].sum(axis=1)
    ok = (ab > 0) & (bb > 0)
    vals = aa[ok] / ab[ok] - ba[ok] / bb[ok]
    if vals.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    point = nums_a.sum() / dens_a.sum() - nums_b.sum() / dens_b.sum()
    lo, hi = np.percentile(vals, ALPHA)
    return (float(point), float(lo), float(hi))


def survival_rk(m, n, k):
    """R_k = C(m,k)/C(n,k): the probability that a candidate which passed m of n
    hidden schedules survives k of them drawn without replacement.

    Hypergeometric, so it does not assume schedules are independent — which they
    are not (review issue T4)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    den = math.comb(n, k)
    if den == 0:
        return 0.0
    return math.comb(m, k) / den


_DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def tex_name(s):
    """A LaTeX-safe control-sequence suffix: letters only.

    Digits must be spelled out. A TeX control sequence is a run of catcode-11
    letters, so `\\failcompm1unstableidentity` does NOT define a macro of that
    name — it defines `\\failcompm` with `1unstableidentity` as delimiter text,
    and five such lines silently collide onto one macro. Mapping 1 -> "one"
    keeps every name a real, distinct control sequence.
    """
    out = []
    for ch in str(s):
        if ch.isalpha():
            out.append(ch)
        elif ch in _DIGITS:
            out.append(_DIGITS[ch])
    return "".join(out)
