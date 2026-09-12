"""Modelled individual income distributions for every GB small area.

The problem, restated: HMRC knows individual incomes but only publishes them
down to parliamentary constituency (632 in GB). We need them at small-area
level (43,064). Nothing free bridges that gap, so it has to be modelled, and
the modelling should be legible enough to argue with.

## The distribution

HMRC Table 3.15 gives four facts per constituency: the median and mean of total
income, and the number of taxpayers paying at basic, higher and additional
rates. The rate counts are effectively counts above the tax thresholds, which
makes them counts above known income levels -- and that is what pins down the
top of the distribution.

A plain lognormal fitted to mean and median would badly understate the top end:
real income distributions have a Pareto tail, and £100k+ is exactly where that
matters. So the model is lognormal in the body and Pareto above the higher-rate
threshold:

    F(x)   = Phi((ln x - mu) / sigma)                     for x <= T
    S(x)   = S(T) * (x / T) ^ (-alpha)                    for x >  T

with T = the higher-rate threshold (£50,270). Three constraints give three
parameters in closed form, with no optimisation:

    mu     from the median          (the median sits below T everywhere)
    sigma  from P(income > T)       (higher-rate + additional-rate taxpayers)
    alpha  from P(income > £125,140) (additional-rate taxpayers)

The mean is deliberately *not* used to fit. It is held back to check the fit.

## Getting from constituency to small area

Within a constituency, areas are shifted by their affluence: an area's log
median income moves with how far its affluence score sits from the
constituency's own average. The size of that shift is calibrated on the
*between*-constituency relationship between affluence and income, then applied
*within* constituencies.

That is an ecological assumption and worth stating plainly: it presumes
affluence buys the same income premium inside a constituency as it does between
them. It is the central assumption of the whole model.

Spreading areas apart adds variance, so the within-area sigma is reduced to
compensate -- otherwise the constituency's modelled distribution would come out
wider than the one HMRC actually observed:

    sigma_within^2 = sigma_total^2 - beta^2 * Var(affluence)

## Non-taxpayers

HMRC sees taxpayers. Adults earning under the personal allowance are largely
invisible to it, so the bottom band would be badly undercounted if we stopped
there. Adults aged 16+ who are not taxpayers are added into the lowest band, so
the bands sum to the adult population rather than to the taxpayer population.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, RAW

HMRC_FILE = "hmrc_table_3_15_2324.ods"

# Thresholds for tax year 2023-24.
PERSONAL_ALLOWANCE = 12_570
HIGHER_RATE_THRESHOLD = 50_270
ADDITIONAL_RATE_THRESHOLD = 125_140

# Reporting bands. Chosen to sit on the tax thresholds that actually mean
# something -- the personal allowance, the higher rate, the point where the
# allowance starts tapering (£100k), and the additional rate.
BAND_EDGES = [0, PERSONAL_ALLOWANCE, 25_000, HIGHER_RATE_THRESHOLD, 75_000,
              100_000, ADDITIONAL_RATE_THRESHOLD, 200_000, np.inf]

BAND_LABELS = [
    "Under £12.6k", "£12.6k-25k", "£25k-50k", "£50k-75k",
    "£75k-100k", "£100k-125k", "£125k-200k", "£200k+",
]

# Keeps sigma from collapsing where affluence explains most of the variance.
MIN_SIGMA_FRACTION = 0.55


def load_hmrc() -> pd.DataFrame:
    """Parse HMRC Table 3.15 into one row per GB constituency."""
    raw = pd.read_excel(RAW / HMRC_FILE, engine="odf",
                        sheet_name="Table_3_15", header=4)
    raw = raw.rename(columns={
        raw.columns[0]: "pcon_code",
        raw.columns[1]: "pcon_name",
        "Total income: Number of individuals": "n_taxpayers",
        "Total income: Mean": "mean_income",
        "Total income: Median": "median_income",
        "Savers and Basic rate: Number of Individuals": "n_basic",
        "Higher rate: Number of Individuals": "n_higher",
        "Additional rate: Number of Individuals": "n_additional",
    })

    keep = ["pcon_code", "pcon_name", "n_taxpayers", "mean_income",
            "median_income", "n_basic", "n_higher", "n_additional"]
    df = raw[keep].copy()

    # The sheet mixes region subtotals in with constituencies.
    df = df[df["pcon_code"].astype(str).str.match(r"^(E14|W07|S14)")]

    for col in keep[2:]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Counts are published in thousands.
    for col in ("n_taxpayers", "n_basic", "n_higher", "n_additional"):
        df[col] = df[col] * 1_000

    return df.set_index("pcon_code")


def model_median(mu, sigma, alpha) -> np.ndarray:
    """Median of the spliced distribution, handling the case where it falls in
    the Pareto tail (the richest constituencies)."""
    T = HIGHER_RATE_THRESHOLD
    sT = 1 - stats.norm.cdf((np.log(T) - mu) / sigma)
    lognormal_median = np.exp(mu)
    pareto_median = T * (sT / 0.5) ** (1 / alpha)
    return np.where(sT < 0.5, lognormal_median, pareto_median)


def fit_constituencies(hmrc: pd.DataFrame) -> pd.DataFrame:
    """Least-squares fit of the spliced distribution, per constituency.

    An earlier version solved three of the four published facts in closed form
    (mu from the median, sigma from the higher-rate count, alpha from the
    additional-rate count) and held the mean back to check. That is elegant but
    numerically degenerate exactly where it matters most: when a constituency's
    median sits near the higher-rate threshold, `sigma = (ln T - mu) / z` has
    both numerator and denominator going to zero. Richmond Park, with a median
    of £50,300 against a £50,270 threshold, collapsed to the sigma floor and
    produced an absurdly narrow distribution feeding a very fat tail.

    Fitting all four facts at once is stable and uses more information. The
    residuals are log ratios, so each fact is matched in proportional terms:

        median, mean, P(income > £50,270), P(income > £125,140)

    The additional-rate residual is down-weighted where its count was imputed
    from a rounded zero, so those constituencies lean on the mean instead of on
    a number that was never really published.
    """
    from scipy import optimize

    out = hmrc.copy()

    # Counts are published rounded to the nearest thousand, so a blank or zero
    # additional-rate count means "fewer than 500", not "unknown" -- it happens
    # in 83 low-income constituencies. Treating it as missing would drop them;
    # treating it as exactly zero would make the Pareto tail undefined. Use the
    # midpoint of the interval the rounding leaves us, 250 people.
    n_additional = out["n_additional"].fillna(0.0)
    unresolved = n_additional < 500
    n_additional = n_additional.where(~unresolved, 250.0)
    out["n_additional_imputed"] = unresolved

    p_above_higher = ((out["n_higher"].fillna(0.0) + n_additional)
                      / out["n_taxpayers"]).clip(1e-5, 0.95)
    p_above_additional = (n_additional / out["n_taxpayers"]).clip(1e-6, None)
    # The additional-rate share must stay below the higher-rate share for the
    # tail to be decreasing; a handful of small constituencies round badly.
    p_above_additional = np.minimum(p_above_additional, p_above_higher * 0.9)

    obs_median = out["median_income"].to_numpy(dtype=float)
    obs_mean = out["mean_income"].to_numpy(dtype=float)
    p_hi = p_above_higher.to_numpy(dtype=float)
    p_add = np.asarray(p_above_additional, dtype=float)
    add_weight = np.where(unresolved.to_numpy(), 0.3, 1.0)

    mus, sigmas, alphas = [], [], []
    for i in range(len(out)):
        def residuals(theta, i=i):
            mu, sigma, alpha = theta
            mu, sigma, alpha = np.array([mu]), np.array([sigma]), np.array([alpha])
            return [
                2.0 * np.log(model_median(mu, sigma, alpha)[0] / obs_median[i]),
                1.0 * np.log(_model_mean(mu, sigma, alpha)[0] / obs_mean[i]),
                1.5 * np.log(survival(HIGHER_RATE_THRESHOLD, mu, sigma, alpha)[0] / p_hi[i]),
                add_weight[i] * np.log(
                    survival(ADDITIONAL_RATE_THRESHOLD, mu, sigma, alpha)[0] / p_add[i]),
            ]

        sol = optimize.least_squares(
            residuals,
            x0=[np.log(obs_median[i]), 0.5, 2.5],
            bounds=([np.log(5_000), 0.15, 1.05], [np.log(300_000), 1.6, 8.0]),
            xtol=1e-10, ftol=1e-10,
        )
        mus.append(sol.x[0]); sigmas.append(sol.x[1]); alphas.append(sol.x[2])

    out["mu"] = mus
    out["sigma"] = sigmas
    out["alpha"] = alphas
    out["p_above_higher"] = p_hi
    out["model_mean"] = _model_mean(out["mu"].to_numpy(), out["sigma"].to_numpy(),
                                    out["alpha"].to_numpy())
    out["model_median"] = model_median(out["mu"].to_numpy(), out["sigma"].to_numpy(),
                                       out["alpha"].to_numpy())
    return out


def _model_mean(mu, sigma, alpha) -> np.ndarray:
    """Mean of the spliced distribution, used only to validate the fit."""
    T = HIGHER_RATE_THRESHOLD
    zT = (np.log(T) - mu) / sigma
    # Body: E[X ; X <= T] for a lognormal.
    body = np.exp(mu + sigma ** 2 / 2) * stats.norm.cdf(zT - sigma)
    # Tail: S(T) * alpha/(alpha-1) * T, the Pareto conditional mean.
    sT = 1 - stats.norm.cdf(zT)
    tail = sT * (alpha / (alpha - 1)) * T
    return body + tail


def survival(x, mu, sigma, alpha) -> np.ndarray:
    """P(income > x) under the spliced distribution. Vectorised over areas."""
    x = np.asarray(x, dtype=float)
    T = HIGHER_RATE_THRESHOLD
    zT = (np.log(T) - mu) / sigma
    sT = 1 - stats.norm.cdf(zT)

    with np.errstate(divide="ignore", invalid="ignore"):
        body = 1 - stats.norm.cdf((np.log(np.maximum(x, 1e-9)) - mu) / sigma)
        tail = sT * (x / T) ** (-alpha)
    return np.where(x <= T, body, tail)


def band_shares(mu, sigma, alpha) -> np.ndarray:
    """Share of taxpayers in each reporting band. Shape (n_areas, n_bands)."""
    edges = BAND_EDGES
    surv = np.stack([
        np.ones_like(mu) if e == 0 else survival(np.full_like(mu, e), mu, sigma, alpha)
        for e in edges[:-1]
    ] + [np.zeros_like(mu)], axis=1)
    shares = surv[:, :-1] - surv[:, 1:]
    return np.clip(shares, 0, None)
