"""Intervals and comparisons for the rates the paper reports.

One observation is one session. Sessions that share a perturbation (the same
bug, the same rename) are not independent, so every method here works on
clusters of sessions, one cluster per distinct perturbation, and never on
single sessions (design doc section 5.8, analysis plan section 2).

The primary intervals are t intervals from the cluster-robust variance of a
mean (`cluster_t_ci`, `paired_cluster_t_ci`), with the t quantile from
`t_quantile`. The cluster bootstrap (`cluster_bootstrap_ci`,
`paired_cluster_bootstrap_diff`) is the sensitivity analysis, and comparison 2
also gets a sign-flip permutation p-value (`sign_flip_p`).

The same data, seed and number of draws always give the same numbers, in any
row order. The draws come from NumPy's default generator, whose stream may
change between NumPy versions, so the version pinned in `uv.lock` is part of
what makes a reported interval reproducible.
"""

from __future__ import annotations

import math
import operator
import warnings
from collections.abc import Hashable, Sequence
from statistics import NormalDist

import numpy as np


def wilson_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for `k` successes in `n` independent trials.

    Returns (lo, hi) at confidence 1 - alpha. The trials must be independent.
    Sessions that share a perturbation are not; use `cluster_bootstrap_ci` for
    those.
    """
    k, n = operator.index(k), operator.index(n)
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    if not 0 <= k <= n:
        raise ValueError(f"k must be between 0 and n={n}, got {k}")
    _check_alpha(alpha)

    z = -NormalDist().inv_cdf(alpha / 2)
    centre = (k + z * z / 2) / (n + z * z)
    half = z * math.sqrt(k * (n - k) / n + z * z / 4) / (n + z * z)
    # The lower bound is exactly 0 at k = 0 and the upper bound exactly 1 at k = n. Rounding
    # in the formula can miss that by a hair (0.9999999999999999 for 5 of 5), so set them.
    lo = 0.0 if k == 0 else max(0.0, centre - half)
    hi = 1.0 if k == n else min(1.0, centre + half)
    return lo, hi


def cluster_bootstrap_ci(
    values: Sequence[float],
    clusters: Sequence[Hashable],
    n_boot: int = 10000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Mean of `values`, with a percentile interval from a cluster bootstrap.

    `clusters[i]` names the cluster of `values[i]`; equal labels are one
    cluster. Returns (estimate, lo, hi).

    The estimate is the mean over all observations. With G distinct clusters,
    one draw picks G clusters with replacement, each equally likely, and takes
    the mean over every observation in the picked clusters, counting a cluster
    as often as it was picked. `lo` and `hi` are the alpha/2 and 1 - alpha/2
    quantiles of `n_boot` such means, interpolated linearly between
    neighbouring values (NumPy's default).

    A zero-width interval means every draw gave the same mean. That happens
    when all clusters have the same rate (for example no successes at all),
    and always when there is only one cluster, in which case the function also
    warns. Zero width is then a limit of the method, not evidence of certainty.
    """
    return _bootstrap_mean(_as_values(values, "values"), "values", clusters, n_boot, alpha, seed)


def paired_cluster_bootstrap_diff(
    a: Sequence[float],
    b: Sequence[float],
    clusters: Sequence[Hashable],
    n_boot: int = 10000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float]:
    """mean(a) - mean(b), with a percentile interval from a cluster bootstrap.

    `a[i]` and `b[i]` are the two arms' outcomes for the same scenario and
    repetition, and `clusters[i]` is the cluster of both. Returns
    (difference, lo, hi).

    Each draw picks clusters once and uses them for both arms, so the interval
    reflects how the arms differ on the same perturbations. That is the same
    as bootstrapping the per-observation differences `a[i] - b[i]`, which is
    how it is computed. For 0/1 outcomes every difference of rates is then one
    division of whole numbers, rounded once, so a bound that sits exactly on a
    margin compares as equal to it. Subtracting two rounded rates does not
    guarantee that: 0.2 - 0.3 is -0.09999999999999998.
    """
    first, second = _as_values(a, "a"), _as_values(b, "b")
    if len(first) != len(second):
        raise ValueError(f"a and b differ in length ({len(first)} and {len(second)})")
    return _bootstrap_mean(first - second, "a", clusters, n_boot, alpha, seed)


def noninferior(diff_lo: float, margin: float = 0.10) -> bool:
    """Whether a lower confidence bound rules out a loss of `margin` or more.

    `diff_lo` is the lower bound of (new arm - reference arm) for an outcome
    where larger is better. It and `margin` are on the same scale: for a
    difference of proportions the default 0.10 is 10 percentage points (design
    doc section 5.8). A bound that equals the margin does not pass. With a
    two-sided interval at alpha = 0.05 this is a one-sided test at 0.025.
    """
    if math.isnan(diff_lo):
        raise ValueError("diff_lo is NaN, so there is no bound to compare")
    if not margin >= 0:
        raise ValueError(f"margin must be zero or positive, got {margin}")
    return bool(diff_lo > -margin)


def _check_alpha(alpha: float) -> None:
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be strictly between 0 and 1, got {alpha}")


def _as_values(values: Sequence[float], name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got {array.ndim} dimensions")
    if array.size == 0:
        raise ValueError(f"{name} is empty")
    if not np.isfinite(array).all():
        raise ValueError(f"{name} must be finite numbers, found NaN or infinity")
    return array


def _bootstrap_mean(
    values: np.ndarray,
    name: str,
    clusters: Sequence[Hashable],
    n_boot: int,
    alpha: float,
    seed: int,
) -> tuple[float, float, float]:
    """The cluster bootstrap of a mean that both public functions share."""
    observations = values.tolist()
    labels = list(clusters)
    if len(labels) != len(observations):
        raise ValueError(
            f"{name} and clusters differ in length ({len(observations)} and {len(labels)})"
        )
    n_boot = operator.index(n_boot)
    if n_boot < 1:
        raise ValueError(f"n_boot must be at least 1, got {n_boot}")
    _check_alpha(alpha)
    # An integer seed only: None would seed from the operating system and never repeat.
    rng = np.random.default_rng(operator.index(seed))

    by_cluster: dict[Hashable, list[float]] = {}
    for value, label in zip(observations, labels, strict=True):
        by_cluster.setdefault(label, []).append(value)
    if len(by_cluster) == 1:
        warnings.warn(
            "only one cluster: every bootstrap draw equals the estimate, so the interval "
            "has zero width and says nothing about uncertainty",
            RuntimeWarning,
            stacklevel=3,
        )

    # A draw's mean needs only each cluster's total and size. fsum rounds the true sum
    # once, so a total does not depend on the order of the rows.
    totals = np.array([math.fsum(group) for group in by_cluster.values()])
    sizes = np.array([len(group) for group in by_cluster.values()])
    # Number the clusters by content (size, then total) rather than by first appearance, so
    # the same data in another row order or under other cluster names gets the same draws.
    order = np.lexsort((totals, sizes))
    totals, sizes = totals[order], sizes[order]

    n_clusters = len(totals)
    picked = rng.integers(0, n_clusters, size=(n_boot, n_clusters))
    means = totals[picked].sum(axis=1) / sizes[picked].sum(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2], method="linear")
    return math.fsum(observations) / len(observations), float(lo), float(hi)


# --- Student's t --------------------------------------------------------------------------

# Below this the continued fraction's denominators are treated as zero (Lentz).
_TINY = 1e-300
# The continued fraction has converged when a step changes it by less than this.
_CONVERGED = 1e-15
_MAX_TERMS = 10_000


def t_quantile(p: float, df: float) -> float:
    """The `p` quantile of Student's t distribution with `df` degrees of freedom.

    The standard library has no t distribution and SciPy is not a dependency, so the
    quantile is found here by bisection on the distribution function. For t >= 0 the upper
    tail is P(T > t) = I_x(df/2, 1/2) / 2 with x = df / (df + t^2) (Abramowitz and Stegun
    26.7.1), where I_x(a, b) is the regularized incomplete beta function. I_x is evaluated
    from its continued fraction (Abramowitz and Stegun 26.5.8) by the modified Lentz method
    (Press et al., Numerical Recipes, section 6.4), on whichever side of the symmetry
    I_x(a, b) = 1 - I_(1-x)(b, a) the fraction converges fast. The bisection doubles an
    upper bound until the tail beyond it is no larger than the target, then halves the
    bracket until no float lies strictly inside it. The quantile is odd around p = 1/2.

    Accuracy: about 1e-13 relative where it matters (p = 0.975 and 1 to 200 degrees of
    freedom); worse only close to p = 1/2, where the tail is 1/2 minus a small number and the
    relative error reaches about 1e-9 (an absolute error of about 1e-13).
    """
    if not 0 < p < 1:
        raise ValueError(f"p must be strictly between 0 and 1, got {p}")
    if not (df > 0 and math.isfinite(df)):
        raise ValueError(f"df must be positive and finite, got {df}")
    p, df = float(p), float(df)
    if p == 0.5:
        return 0.0
    # The tail beyond |t|. For p above 1/2, 1 - p is exact (Sterbenz).
    target = 1.0 - p if p > 0.5 else p
    lo, hi = 0.0, 1.0
    while _t_upper_tail(hi, df) > target:
        lo, hi = hi, 2.0 * hi
    while True:
        mid = (lo + hi) / 2.0
        if not lo < mid < hi:
            break
        if _t_upper_tail(mid, df) > target:
            lo = mid
        else:
            hi = mid
    return hi if p > 0.5 else -hi


def _t_upper_tail(t: float, df: float) -> float:
    """P(T > t) for t >= 0 (Abramowitz and Stegun 26.7.1)."""
    square = t * t
    if math.isinf(square):
        return 0.0
    total = df + square
    return 0.5 * _betainc(df / 2.0, 0.5, df / total, square / total)


def _betainc(a: float, b: float, x: float, y: float) -> float:
    """The regularized incomplete beta function I_x(a, b), given x and y = 1 - x.

    The caller passes y so that it keeps its precision when x is close to 1.
    """
    if x <= 0.0:
        return 0.0
    if y <= 0.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(y)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_fraction(a, b, x) / a
    return 1.0 - front * _beta_fraction(b, a, y) / b


def _beta_fraction(a: float, b: float, x: float) -> float:
    """The continued fraction of I_x(a, b) (A&S 26.5.8), by the modified Lentz method."""
    a_plus_b, a_plus_1, a_minus_1 = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - a_plus_b * x / a_plus_1
    d = 1.0 / (d if abs(d) > _TINY else _TINY)
    fraction = d
    for m in range(1, _MAX_TERMS):
        two_m = 2 * m
        # the even term, then the odd one
        for numerator in (
            m * (b - m) * x / ((a_minus_1 + two_m) * (a + two_m)),
            -(a + m) * (a_plus_b + m) * x / ((a + two_m) * (a_plus_1 + two_m)),
        ):
            d = 1.0 + numerator * d
            d = 1.0 / (d if abs(d) > _TINY else _TINY)
            c = 1.0 + numerator / c
            c = c if abs(c) > _TINY else _TINY
            step = d * c
            fraction *= step
        if abs(step - 1.0) < _CONVERGED:
            return fraction
    raise ArithmeticError(f"the continued fraction of I_x({a}, {b}) did not converge")


# --- Cluster t interval and sign-flip test ------------------------------------------------


def cluster_t_ci(
    values: Sequence[float], clusters: Sequence[Hashable], alpha: float = 0.05
) -> tuple[float, float, float]:
    """Mean of `values`, with a t interval from the cluster-robust variance of a mean.

    `clusters[i]` names the cluster of `values[i]`. Returns (estimate, lo, hi). The estimate
    p is the mean over all n observations. With G clusters, cluster c holding n_c of them
    with total t_c, the variance of p is G/(G-1) * sum_c (t_c - p * n_c)^2 / n^2, and the
    interval is p plus and minus the 1 - alpha/2 quantile of t with G - 1 degrees of freedom
    times its square root (analysis plan section 2). When every value is 0 or 1 the bounds
    are clipped to [0, 1]. Needs at least two clusters.

    A zero-width interval means every cluster has the same rate, for instance no successes
    at all; the analysis plan then reports a Wilson bound over clusters instead.
    """
    observations = _as_values(values, "values")
    estimate, lo, hi = _cluster_t(observations, "values", clusters, alpha)
    if np.isin(observations, (0.0, 1.0)).all():
        lo, hi = max(0.0, lo), min(1.0, hi)
    return estimate, lo, hi


def paired_cluster_t_ci(
    a: Sequence[float],
    b: Sequence[float],
    clusters: Sequence[Hashable],
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """mean(a) - mean(b), with the interval of `cluster_t_ci` on the differences a[i] - b[i].

    `a[i]` and `b[i]` are the two arms' outcomes for the same scenario and repetition, and
    `clusters[i]` is the cluster of both. Returns (difference, lo, hi), never clipped.
    """
    return _cluster_t(_paired_differences(a, b), "a", clusters, alpha)


def sign_flip_p(
    a: Sequence[float],
    b: Sequence[float],
    clusters: Sequence[Hashable],
    n_perm: int = 100_000,
    seed: int = 20261114,
) -> float:
    """Two-sided sign-flip permutation p-value of mean(a) - mean(b), paired by cluster.

    The statistic is the overall paired mean difference, the sum of a[i] - b[i] over the n
    observations divided by n. Under the null hypothesis each cluster's total difference is
    as likely to have either sign, so the reference distribution flips the sign of whole
    cluster totals. The p-value is the share of sign patterns whose statistic is at least as
    large in size as the observed one (with a tolerance of 1e-9 times the sum of the totals'
    sizes, so that ties broken by rounding still count).

    With G clusters, all 2^G patterns are listed when G <= 20 and the p-value is exact.
    Otherwise `n_perm` patterns are drawn from `numpy.random.default_rng(seed)` and the
    p-value is (1 + patterns at least as extreme) / (1 + n_perm), which counts the observed
    pattern as one of the draws and so is never 0 (Phipson and Smyth 2010). The clusters are
    put in an order fixed by their content first, so the draws do not depend on row order or
    cluster names.
    """
    differences = _paired_differences(a, b)
    n_perm, seed = operator.index(n_perm), operator.index(seed)
    if n_perm < 1:
        raise ValueError(f"n_perm must be at least 1, got {n_perm}")
    totals = np.array(_cluster_totals(differences, "a", clusters)[0])
    observed = abs(math.fsum(totals.tolist()))
    threshold = observed - 1e-9 * math.fsum(np.abs(totals).tolist())
    if len(totals) <= 20:
        sums = np.zeros(1)
        for total in totals:
            sums = np.concatenate((sums + total, sums - total))
        return float(np.count_nonzero(np.abs(sums) >= threshold) / sums.size)
    flips = np.random.default_rng(seed).integers(0, 2, size=(n_perm, len(totals)), dtype=np.int8)
    whole = totals.sum()
    extreme = 0
    for start in range(0, n_perm, 10_000):
        # flipping the clusters marked 1 takes twice their total off the sum
        sums = whole - 2.0 * (flips[start : start + 10_000].astype(float) @ totals)
        extreme += int(np.count_nonzero(np.abs(sums) >= threshold))
    return (1 + extreme) / (1 + n_perm)


def _paired_differences(a: Sequence[float], b: Sequence[float]) -> np.ndarray:
    first, second = _as_values(a, "a"), _as_values(b, "b")
    if len(first) != len(second):
        raise ValueError(f"a and b differ in length ({len(first)} and {len(second)})")
    return first - second


def _cluster_totals(
    values: np.ndarray, name: str, clusters: Sequence[Hashable]
) -> tuple[list[float], list[int]]:
    """Each cluster's total and size, in an order fixed by the data (size, then total)."""
    observations = values.tolist()
    labels = list(clusters)
    if len(labels) != len(observations):
        raise ValueError(
            f"{name} and clusters differ in length ({len(observations)} and {len(labels)})"
        )
    by_cluster: dict[Hashable, list[float]] = {}
    for value, label in zip(observations, labels, strict=True):
        by_cluster.setdefault(label, []).append(value)
    # fsum rounds each total once, so it does not depend on the order of the rows
    ordered = sorted((len(group), math.fsum(group)) for group in by_cluster.values())
    return [total for _size, total in ordered], [size for size, _total in ordered]


def _cluster_t(
    values: np.ndarray, name: str, clusters: Sequence[Hashable], alpha: float
) -> tuple[float, float, float]:
    """The cluster t interval of a mean that both public functions share."""
    totals, sizes = _cluster_totals(values, name, clusters)
    _check_alpha(alpha)
    g = len(totals)
    if g < 2:
        raise ValueError(f"a cluster t interval needs at least two clusters, got {g}")
    n = len(values)
    estimate = math.fsum(values.tolist()) / n
    spread = math.fsum(
        (total - estimate * size) ** 2 for total, size in zip(totals, sizes, strict=True)
    )
    standard_error = math.sqrt(g / (g - 1) * spread / (n * n))
    half = t_quantile(1 - alpha / 2, g - 1) * standard_error
    return estimate, estimate - half, estimate + half
