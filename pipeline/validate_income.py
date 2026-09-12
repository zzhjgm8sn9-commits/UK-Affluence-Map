"""Independent validation of the modelled small-area incomes.

The income model is built from HMRC constituency data plus the affluence index.
ONS publishes its own model-based income estimates at MSOA level, built from a
completely different source (the Family Resources Survey, small-area estimation
against census and administrative covariates). Nothing in our pipeline touches
it, so it is a genuine out-of-sample check.

The two are *not* the same quantity -- ONS estimates mean **household** income,
we estimate the distribution of **individual** income -- so the levels are not
comparable and only the spatial pattern is. What a high rank correlation would
show is that the two models agree about which neighbourhoods are richer than
which, which is the thing the map is used for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))

from config import INTERIM, RAW

ONS_FILE = "ons_msoa_income_fye2023.xlsx"


def load_ons(sheet: str) -> pd.DataFrame:
    """ONS sheets carry several rows of preamble above the real header."""
    raw = pd.read_excel(RAW / ONS_FILE, sheet_name=sheet, header=None)
    # The preamble lines mention MSOAs in prose, so match the header row on its
    # first cell being the column name itself.
    header_row = next(i for i in range(len(raw))
                      if str(raw.iloc[i, 0]).strip().lower() == "msoa code")
    df = pd.read_excel(RAW / ONS_FILE, sheet_name=sheet, header=header_row)

    code_col = next(c for c in df.columns if str(c).strip().lower() == "msoa code")
    income_col = next(c for c in df.columns
                      if "income" in str(c).lower()
                      and not any(t in str(c).lower()
                                  for t in ("upper", "lower", "confidence", "limit")))
    out = df[[code_col, income_col]].copy()
    out.columns = ["msoa_code", "ons_income"]
    out["ons_income"] = pd.to_numeric(out["ons_income"], errors="coerce")
    return out.dropna().set_index("msoa_code")


def main() -> None:
    income = pd.read_parquet(INTERIM / "gb_income.parquet")
    metrics = pd.read_parquet(INTERIM / "gb_metrics.parquet")

    lookup = (pd.read_parquet(INTERIM / "postcodes.parquet",
                              columns=["area_code", "msoa_code"])
              .drop_duplicates("area_code").set_index("area_code"))

    df = income.join(lookup).join(metrics[["nation", "affluence_index"]])
    df = df[df["nation"] != "Scotland"].dropna(subset=["msoa_code"])

    # Roll the modelled areas up to MSOA, weighting by adults.
    df["w_income"] = df["median_income"] * df["adults"]
    df["w_100k"] = df["n_100k_plus"]
    grouped = df.groupby("msoa_code").agg(
        adults=("adults", "sum"),
        w_income=("w_income", "sum"),
        n_100k=("w_100k", "sum"),
    )
    grouped["model_income"] = grouped["w_income"] / grouped["adults"]
    grouped["model_pct_100k"] = 100 * grouped["n_100k"] / grouped["adults"]

    print("Comparing modelled individual income against ONS household income\n")
    for sheet in ("Total annual income", "Net income before housing costs"):
        try:
            ons = load_ons(sheet)
        except (StopIteration, ValueError) as err:
            print(f"  {sheet}: could not parse ({err})")
            continue

        merged = grouped.join(ons, how="inner").dropna(
            subset=["model_income", "ons_income"])
        if merged.empty:
            print(f"  {sheet}: no overlap on MSOA code")
            continue

        r_p = stats.pearsonr(np.log(merged["model_income"]), np.log(merged["ons_income"]))[0]
        r_s = stats.spearmanr(merged["model_income"], merged["ons_income"])[0]
        r_100 = stats.spearmanr(merged["model_pct_100k"], merged["ons_income"])[0]

        print(f"  {sheet}  (n = {len(merged):,} MSOAs)")
        print(f"    Spearman, modelled median income vs ONS : {r_s: .3f}")
        print(f"    Pearson on logs                          : {r_p: .3f}")
        print(f"    Spearman, modelled % on £100k+ vs ONS    : {r_100: .3f}")

        deciles = pd.qcut(merged["ons_income"], 10, labels=False, duplicates="drop")
        summary = merged.groupby(deciles).agg(
            ons=("ons_income", "median"),
            modelled=("model_income", "median"),
            pct_100k=("model_pct_100k", "median"))
        print("\n    ONS decile   ONS household   modelled individual   modelled %100k+")
        for d, row in summary.iterrows():
            print(f"      {int(d)+1:>4}       £{row.ons:>9,.0f}      £{row.modelled:>10,.0f}"
                  f"        {row.pct_100k:>6.2f}%")
        print()


if __name__ == "__main__":
    main()
