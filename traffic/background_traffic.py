#!/usr/bin/env python3
"""
Background (competing) traffic generator, built on iperf3.

Run it on a Mininet host such as h3. An iperf3 server must already be running on
the sink host (hsink), for example:   hsink iperf3 -s -p 5201 &

NETWORKING CONCEPTS
- The s1-s2 link is the bottleneck (10 Mbps). If this generator sends 20 Mbps
  across it, the link's queue fills up. Packets then wait longer (latency),
  wait by different amounts (jitter), and are dropped once the queue is full (loss).
- UDP mode sends at a fixed rate and does NOT slow down, so it reliably creates
  congestion. TCP mode (--tcp) backs off when it sees loss, so it is gentler.
- Background traffic must NOT use the game port (5000), otherwise it would look
  like game traffic to the controller.
"""
import argparse
import shutil
import subprocess
import sys
import time

GAME_PORT = 5000


def main():
    p = argparse.ArgumentParser(description="Background traffic generator (iperf3)")
    p.add_argument("--server-ip", required=True, help="IP of the iperf3 server (hsink)")
    p.add_argument("--port", type=int, default=5201, help="iperf3 server port")
    p.add_argument("--bandwidth", default="20M",
                   help="UDP sending rate, e.g. 20M (ignored with --tcp)")
    p.add_argument("--duration", type=int, default=10, help="seconds of traffic")
    p.add_argument("--start-delay", type=float, default=0,
                   help="wait this many seconds before starting")
    p.add_argument("--tcp", action="store_true", help="use TCP instead of UDP")
    args = p.parse_args()

    if args.port == GAME_PORT:
        sys.exit(f"Port {GAME_PORT} is the game port. Use a different port.")
    if shutil.which("iperf3") is None:
        sys.exit("iperf3 not found. Install it with: sudo apt install iperf3")

    cmd = ["iperf3", "-c", args.server_ip, "-p", str(args.port),
           "-t", str(args.duration)]
    if not args.tcp:
        cmd += ["-u", "-b", args.bandwidth]

    if args.start_delay > 0:
        print(f"[BG] waiting {args.start_delay}s before starting...", flush=True)
        time.sleep(args.start_delay)

    mode = "TCP" if args.tcp else f"UDP at {args.bandwidth}"
    print(f"[BG] {mode} to {args.server_ip}:{args.port} for {args.duration}s",
          flush=True)
    print("[BG] running: " + " ".join(cmd), flush=True)
    try:
        result = subprocess.run(cmd)        # iperf3 prints its own report
    except KeyboardInterrupt:
        print("[BG] stopped by user")
        return
    if result.returncode != 0:
        print("[BG] iperf3 failed. Is the server running on the sink "
              f"({args.server_ip}:{args.port}) and reachable?")
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
