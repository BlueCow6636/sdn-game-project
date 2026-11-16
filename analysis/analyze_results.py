#!/usr/bin/env python3
"""
Draws the comparison graphs from the experiment results.

INPUT  (written by experiments/run_experiments.py)
  results/summary.csv                          mean and standard deviation over the runs
  results/raw/<condition>_run<k>_packets.csv   every received game packet
  results/raw/<condition>_run<k>_controller.csv  the controller's decisions (dynamic SDN)

OUTPUT (results/graphs/)
  avg_latency.png            average and median one-way latency, per condition
  jitter.png                 average jitter, per condition
  packet_loss.png            packet loss, per condition
  throughput.png             game traffic throughput, per condition
  latency_over_time.png      latency of every packet over time, one panel per condition
  latency_over_time_medians.png   the same data as 1-second medians on one chart
  controller_decisions.png   what the dynamic controller measured and decided
  overview.png               loss, latency, jitter and throughput in one figure

The bar charts show the flood window (10-20 s). In the "Normal" condition there is
no flood, but the same window is measured so the bars are comparable.
Error bars are the standard deviation over the runs.

Usage:  python3 analysis/analyze_results.py
"""
import argparse
import csv
import os
import statistics
import sys

import matplotlib
matplotlib.use("Agg")                  # draw to files, no window needed
import matplotlib.pyplot as plt

# (condition name, label for the chart, colour)
CONDITIONS = [
    ("normal", "Normal\n(no flood)", "#7f7f7f"),
    ("congested_no_sdn", "Congested\nno SDN", "#d62728"),
    ("congested_sdn_static", "Congested\nstatic SDN", "#1f77b4"),
    ("congested_sdn_dynamic", "Congested\ndynamic SDN", "#2ca02c"),
]
DPI = 150


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def load_summary(path):
    data = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            data[(row["condition"], row["phase"])] = row
    return data


def load_run(results, cond, run, rate):
    """Returns (t0, [(seconds since players started, latency_ms), ...]) or None.

    A packet's SEND time is (receive time - latency). The players start sending at
    t0, so a packet with sequence number n was sent about (n-1)/rate seconds after t0.
    """
    path = os.path.join(results, "raw", f"{cond}_run{run}_packets.csv")
    if not os.path.exists(path):
        return None
    per_player = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            lat = float(row["latency_ms"])
            send = float(row["timestamp"]) - lat / 1000.0
            per_player.setdefault(row["player"], []).append((int(row["sequence"]), send, lat))
    if not per_player:
        return None
    starts = []
    for rows in per_player.values():
        offsets = sorted(s - (q - 1) / rate for q, s, _ in rows)
        starts.append(offsets[len(offsets) // 2])        # median is robust
    first_send = min(send for rows in per_player.values() for _, send, _ in rows)
    t0 = min(min(starts), first_send)         # so that no packet falls before time 0
    pts = sorted((s - t0, lat) for rows in per_player.values() for _, s, lat in rows)
    return t0, pts


def load_events(results, cond, run, t0):
    """Controller log rows for this run: [(seconds, tx_mbps, priority_on, event)]."""
    path = os.path.join(results, "raw", f"{cond}_run{run}_controller.csv")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                out.append((float(row["timestamp"]) - t0, float(row["tx_mbps"]),
                            int(row["priority_on"]), row["event"]))
            except (ValueError, KeyError):
                pass
    return out


def binned_median(pts, width=1.0):
    bins = {}
    for t, lat in pts:
        bins.setdefault(int(t // width), []).append(lat)
    keys = sorted(bins)
    return [(k + 0.5) * width for k in keys], [statistics.median(bins[k]) for k in keys]


# ----------------------------------------------------------------------------
# Bar charts
# ----------------------------------------------------------------------------
def bar_panel(ax, data, phase, metrics, ylabel, title, ymax=None):
    """metrics: list of (column prefix, legend label). One metric -> bars coloured
    by condition; several metrics -> bars coloured by metric, with a legend."""
    present = [(k, label, color) for k, label, color in CONDITIONS if (k, phase) in data]
    if not present:
        ax.text(0.5, 0.5, "no data", ha="center", transform=ax.transAxes)
        return
    n = len(metrics)
    width = 0.8 / n
    palette = ["#4c72b0", "#dd8452", "#55a868"]
    top = 0.0
    for j, (col, mlabel) in enumerate(metrics):
        xs, means, stds, colors = [], [], [], []
        for i, (key, _, color) in enumerate(present):
            row = data[(key, phase)]
            xs.append(i - 0.4 + width * (j + 0.5))
            means.append(float(row[col + "_mean"]))
            stds.append(float(row[col + "_std"]))
            colors.append(color if n == 1 else palette[j])
        ax.bar(xs, means, width * 0.95, yerr=stds, capsize=4, color=colors,
               edgecolor="black", linewidth=0.8, label=mlabel if n > 1 else None)
        top = max([top] + [m + sd for m, sd in zip(means, stds)])
        for x, m, s in zip(xs, means, stds):
            ax.annotate(f"{m:.1f}", (x, m + s), xytext=(0, 3), textcoords="offset points",
                        ha="center", va="bottom", fontsize=8)
    runs = max(int(data[(k, phase)]["runs"]) for k, _, _ in present)
    ax.set_xticks(range(len(present)))
    ax.set_xticklabels([label for _, label, _ in present], fontsize=9)
    ax.set_ylabel(ylabel)
    ax.set_title(f"{title}\n(10-20 s window, mean ± std of {runs} runs)", fontsize=10)
    ax.set_ylim(0, ymax if ymax else max(top * 1.15, 1e-9))     # headroom for the value labels
    ax.grid(axis="y", alpha=0.3)
    if n > 1:
        ax.legend()


def save_bar(out, name, data, metrics, ylabel, title, ymax=None):
    fig, ax = plt.subplots(figsize=(7, 4.5))
    bar_panel(ax, data, "during", metrics, ylabel, title, ymax)
    fig.tight_layout()
    path = os.path.join(out, name)
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def save_overview(out, data):
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    bar_panel(axes[0, 0], data, "during", [("loss_pct", "Loss")], "Packet loss (%)",
              "Packet loss", ymax=100)
    bar_panel(axes[0, 1], data, "during", [("lat_median_ms", "Median"), ("lat_avg_ms", "Average")],
              "One-way latency (ms)", "Latency")
    bar_panel(axes[1, 0], data, "during", [("jitter_avg_ms", "Jitter")], "Jitter (ms)", "Jitter")
    bar_panel(axes[1, 1], data, "during", [("throughput_kbps", "Throughput")],
              "Game throughput (kbps)", "Game traffic throughput")
    fig.tight_layout()
    path = os.path.join(out, "overview.png")
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


# ----------------------------------------------------------------------------
# Latency over time
# ----------------------------------------------------------------------------
def shade_flood(ax, a):
    ax.axvspan(a.flood_start, a.flood_start + a.flood_duration, color="#ffcc66",
               alpha=0.25, label="flood window")


def save_over_time(out, results, a):
    runs = []
    for key, label, color in CONDITIONS:
        r = load_run(results, key, a.run, a.rate)
        if r:
            runs.append((key, label.replace("\n", " "), color, r))
    if not runs:
        return []
    all_lat = sorted(lat for _, _, _, (_, pts) in runs for _, lat in pts)
    p99 = all_lat[int(len(all_lat) * 0.99) - 1]
    ymax = max(120.0, p99 * 1.3)            # common y axis so the panels compare fairly

    fig, axes = plt.subplots(len(runs), 1, figsize=(10, 2.6 * len(runs)), sharex=True)
    if len(runs) == 1:
        axes = [axes]
    for ax, (key, label, color, (t0, pts)) in zip(axes, runs):
        shade_flood(ax, a)
        ax.scatter([t for t, _ in pts], [l for _, l in pts], s=5, alpha=0.35, color=color)
        xs, ys = binned_median(pts)
        ax.plot(xs, ys, color="black", linewidth=1.4, label="1 s median")
        if key == "congested_sdn_dynamic":
            for t, _, _, ev in load_events(results, key, a.run, t0):
                if ev:
                    on = ev == "CONGESTION_ON"
                    ax.axvline(t, color="green" if on else "purple", linestyle="--", linewidth=1.5,
                               label="controller: prioritization ON" if on else
                                     "controller: prioritization OFF")
        ax.set_ylim(0, ymax)
        ax.set_ylabel("Latency (ms)")
        ax.set_title(label, fontsize=10, loc="left")
        ax.grid(alpha=0.3)
        ax.legend(loc="upper right", fontsize=8, ncol=2)
    axes[-1].set_xlabel("Time since the players started (s)")
    fig.suptitle(f"One-way latency of every game packet over time (run {a.run}, all players; "
                 f"y axis capped at {ymax:.0f} ms)", fontsize=11)
    fig.tight_layout()
    p1 = os.path.join(out, "latency_over_time.png")
    fig.savefig(p1, dpi=DPI)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 4.8))
    shade_flood(ax, a)
    for key, label, color, (t0, pts) in runs:
        xs, ys = binned_median(pts)
        ax.plot(xs, ys, color=color, linewidth=2, label=label)
    ax.set_xlabel("Time since the players started (s)")
    ax.set_ylabel("Median latency per 1 s window (ms)")
    ax.set_title(f"Latency over time, all conditions (run {a.run})")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    p2 = os.path.join(out, "latency_over_time_medians.png")
    fig.savefig(p2, dpi=DPI)
    plt.close(fig)
    return [p1, p2]


def save_decisions(out, results, a):
    key = "congested_sdn_dynamic"
    r = load_run(results, key, a.run, a.rate)
    if not r:
        return []
    t0, _ = r
    ev = load_events(results, key, a.run, t0)
    if not ev:
        return []
    ts = [e[0] for e in ev]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 5.5), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    shade_flood(ax1, a)
    ax1.plot(ts, [e[1] for e in ev], color="#1f77b4", linewidth=1.6, label="bottleneck load")
    ax1.axhline(a.link_mbps * a.on_threshold, color="green", linestyle=":", label="ON threshold")
    ax1.axhline(a.link_mbps * a.off_threshold, color="purple", linestyle=":", label="OFF threshold")
    ax1.set_ylabel("Bottleneck load (Mbps)")
    ax1.set_title(f"What the dynamic controller measured and decided (run {a.run})")
    ax1.grid(alpha=0.3)
    ax1.legend(fontsize=8, loc="upper right")
    ax2.step(ts, [e[2] for e in ev], where="post", color="#2ca02c")
    ax2.set_yticks([0, 1])
    ax2.set_yticklabels(["OFF", "ON"])
    ax2.set_ylabel("Game queue")
    ax2.set_xlabel("Time since the players started (s)")
    ax2.grid(alpha=0.3)
    for t, _, _, e in ev:
        if e:
            for ax in (ax1, ax2):
                ax.axvline(t, color="green" if e == "CONGESTION_ON" else "purple",
                           linestyle="--", linewidth=1.2)
    fig.tight_layout()
    path = os.path.join(out, "controller_decisions.png")
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return [path]


def main():
    p = argparse.ArgumentParser(description="Draw the comparison graphs")
    p.add_argument("--results", default="results", help="results folder")
    p.add_argument("--out", default="", help="graph folder (default: <results>/graphs)")
    p.add_argument("--run", type=int, default=1, help="which run to use for the over-time graphs")
    p.add_argument("--rate", type=int, default=20, help="player updates per second")
    p.add_argument("--flood-start", type=float, default=10)
    p.add_argument("--flood-duration", type=float, default=10)
    p.add_argument("--link-mbps", type=float, default=10)
    p.add_argument("--on-threshold", type=float, default=0.8)
    p.add_argument("--off-threshold", type=float, default=0.5)
    a = p.parse_args()

    summary = os.path.join(a.results, "summary.csv")
    if not os.path.exists(summary):
        sys.exit(f"{summary} not found. Run experiments/run_experiments.py first.")
    data = load_summary(summary)
    out = a.out or os.path.join(a.results, "graphs")
    os.makedirs(out, exist_ok=True)

    written = [
        save_bar(out, "avg_latency.png", data,
                 [("lat_avg_ms", "Average"), ("lat_median_ms", "Median")],
                 "One-way latency (ms)", "Game packet latency"),
        save_bar(out, "jitter.png", data, [("jitter_avg_ms", "Average jitter")],
                 "Jitter (ms)", "Jitter (mean |latency[n] - latency[n-1]|)"),
        save_bar(out, "packet_loss.png", data, [("loss_pct", "Loss")],
                 "Packet loss (%)", "Game packet loss", ymax=100),
        save_bar(out, "throughput.png", data, [("throughput_kbps", "Throughput")],
                 "Game throughput (kbps)", "Game traffic throughput"),
        save_overview(out, data),
    ]
    written += save_over_time(out, a.results, a)
    written += save_decisions(out, a.results, a)
    print("Wrote:")
    for path in written:
        print("  " + path)
    if not any("over_time" in w for w in written):
        print("Note: no raw packet files found, so the over-time graphs were skipped.")


if __name__ == "__main__":
    main()
