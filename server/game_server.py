#!/usr/bin/env python3
"""
UDP game server (JSON datagrams), D2 version.
- Registers players, tracks sequence numbers, detects loss (no retransmission).
- Measures one-way latency, jitter and throughput.
- Notifies the SDN controller about player events.

LATENCY  = server receive time - player send timestamp (ms).
           Valid because all Mininet hosts share one clock (the VM's).
JITTER   = average of |latency[n] - latency[n-1]| over consecutive packets.
THROUGHPUT = game bytes received * 8 / time between first and last packet.

PER-PACKET CSV columns: timestamp, player, sequence, latency_ms, bytes
  (bytes = size of the UDP payload, used for per-phase throughput)
"""
import argparse
import csv
import json
import os
import signal
import socket
import statistics
import time


def make_notifier(ip, port):
    """Returns notify(event_dict). Sends one UDP datagram to the controller."""
    if not ip:
        return lambda event: None
    csock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def notify(event):
        try:
            csock.sendto(json.dumps(event).encode("utf-8"), (ip, port))
        except OSError as e:
            print(f"[CTRL] could not notify controller: {e}")
    return notify


def new_stats(seq=0):
    return {"last_seq": seq, "received": 0, "lost": 0, "out_of_order": 0,
            "w_recv": 0, "w_lost": 0,                 # counters for the current window
            "lat": [], "jit": [], "last_lat": None,   # latency and jitter samples
            "bytes": 0, "first_t": None, "last_t": None}


def summarize(stats):
    """One summary row (dict) per player."""
    rows = []
    for pid, s in sorted(stats.items()):
        total = s["received"] + s["lost"]
        loss = s["lost"] / total * 100 if total else 0.0
        lat, jit = s["lat"], s["jit"]
        span = (s["last_t"] - s["first_t"]) if s["first_t"] is not None else 0
        kbps = s["bytes"] * 8 / span / 1000 if span > 0 else 0.0
        rows.append({
            "player": pid, "received": s["received"], "lost": s["lost"],
            "loss_pct": round(loss, 2),
            "lat_min_ms": round(min(lat), 3) if lat else 0.0,
            "lat_avg_ms": round(statistics.mean(lat), 3) if lat else 0.0,
            "lat_median_ms": round(statistics.median(lat), 3) if lat else 0.0,
            "lat_max_ms": round(max(lat), 3) if lat else 0.0,
            "jitter_min_ms": round(min(jit), 3) if jit else 0.0,
            "jitter_avg_ms": round(statistics.mean(jit), 3) if jit else 0.0,
            "jitter_max_ms": round(max(jit), 3) if jit else 0.0,
            "throughput_kbps": round(kbps, 2)})
    return rows


def run_server(args):
    notify = make_notifier(args.ctrl_ip, args.ctrl_port)
    # SIGTERM (from pkill or the experiment runner) behaves like Ctrl+C,
    # so the summary is still written
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # UDP
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as e:
        raise SystemExit(f"Cannot bind {args.host}:{args.port}: {e}")
    sock.settimeout(0.5)

    csv_file = csv_writer = None
    if args.csv:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)), exist_ok=True)
        csv_file = open(args.csv, "w", newline="")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["timestamp", "player", "sequence", "latency_ms", "bytes"])

    print(f"[SERVER] Running on UDP {args.host}:{args.port}", flush=True)
    if args.ctrl_ip:
        print(f"[SERVER] Notifying controller at {args.ctrl_ip}:{args.ctrl_port}",
              flush=True)

    clients, stats = {}, {}
    last_report = time.time()
    try:
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                data = None
            now = time.time()

            if data:
                try:
                    msg = json.loads(data.decode("utf-8"))
                    kind, pid = msg.get("type"), str(msg.get("player_id"))
                    if kind == "REGISTER":
                        clients[pid], stats[pid] = addr, new_stats()
                        print(f"[REGISTRATION] {pid} from {addr[0]}:{addr[1]}",
                              flush=True)
                        sock.sendto(json.dumps({"type": "REGISTER_ACK",
                                                "status": "OK"}).encode(), addr)
                        notify({"event": "PLAYER_REGISTER", "player_id": pid,
                                "ip": addr[0], "port": addr[1]})
                    elif kind == "UPDATE":
                        seq = int(msg["seq"])
                        t_sent = float(msg["timestamp"])
                        if pid not in clients:          # update before register
                            clients[pid], stats[pid] = addr, new_stats(seq - 1)
                            notify({"event": "PLAYER_REGISTER", "player_id": pid,
                                    "ip": addr[0], "port": addr[1]})
                        st = stats[pid]

                        # ---- sequence numbers: a gap means lost packets ----
                        expected = st["last_seq"] + 1
                        if seq > expected:
                            st["lost"] += seq - expected
                            st["w_lost"] += seq - expected
                            st["last_seq"] = seq
                        elif seq == expected:
                            st["last_seq"] = seq
                        else:                           # late / reordered
                            st["out_of_order"] += 1
                        st["received"] += 1
                        st["w_recv"] += 1

                        # ---- latency, jitter, throughput ----
                        lat = (now - t_sent) * 1000.0
                        st["lat"].append(lat)
                        if st["last_lat"] is not None:
                            st["jit"].append(abs(lat - st["last_lat"]))
                        st["last_lat"] = lat
                        st["bytes"] += len(data)
                        if st["first_t"] is None:
                            st["first_t"] = now
                        st["last_t"] = now
                        if csv_writer:
                            csv_writer.writerow([f"{now:.6f}", pid, seq,
                                                 f"{lat:.3f}", len(data)])

                        # ---- relay to the other players ----
                        relay = json.dumps({
                            "type": "STATE_RELAY", "player_id": pid, "seq": seq,
                            "t_sent": t_sent, "x": msg.get("x"),
                            "y": msg.get("y"), "hp": msg.get("hp")}).encode()
                        for other, caddr in clients.items():
                            if other != pid:
                                sock.sendto(relay, caddr)
                except (ValueError, KeyError, TypeError, OSError) as e:
                    print(f"[WARN] bad datagram from {addr}: {e}", flush=True)

            if now - last_report >= args.stats_interval:
                print("\n--- [SERVER TRAFFIC METRICS] ---", flush=True)
                for pid, s in stats.items():
                    total = s["received"] + s["lost"]
                    cum = s["lost"] / total * 100 if total else 0.0
                    wtotal = s["w_recv"] + s["w_lost"]
                    win = s["w_lost"] / wtotal * 100 if wtotal else 0.0
                    print(f"{pid} | recv {s['received']} | lost {s['lost']} | "
                          f"loss {cum:.2f}% | last window {win:.2f}%", flush=True)
                    if wtotal >= 10 and win > args.loss_threshold:
                        notify({"event": "LOSS_ALERT", "player_id": pid,
                                "loss_pct": round(win, 2)})
                    s["w_recv"] = s["w_lost"] = 0
                last_report = now
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down.", flush=True)
    finally:
        sock.close()
        if csv_file:
            csv_file.close()
        rows = summarize(stats)
        if rows:
            print("\n=== FINAL SUMMARY ===", flush=True)
            for r in rows:
                print(r, flush=True)
            if args.summary:
                os.makedirs(os.path.dirname(os.path.abspath(args.summary)),
                            exist_ok=True)
                with open(args.summary, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="SDN Game UDP Server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--ctrl-ip", default="", help="controller IP (empty = no notifications)")
    p.add_argument("--ctrl-port", type=int, default=9999)
    p.add_argument("--loss-threshold", type=float, default=5.0,
                   help="send LOSS_ALERT when window loss %% exceeds this")
    p.add_argument("--stats-interval", type=float, default=5.0)
    p.add_argument("--csv", default="", help="per-packet CSV: timestamp,player,sequence,latency_ms,bytes")
    p.add_argument("--summary", default="", help="summary CSV, one row per player")
    run_server(p.parse_args())
