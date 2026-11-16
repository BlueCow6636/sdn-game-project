#!/usr/bin/env python3
"""
Runs all experiments automatically and writes the results as CSV files.

CONDITIONS (each is run --runs times, default 3)
  normal                 no background traffic, no prioritization   (Experiment A)
  congested_no_sdn       flood, no prioritization                   (Experiment B)
  congested_sdn_static   flood, game queue ALWAYS on                (static SDN)
  congested_sdn_dynamic  flood, game queue switched on/off by the
                         controller when it detects congestion      (dynamic SDN)

EVERY RUN (default 30 s)
  0-10 s  players only ("before")
  10-20 s players + a 20 Mbps UDP flood on the 10 Mbps bottleneck ("during")
          (in the "normal" condition there is no flood, but the same window is
           still measured so the conditions can be compared fairly)
  20-30 s players only ("after")

FOR EACH CONDITION the script
  1. starts Ryu in the right mode (environment variable PRIORITY),
  2. builds the Mininet network (topo/topology.py),
  3. runs the game server, the players and the flood,
  4. stops everything and keeps the raw logs.

OUTPUT (all under results/)
  raw/<condition>_run<k>_packets.csv      one row per received game packet
  raw/<condition>_run<k>_summary.csv      the server's own summary
  raw/<condition>_run<k>_controller.csv   controller log for that run's time window
  <condition>.csv                         one row per run and phase (the metrics below)
  summary.csv                             mean and standard deviation over the runs

METRICS (pooled over all players, per phase)
  loss_pct        (expected - received) / expected * 100, using sequence numbers
  latency         one-way: server receive time - player send time (ms)
  jitter          |latency[n] - latency[n-1]| between consecutive received packets
  throughput_kbps game bytes delivered * 8 / phase length

Run from the project folder, with sudo, and with no other Ryu or Mininet running:
  sudo python3 experiments/run_experiments.py
"""
import argparse
import csv
import os
import pwd
import shutil
import statistics
import subprocess
import sys
import time

sys.dont_write_bytecode = True      # do not leave root-owned __pycache__ folders

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server", "game_server.py")
PLAYER = os.path.join(ROOT, "client", "player.py")
BG = os.path.join(ROOT, "traffic", "background_traffic.py")
RESULTS = os.path.join(ROOT, "results")
RAW = os.path.join(RESULTS, "raw")
MGMT_IP = "10.0.0.200"              # the VM itself, where Ryu listens for events

# condition name -> (PRIORITY mode for the controller, flood on/off)
CONDITIONS = {
    "normal": ("off", False),
    "congested_no_sdn": ("off", True),
    "congested_sdn_static": ("on", True),
    "congested_sdn_dynamic": ("dynamic", True),
}
PHASES = ["before", "during", "after", "all"]
METRICS = ["received", "expected", "loss_pct", "lat_min_ms", "lat_avg_ms",
           "lat_median_ms", "lat_max_ms", "jitter_min_ms", "jitter_avg_ms",
           "jitter_max_ms", "throughput_kbps"]


# ----------------------------------------------------------------------------
# Analysis (no Mininet needed, so it can be tested on its own)
# ----------------------------------------------------------------------------
def load_packets(path):
    """player -> list of (sequence, latency_ms, bytes)."""
    data = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            data.setdefault(row["player"], []).append(
                (int(row["sequence"]), float(row["latency_ms"]),
                 int(row.get("bytes") or 0)))
    return data


def phase_table(a):
    """(name, first_seq, last_seq_exclusive, seconds). A player sends one packet
    per tick, so sequence numbers map exactly onto time."""
    r, fs, fd = a.rate, a.flood_start, a.flood_duration
    rest = a.duration - fs - fd
    inf = float("inf")
    return [("before", 1, fs * r + 1, fs),
            ("during", fs * r + 1, (fs + fd) * r + 1, fd),
            ("after", (fs + fd) * r + 1, inf, rest),
            ("all", 1, inf, a.duration)]


def phase_metrics(data, players, lo, hi, seconds, rate):
    lats, jits, nbytes, received = [], [], 0, 0
    for pid in players:                       # a player with no packets counts as 100% loss
        rows = sorted(r for r in data.get(pid, []) if lo <= r[0] < hi)
        received += len(rows)
        nbytes += sum(r[2] for r in rows)
        lat = [r[1] for r in rows]
        lats += lat
        jits += [abs(y - x) for x, y in zip(lat, lat[1:])]
    expected = int(round(rate * seconds)) * len(players)
    loss = max(0, expected - received) / expected * 100 if expected else 0.0

    def stat(fn, values):
        return round(fn(values), 3) if values else 0.0
    return {"received": received, "expected": expected,
            "loss_pct": round(loss, 2),
            "lat_min_ms": stat(min, lats), "lat_avg_ms": stat(statistics.mean, lats),
            "lat_median_ms": stat(statistics.median, lats),
            "lat_max_ms": stat(max, lats),
            "jitter_min_ms": stat(min, jits), "jitter_avg_ms": stat(statistics.mean, jits),
            "jitter_max_ms": stat(max, jits),
            "throughput_kbps": round(nbytes * 8 / seconds / 1000, 2) if seconds else 0.0}


def analyse_run(packets_csv, a):
    players = [f"P{i}" for i in range(1, a.players + 1)]
    data = load_packets(packets_csv)
    out = []
    for name, lo, hi, secs in phase_table(a):
        m = phase_metrics(data, players, lo, hi, secs, a.rate)
        m["phase"] = name
        out.append(m)
    return out


def write_condition_csv(name, rows):
    path = os.path.join(RESULTS, f"{name}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["condition", "run", "phase"] + METRICS)
        for run, m in rows:
            w.writerow([name, run, m["phase"]] + [m[k] for k in METRICS])
    return path


def write_summary():
    """Mean and standard deviation over runs, for every condition found on disk."""
    out_rows = []
    for name in CONDITIONS:
        path = os.path.join(RESULTS, f"{name}.csv")
        if not os.path.exists(path):
            continue
        groups = {}
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                groups.setdefault(row["phase"], []).append(row)
        for phase in PHASES:
            rows = groups.get(phase)
            if not rows:
                continue
            rec = {"condition": name, "phase": phase, "runs": len(rows)}
            for k in METRICS:
                vals = [float(r[k]) for r in rows]
                rec[k + "_mean"] = round(statistics.mean(vals), 3)
                rec[k + "_std"] = round(statistics.stdev(vals), 3) if len(vals) > 1 else 0.0
            out_rows.append(rec)
    if not out_rows:
        return out_rows
    path = os.path.join(RESULTS, "summary.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    return out_rows


def print_summary(rows):
    print("\n=== SUMMARY (mean over runs; phase 'during' = 10-20 s flood window) ===")
    print(f"{'condition':24}{'phase':8}{'runs':>5}{'loss %':>9}{'lat med ms':>12}"
          f"{'lat avg ms':>12}{'jitter ms':>11}{'kbps':>9}")
    for r in rows:
        if r["phase"] not in ("during", "all"):
            continue
        print(f"{r['condition']:24}{r['phase']:8}{r['runs']:5d}"
              f"{r['loss_pct_mean']:9.2f}{r['lat_median_ms_mean']:12.2f}"
              f"{r['lat_avg_ms_mean']:12.2f}{r['jitter_avg_ms_mean']:11.2f}"
              f"{r['throughput_kbps_mean']:9.2f}")


# ----------------------------------------------------------------------------
# Running the network
# ----------------------------------------------------------------------------
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def default_ryu():
    user = os.environ.get("SUDO_USER")
    home = pwd.getpwnam(user).pw_dir if user else os.path.expanduser("~")
    return os.path.join(home, "ryu-venv", "bin", "ryu-manager")


def wait_log(path, ok, timeout, what):
    """Wait until ok(text_of_log_file) is true."""
    end = time.time() + timeout
    text = ""
    while time.time() < end:
        try:
            text = open(path).read()
        except OSError:
            text = ""
        if ok(text):
            return
        time.sleep(0.5)
    tail = "\n".join(text.splitlines()[-12:])
    raise RuntimeError(f"Timed out waiting for {what}. Last lines of {path}:\n{tail}")


def stop(proc, timeout=6):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


def slice_controller_log(src, dst, t0, t1):
    """Copy the controller-log rows that fall inside this run's time window."""
    if not os.path.exists(src):
        return
    with open(src, newline="") as f, open(dst, "w", newline="") as g:
        reader, writer = csv.reader(f), csv.writer(g)
        writer.writerow(next(reader, []))
        for row in reader:
            try:
                if t0 <= float(row[0]) <= t1:
                    writer.writerow(row)
            except (ValueError, IndexError):
                pass


def one_run(net, a, name, flood, k, ctrl_log):
    hserver, hsink = net.get("hserver"), net.get("hsink")
    players = [net.get(f"h{i}") for i in range(1, a.players + 1)]
    flooder = net.get("h4")
    tag = f"{name}_run{k}"
    pk = os.path.join(RAW, tag + "_packets.csv")
    sm = os.path.join(RAW, tag + "_summary.csv")
    procs, files = [], []

    def logfile(suffix):
        f = open(os.path.join(RAW, f"{tag}_{suffix}.log"), "w")
        files.append(f)
        return f
    iperf = srv = None
    try:
        if flood:
            iperf = hsink.popen(["iperf3", "-s", "-p", "5201"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        srv = hserver.popen(
            ["python3", "-u", SERVER, "--host", hserver.IP(), "--port", "5000",
             "--ctrl-ip", MGMT_IP, "--csv", pk, "--summary", sm],
            stdout=logfile("server"), stderr=subprocess.STDOUT)
        time.sleep(1.5)                       # let the server bind and iperf3 listen
        t0 = time.time()
        for i, h in enumerate(players, start=1):
            procs.append(h.popen(
                ["python3", "-u", PLAYER, "--id", f"P{i}", "--server-ip", hserver.IP(),
                 "--rate", str(a.rate), "--duration", str(a.duration)],
                stdout=logfile(f"player{i}"), stderr=subprocess.STDOUT))
        if flood:
            procs.append(flooder.popen(
                ["python3", BG, "--server-ip", hsink.IP(), "--bandwidth", a.flood_bw,
                 "--duration", str(a.flood_duration), "--start-delay", str(a.flood_start)],
                stdout=logfile("flood"), stderr=subprocess.STDOUT))
        for p in procs:
            p.wait(timeout=a.duration + 20)
        time.sleep(2)                         # let the last packets arrive
    finally:
        for p in procs:
            stop(p, 2)
        stop(srv)                             # SIGTERM makes the server write its summary
        stop(iperf, 2)
        for f in files:
            f.close()
    t1 = time.time()
    if not os.path.exists(pk):
        raise RuntimeError(f"No packet log was written for {tag}. See raw/{tag}_server.log")
    slice_controller_log(ctrl_log, os.path.join(RAW, tag + "_controller.csv"), t0, t1)
    return analyse_run(pk, a)


def run_condition(a, name):
    import topology                           # topo/topology.py (needs Mininet)
    from mininet.log import setLogLevel
    mode, flood = CONDITIONS[name]
    setLogLevel("warning")

    ctrl_dir = os.path.join(RESULTS, f"ctrl_{name}")
    ryu_log = os.path.join(RAW, f"ryu_{name}.log")
    env = dict(os.environ, PRIORITY=mode, LOG_DIR=ctrl_dir, PYTHONUNBUFFERED="1")
    log(f"=== {name}: Ryu mode '{mode}', flood {'ON' if flood else 'OFF'} ===")
    ryu_out = open(ryu_log, "w")
    ryu = subprocess.Popen([a.ryu, "controller/sdn_controller.py"], cwd=ROOT, env=env,
                           stdout=ryu_out, stderr=subprocess.STDOUT)
    net = None
    rows = []
    try:
        wait_log(ryu_log, lambda t: "listening for game-server events" in t
                 or ryu.poll() is not None, 25, "Ryu to start")
        if ryu.poll() is not None:
            raise RuntimeError(f"Ryu exited immediately. See {ryu_log}")
        time.sleep(2)

        targs = argparse.Namespace(controller_ip="127.0.0.1", controller_port=6653,
                                   players=4, bw=a.bw, delay=a.delay, loss=0,
                                   queue_len=a.queue_len, no_queues=False)
        net, bottleneck, root, root_link = topology.build(targs)
        net.start()
        root_if = root_link.intf1.name
        root.cmd(f"ip addr add {MGMT_IP}/24 dev {root_if}")
        root.cmd(f"ip link set {root_if} up")
        topology.setup_queues(bottleneck.intf1.name, a.bw, a.queue_len)
        wait_log(ryu_log, lambda t: t.count("[SWITCH JOINED]") >= 2
                 and "[MONITOR] bottleneck" in t, 30, "the switches to join Ryu")
        time.sleep(2)
        for attempt in range(3):              # first pings can be lost while MACs are learned
            drop = net.pingAll(timeout=2)
            if drop == 0:
                break
            time.sleep(2)
        log(f"network ready (ping loss {drop:.0f}%)")

        for k in range(1, a.runs + 1):
            log(f"{name} run {k}/{a.runs} ({a.duration}s)...")
            phase_rows = one_run(net, a, name, flood, k, os.path.join(ctrl_dir, "controller_log.csv"))
            for m in phase_rows:
                rows.append((k, m))
            d = next(m for m in phase_rows if m["phase"] == "during")
            log(f"  during flood window: loss {d['loss_pct']}%, "
                f"median latency {d['lat_median_ms']} ms, jitter {d['jitter_avg_ms']} ms")
            time.sleep(a.settle)
    finally:
        if net is not None:
            net.stop()
        stop(ryu)
        ryu_out.close()
    if rows:
        log(f"wrote {write_condition_csv(name, rows)}")


def main():
    p = argparse.ArgumentParser(description="Run all SDN game experiments")
    p.add_argument("--runs", type=int, default=3, help="repeats per condition")
    p.add_argument("--conditions", default=",".join(CONDITIONS),
                   help="comma-separated subset to run (default: all four)")
    p.add_argument("--players", type=int, default=3, help="player hosts, 1 to 3 (h4 sends the flood)")
    p.add_argument("--rate", type=int, default=20, help="game updates per second per player")
    p.add_argument("--duration", type=int, default=30)
    p.add_argument("--flood-start", type=int, default=10)
    p.add_argument("--flood-duration", type=int, default=10)
    p.add_argument("--flood-bw", default="20M")
    p.add_argument("--bw", type=float, default=10, help="bottleneck rate, Mbps")
    p.add_argument("--delay", type=float, default=10, help="path delay, ms")
    p.add_argument("--queue-len", type=int, default=50)
    p.add_argument("--settle", type=float, default=3, help="seconds between runs")
    p.add_argument("--ryu", default=default_ryu(), help="path to ryu-manager")
    a = p.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo (Mininet needs root): sudo python3 experiments/run_experiments.py")
    if not 1 <= a.players <= 3:
        sys.exit("--players must be 1 to 3")
    if a.flood_start + a.flood_duration >= a.duration:
        sys.exit("The flood must end before the run does (leave a quiet phase after it).")
    names = [c.strip() for c in a.conditions.split(",") if c.strip()]
    for n in names:
        if n not in CONDITIONS:
            sys.exit(f"Unknown condition '{n}'. Choose from: {', '.join(CONDITIONS)}")
    for path in (SERVER, PLAYER, BG, a.ryu):
        if not os.path.exists(path):
            sys.exit(f"Missing file: {path}")
    if shutil.which("iperf3") is None:
        sys.exit("iperf3 not found: sudo apt install iperf3")

    os.makedirs(RAW, exist_ok=True)
    subprocess.run(["mn", "-c"], capture_output=True)     # clear leftovers (also stops stray Ryu)
    sys.path.insert(0, os.path.join(ROOT, "topo"))
    est = len(names) * (a.runs * (a.duration + 9) + 25) / 60
    log(f"{len(names)} condition(s) x {a.runs} run(s), about {est:.0f} minutes")
    try:
        for n in names:
            run_condition(a, n)
    except KeyboardInterrupt:
        log("interrupted")
    except RuntimeError as e:
        log(f"ERROR: {e}")
    finally:
        subprocess.run(["mn", "-c"], capture_output=True)
        rows = write_summary()
        if rows:
            print_summary(rows)
            log(f"wrote {os.path.join(RESULTS, 'summary.csv')}")
        user = os.environ.get("SUDO_USER")        # give the files back to the normal user
        if user:
            for folder, dirs, files in os.walk(RESULTS):
                for item in dirs + files:
                    try:
                        shutil.chown(os.path.join(folder, item), user=user)
                    except (OSError, LookupError):
                        pass


if __name__ == "__main__":
    main()
