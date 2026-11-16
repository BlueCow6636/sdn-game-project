#!/usr/bin/env python3
"""
Per-phase statistics from the server's per-packet CSV.

A test run is split into equal phases (default 3 x 10 s: quiet, congested, quiet).
Packets are assigned to a phase by SEQUENCE NUMBER, which is exact because a
player sends one packet per tick: at 20 packets/s, sequence 1-200 were sent in
the first 10 s, 201-400 in the next 10 s, and so on.

  loss in a phase = packets expected in that phase - packets actually received

Usage:
  python3 analysis/phase_stats.py results/congested_no_sdn_run1_packets.csv
"""
import argparse
import csv
import statistics
import sys
from collections import defaultdict


def main():
    p = argparse.ArgumentParser(description="Per-phase latency/loss from packet CSV")
    p.add_argument("csv", help="per-packet CSV written by game_server.py --csv")
    p.add_argument("--rate", type=float, default=20, help="player updates per second")
    p.add_argument("--phase-seconds", type=float, default=10)
    p.add_argument("--phases", type=int, default=3)
    a = p.parse_args()

    per_phase = int(a.rate * a.phase_seconds)       # packets sent per phase
    data = defaultdict(lambda: defaultdict(list))   # player -> phase -> [latency_ms]
    try:
        with open(a.csv, newline="") as f:
            for row in csv.DictReader(f):
                phase = (int(row["sequence"]) - 1) // per_phase
                if 0 <= phase < a.phases:
                    data[row["player"]][phase].append(float(row["latency_ms"]))
    except (OSError, KeyError, ValueError) as e:
        sys.exit(f"Could not read {a.csv}: {e}")
    if not data:
        sys.exit("No data found in the CSV.")

    print(f"{'player':7}{'phase':>10}{'recv':>7}{'loss%':>8}"
          f"{'avg ms':>9}{'median':>9}{'max ms':>9}")
    for player in sorted(data):
        for ph in range(a.phases):
            lats = data[player].get(ph, [])
            start = int(ph * a.phase_seconds)
            end = int((ph + 1) * a.phase_seconds)
            loss = max(0, per_phase - len(lats)) / per_phase * 100
            if lats:
                print(f"{player:7}{f'{start}-{end}s':>10}{len(lats):7d}{loss:8.1f}"
                      f"{statistics.mean(lats):9.1f}{statistics.median(lats):9.1f}"
                      f"{max(lats):9.1f}")
            else:
                print(f"{player:7}{f'{start}-{end}s':>10}{0:7d}{100.0:8.1f}"
                      f"{'-':>9}{'-':>9}{'-':>9}")


if __name__ == "__main__":
    main()
