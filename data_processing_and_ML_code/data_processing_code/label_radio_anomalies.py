"""
Post-hoc anomaly labeling for the radio-KPI extension's radio_kpi.csv
(see cellular-network-functions.h's RadioKpiSample()). Congestion in that
scenario is radio-emergent (real UE contention at a configurable density,
no scripted faults -- see cellular-network.h's Validate()), so there are no
ground-truth fault labels to reuse; this script derives them from the KPIs
themselves, purely in Python, with no C++/re-simulation involved.

Primary triggers: queue_bytes and mac_retries -- the actual congestion
signals (DL RLC TX-queue occupancy, DL HARQ NACK count). snr_db is
corroborating only: a coverage dip alone is not flagged as a breach, but if
one coincides with a queue_bytes/mac_retries breach it's recorded alongside
it. mos_* is never a trigger -- it's a synthetic QoE proxy computed FROM the
RTT/jitter/loss columns (see ComputeMos() in cellular-network-functions.h),
so using it as a trigger would be circular w.r.t. its own inputs.

Usage:
    python label_radio_anomalies.py radio_kpi.csv [-o radio_breaches.csv]
"""

import argparse
import numpy as np
import pandas as pd

# Primary (congestion) trigger columns. mos_* is intentionally absent.
TRIGGER_COLUMNS = ["queue_bytes", "mac_retries"]
CORROBORATING_COLUMN = "snr_db"

# Modified z-score constant (Iglewicz & Hoaglin), used with the MAD baseline.
MAD_SCALE = 0.6745


def _rolling_baseline(series, window, method):
    """
    Trailing (causal) rolling baseline for one UE's one column, using only
    past samples (shift(1) before rolling) so a breach never contaminates
    its own baseline.

    Returns (center, spread) series:
      - method="mad":  center = rolling median, spread = rolling MAD
      - method="p90":  center = rolling 90th percentile, spread = NaN (unused)
    """
    past = series.shift(1)
    roll = past.rolling (window, min_periods=max (3, window // 3))
    if method == "mad":
        center = roll.median ()
        # MAD of a rolling window: median absolute deviation from that
        # window's own median. pandas has no builtin, so apply() it.
        spread = past.rolling (window, min_periods=max (3, window // 3)).apply (
            lambda w: np.nanmedian (np.abs (w - np.nanmedian (w))), raw=True
        )
        return center, spread
    elif method == "p90":
        center = roll.quantile (0.9)
        return center, None
    else:
        raise ValueError ("baseline method must be 'mad' or 'p90'")


def _flag_high (series, window, method, k, floor):
    """Per-tick boolean: series value is anomalously HIGH vs its trailing baseline."""
    center, spread = _rolling_baseline (series, window, method)
    if method == "mad":
        # Modified z-score; MAD_SCALE*MAD acts as the spread estimator. A
        # `floor` keeps a near-constant-zero baseline (e.g. an idle queue)
        # from producing a zero spread and flagging every nonzero sample.
        denom = (MAD_SCALE * spread).clip (lower=floor)
        z = (series - center) / denom
        return (z > k).fillna (False)
    else:  # p90
        threshold = center.clip (lower=floor) * k
        return (series > threshold).fillna (False)


def _flag_low (series, window, method, k, floor):
    """Per-tick boolean: series value is anomalously LOW vs its trailing baseline (for snr_db)."""
    center, spread = _rolling_baseline (series, window, method)
    if method == "mad":
        denom = (MAD_SCALE * spread).clip (lower=floor)
        z = (center - series) / denom
        return (z > k).fillna (False)
    else:  # p90
        # Low-side p90 baseline isn't meaningful the same way; use the 10th
        # percentile of the trailing window as the low-side reference instead.
        past = series.shift (1)
        p10 = past.rolling (window, min_periods=max (3, window // 3)).quantile (0.1)
        threshold = p10 / k
        return (series < threshold).fillna (False)


def _debounce_and_merge (flags, min_consecutive):
    """
    flags: boolean array, True where a tick is a breach candidate.
    Returns a list of (start_idx, end_idx) INCLUSIVE index spans where at
    least min_consecutive consecutive ticks were flagged -- single-tick
    blips are dropped, matching the plan's debounce requirement.
    """
    spans = []
    n = len (flags)
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        while j < n and flags[j]:
            j += 1
        # candidate run is [i, j)
        if (j - i) >= min_consecutive:
            spans.append ((i, j - 1))
        i = j
    return spans


def label_ue (df_ue, window, method, k, floor, min_consecutive):
    """df_ue: one UE's rows, already sorted by _time. Returns list of breach dicts."""
    df_ue = df_ue.reset_index (drop=True)

    high_flags = {}
    for col in TRIGGER_COLUMNS:
        high_flags[col] = _flag_high (df_ue[col], window, method, k, floor).to_numpy ()

    combined = np.zeros (len (df_ue), dtype=bool)
    for col in TRIGGER_COLUMNS:
        combined |= high_flags[col]

    snr_low = None
    if CORROBORATING_COLUMN in df_ue.columns:
        snr_low = _flag_low (
            df_ue[CORROBORATING_COLUMN], window, method, k, floor=1e-6
        ).to_numpy ()

    breaches = []
    for start_idx, end_idx in _debounce_and_merge (combined, min_consecutive):
        span = slice (start_idx, end_idx + 1)
        firing = [col for col in TRIGGER_COLUMNS if high_flags[col][span].any ()]
        breach = {
            "ueId": int (df_ue["ueId"].iloc[start_idx]),
            "start_s": df_ue["_time"].iloc[start_idx] / 1e6,
            "end_s": df_ue["_time"].iloc[end_idx] / 1e6,
            "trigger_signal": "+".join (firing),
            "peak_queue_bytes": float (df_ue["queue_bytes"].iloc[span].max ()),
            "peak_mac_retries": float (df_ue["mac_retries"].iloc[span].max ()),
            "snr_corroborated": bool (snr_low[span].any ()) if snr_low is not None else False,
        }
        breaches.append (breach)
    return breaches


def label_radio_anomalies (df, window=30, method="mad", k=3.5, floor=1.0, min_consecutive=2):
    """
    df: radio_kpi.csv loaded as a DataFrame (tab-separated).
    Returns a DataFrame of breach spans, shaped like qos_multiue.cc's
    _faults.csv (start_s, end_s, target/trigger, ...) even though these
    spans are derived post-hoc rather than scripted by the simulator.
    """
    df = df.sort_values (["ueId", "_time"])
    all_breaches = []
    for ue_id, df_ue in df.groupby ("ueId"):
        all_breaches.extend (
            label_ue (df_ue, window, method, k, floor, min_consecutive)
        )
    columns = [
        "ueId", "start_s", "end_s", "trigger_signal",
        "peak_queue_bytes", "peak_mac_retries", "snr_corroborated",
    ]
    if not all_breaches:
        return pd.DataFrame (columns=columns)
    out = pd.DataFrame (all_breaches, columns=columns)
    return out.sort_values (["start_s", "ueId"]).reset_index (drop=True)


def main ():
    parser = argparse.ArgumentParser (description=__doc__,
                                       formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument ("radio_kpi_csv", help="Path to radio_kpi.csv")
    parser.add_argument ("-o", "--output", default=None,
                          help="Output path (default: radio_breaches.csv next to the input)")
    parser.add_argument ("--window", type=int, default=30,
                          help="Trailing rolling-baseline window, in samples (default: 30)")
    parser.add_argument ("--baseline", choices=["mad", "p90"], default="mad",
                          help="Rolling baseline method (default: mad)")
    parser.add_argument ("--k", type=float, default=3.5,
                          help="Threshold multiplier: modified z-score cutoff for "
                               "--baseline=mad, or baseline multiplier for --baseline=p90 "
                               "(default: 3.5)")
    parser.add_argument ("--floor", type=float, default=1.0,
                          help="Minimum baseline spread/value, so a near-zero baseline "
                               "(e.g. an idle queue) doesn't flag every nonzero sample "
                               "(default: 1.0)")
    parser.add_argument ("--min-consecutive", type=int, default=2,
                          help="Debounce: minimum consecutive flagged ticks to count as "
                               "a breach, drops single-tick blips (default: 2)")
    args = parser.parse_args ()

    df = pd.read_csv (args.radio_kpi_csv, sep="\t")
    breaches = label_radio_anomalies (
        df, window=args.window, method=args.baseline, k=args.k,
        floor=args.floor, min_consecutive=args.min_consecutive,
    )

    out_path = args.output
    if out_path is None:
        out_path = args.radio_kpi_csv.rsplit ("/", 1)[0] + "/radio_breaches.csv" \
            if "/" in args.radio_kpi_csv else "radio_breaches.csv"
    breaches.to_csv (out_path, index=False)
    print (f"{len (breaches)} breach spans across {df['ueId'].nunique ()} UEs -> {out_path}")


if __name__ == "__main__":
    main ()
