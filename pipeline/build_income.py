"""Push the constituency income model down to small areas and export it.

See income_model.py for the distribution itself. This module does the
geographic descent and the bookkeeping around it:

  1.  assign each small area to a constituency
  2.  calibrate how much affluence moves income, between constituencies
  3.  apply that shift within constituencies, correcting the variance
  4.  add non-taxpayers so the bands cover all adults, not just taxpayers
  5.  export band counts per area

Output:
  data/interim/gb_income.parquet
  web/data/gb_income.json
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import components as C
import income_model as M
from build_metrics import COMPONENTS
from config import INTERIM, RAW, WEB


def area_to_constituency() -> pd.DataFrame:
    """Assign each area its dominant constituency, by live postcode count.

    Small areas mostly nest inside constituencies, but not always -- boundary
    reviews do not respect them. The dominant assignment is reported so the
    scale of the approximation is visible rather than assumed away.
    """
    pcs = pd.read_parquet(INTERIM / "postcodes.parquet",
                          columns=["area_code", "pcon_code"])
    counts = pcs.groupby(["area_code", "pcon_code"]).size().rename("n").reset_index()
    total = counts.groupby("area_code")["n"].transform("sum")
    counts["share"] = counts["n"] / total

    dominant = counts.sort_values("n").groupby("area_code").tail(1).set_index("area_code")
    split = (dominant["share"] < 0.95).mean()
    print(f"  {len(dominant):,} areas assigned; "
          f"{split*100:.1f}% draw under 95% of their postcodes from one constituency")
    return dominant[["pcon_code", "share"]]


def adults_16plus() -> pd.Series:
    """Usual residents aged 16 and over, the denominator for the bands."""
    with zipfile.ZipFile(RAW / "census2021-ts062.zip") as z:
        ew = pd.read_csv(io.BytesIO(z.read("census2021-ts062-lsoa.csv")))
    total_col = next(c for c in ew.columns if "Total: All usual residents aged 16" in c)
    ew_s = pd.Series(ew[total_col].values, index=ew["geography code"])

    # Scotland: the same marginal total from the NS-SEC cross-tab.
    codes, levels, data = C._read_nrs_bulk("MV607")
    oa_total = pd.Series(data[0].values, index=codes)
    oa_total.index.name = "oa_code"
    pcs = (C._scotland_postcodes()[["oa_code", "area_code"]]
           .drop_duplicates("oa_code").set_index("oa_code"))
    sc_s = oa_total.to_frame("adults").join(pcs, how="inner").groupby("area_code")["adults"].sum()

    out = pd.concat([ew_s, sc_s])
    out.index.name = "area_code"
    return out.rename("adults")


def affluence_z(metrics: pd.DataFrame) -> pd.Series:
    """The composite affluence score, before it was ranked to a percentile.

    The percentile is right for display but wrong for modelling -- ranking
    destroys the distances between areas, and the distances are exactly what
    scales the income shift.
    """
    weights = {k: v[2] for k, v in COMPONENTS.items()}
    z = metrics[["z_" + k for k in COMPONENTS]].to_numpy(dtype=float)
    w = np.array([weights[k] for k in COMPONENTS])

    present = ~np.isnan(z)
    weight_present = (present * w).sum(axis=1)
    weighted = np.nansum(np.nan_to_num(z) * w, axis=1)
    score = np.where(weight_present >= 0.5 * w.sum(), weighted / weight_present, np.nan)
    return pd.Series(score, index=metrics.index, name="affluence_z")


def calibrate_slope(df: pd.DataFrame, fits: pd.DataFrame) -> float:
    """How much a unit of affluence moves log median income, between constituencies.

    This is the model's central assumption: the same slope is then applied
    *within* constituencies, where no income data exists to check it.
    """
    grouped = df.groupby("pcon_code").apply(
        lambda g: pd.Series({
            "z_mean": np.average(g["affluence_z"], weights=g["adults"]),
            "adults": g["adults"].sum(),
        }),
        include_groups=False,
    )
    joined = grouped.join(fits[["median_income", "n_taxpayers"]], how="inner").dropna()

    x = joined["z_mean"].to_numpy()
    y = np.log(joined["median_income"].to_numpy())
    w = joined["adults"].to_numpy()

    beta, intercept = np.polyfit(x, y, 1, w=np.sqrt(w))
    pred = beta * x + intercept
    r2 = 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    print(f"  slope beta = {beta:.4f} log-income per affluence sd   (R^2 = {r2:.3f}, n = {len(joined)})")
    print(f"  i.e. +1 sd of affluence => {(np.exp(beta)-1)*100:+.1f}% median income")
    return float(beta)


def main() -> None:
    t0 = time.time()

    print("constituency fits:")
    hmrc = M.load_hmrc()
    fits = M.fit_constituencies(hmrc)
    err = np.abs((fits.model_mean - fits.mean_income) / fits.mean_income)
    print(f"  {len(fits)} constituencies; median error vs held-back mean {err.median()*100:.1f}%")

    print("\ngeography:")
    metrics = pd.read_parquet(INTERIM / "gb_metrics.parquet")
    df = metrics[["area_name", "nation"]].copy()
    df = df.join(area_to_constituency())
    df["adults"] = adults_16plus()
    df["affluence_z"] = affluence_z(metrics)
    df = df.dropna(subset=["pcon_code", "adults", "affluence_z"])
    df = df[df["adults"] > 0]
    print(f"  {len(df):,} areas with adults, affluence and a constituency")

    print("\ncalibration:")
    beta = calibrate_slope(df, fits)

    # Within-constituency spread and variance correction.
    grp = df.groupby("pcon_code")
    z_mean = grp["affluence_z"].transform(lambda s: np.average(s, weights=df.loc[s.index, "adults"]))
    z_var = grp["affluence_z"].transform(
        lambda s: np.average((s - np.average(s, weights=df.loc[s.index, "adults"])) ** 2,
                             weights=df.loc[s.index, "adults"]))

    params = fits[["mu", "sigma", "alpha", "n_taxpayers"]].reindex(df["pcon_code"].values)
    mu_pcon = params["mu"].to_numpy()
    sigma_pcon = params["sigma"].to_numpy()
    alpha = params["alpha"].to_numpy()

    mu = mu_pcon + beta * (df["affluence_z"].to_numpy() - z_mean.to_numpy())

    # Spreading areas apart adds variance; remove it from within-area sigma so
    # the constituency mixture still matches the width HMRC observed.
    explained = (beta ** 2) * z_var.to_numpy()
    sigma_sq = np.maximum(sigma_pcon ** 2 - explained,
                          (M.MIN_SIGMA_FRACTION * sigma_pcon) ** 2)
    sigma = np.sqrt(sigma_sq)
    print(f"  sigma reduced from {sigma_pcon.mean():.3f} to {sigma.mean():.3f} on average")

    print("\nbands:")
    shares = M.band_shares(mu, sigma, alpha)

    # Taxpayer rate is taken from the constituency and applied uniformly to its
    # areas. Affluent areas almost certainly have a higher rate than that, so
    # the lowest band is a little overstated in rich areas and understated in
    # poor ones. It does not affect the upper bands, which is where the
    # question actually lies.
    pcon_adults = df.groupby("pcon_code")["adults"].transform("sum").to_numpy()
    taxpayer_rate = np.clip(params["n_taxpayers"].to_numpy() / pcon_adults, 0, 1)
    taxpayers = df["adults"].to_numpy() * taxpayer_rate
    non_taxpayers = df["adults"].to_numpy() - taxpayers

    counts = shares * taxpayers[:, None]
    counts[:, 0] += non_taxpayers  # everyone below the personal allowance

    band_cols = [f"band_{i}" for i in range(len(M.BAND_LABELS))]
    out = pd.DataFrame(counts, index=df.index, columns=band_cols)
    out.insert(0, "adults", df["adults"])
    out["pcon_code"] = df["pcon_code"]
    out["taxpayers"] = taxpayers
    out["median_income"] = np.exp(mu)
    out["pct_100k_plus"] = 100 * counts[:, 5:].sum(axis=1) / df["adults"].to_numpy()
    out["n_100k_plus"] = counts[:, 5:].sum(axis=1)

    total_adults = out["adults"].sum()
    print(f"  {len(out):,} areas, {total_adults/1e6:.1f}m adults")
    print("\nMODELLED GB DISTRIBUTION (all adults 16+)")
    for i, lbl in enumerate(M.BAND_LABELS):
        n = out[f"band_{i}"].sum()
        print(f"  {lbl:>14}  {100*n/total_adults:5.2f}%   {n/1e6:5.2f}m")
    print(f"\n  £100k+: {out['n_100k_plus'].sum()/1e6:.2f}m adults "
          f"({100*out['n_100k_plus'].sum()/total_adults:.2f}%)")

    out.to_parquet(INTERIM / "gb_income.parquet")
    write_json(out)
    print(f"\ndone in {time.time() - t0:.0f}s")


def write_json(out: pd.DataFrame) -> None:
    def clean(s, digits):
        return [None if pd.isna(v) else round(float(v), digits) for v in s]

    payload = {
        "band_labels": M.BAND_LABELS,
        "band_edges": [e if np.isfinite(e) else None for e in M.BAND_EDGES],
        "codes": out.index.tolist(),
        "values": {
            **{f"band_{i}": clean(out[f"band_{i}"], 0) for i in range(len(M.BAND_LABELS))},
            "adults": clean(out["adults"], 0),
            "median_income": clean(out["median_income"], 0),
            "pct_100k_plus": clean(out["pct_100k_plus"], 2),
            "n_100k_plus": clean(out["n_100k_plus"], 0),
        },
    }
    dest = WEB / "data" / "gb_income.json"
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
