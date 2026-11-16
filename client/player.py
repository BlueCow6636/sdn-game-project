#!/usr/bin/env python3
"""
client/player.py
UDP Game Client Simulating Player Kinematics and Metric Collection.
"""

import socket
import json
import time
import argparse
import random
import csv
import os

def run_player(player_id: str, server_ip: str, server_port: int, rate_hz: float, duration: float, csv_path: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    server_addr = (server_ip, server_port)

    # Step 1: Handshake Registration
    reg_packet = json.dumps({"type": "REGISTER", "player_id": player_id}).encode("utf-8")
    registered = False
    print(f"[{player_id}] Attempting registration with server {server_ip}:{server_port}...")

    retries = 5
    while not registered and retries > 0:
        sock.sendto(reg_packet, server_addr)
        t_start = time.time()
        while time.time() - t_start < 1.0:
            try:
                resp, _ = sock.recvfrom(2048)
                data = json.loads(resp.decode("utf-8"))
                if data.get("type") == "REGISTER_ACK":
                    registered = True
                    print(f"[{player_id}] Successfully registered with server.")
                    break
            except BlockingIOError:
                time.sleep(0.01)
        retries -= 1

    if not registered:
        print(f"[{player_id}] Registration failed. Proceeding under optimistic transmission.")

    # Prepare latency tracking log
    csv_file = None
    csv_writer = None
    if csv_path:
        os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
        csv_file = open(csv_path, mode="w", newline="")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["timestamp_sent", "player_id", "sequence", "rtt_or_latency_ms"])

    # State variables
    seq = 1
    x = round(random.uniform(0.0, 100.0), 2)
    y = round(random.uniform(0.0, 100.0), 2)
    hp = 100
    interval = 1.0 / rate_hz

    start_time = time.time()
    next_tick = start_time

    print(f"[{player_id}] Commencing updates at {rate_hz} Hz ({interval*1000:.1f}ms per tick)...")

    try:
        while True:
            cur_time = time.time()
            if duration > 0 and (cur_time - start_time) >= duration:
                break

            if cur_time >= next_tick:
                # Update synthetic positions
                x = round(x + random.uniform(-1.5, 1.5), 2)
                y = round(y + random.uniform(-1.5, 1.5), 2)

                payload = json.dumps({
                    "type": "UPDATE",
                    "player_id": player_id,
                    "seq": seq,
                    "timestamp": cur_time,
                    "x": x,
                    "y": y,
                    "hp": hp
                }).encode("utf-8")

                sock.sendto(payload, server_addr)

                if csv_writer:
                    csv_writer.writerow([f"{cur_time:.6f}", player_id, seq, 0.0])

                seq += 1
                next_tick += interval

            # Drain inbound packets (relayed state updates from other players)
            while True:
                try:
                    inbound_data, _ = sock.recvfrom(2048)
                    recv_t = time.time()
                    msg = json.loads(inbound_data.decode("utf-8"))
                    if msg.get("type") == "STATE_RELAY":
                        orig_t = msg.get("t_sent", recv_t)
                        latency_ms = (recv_t - orig_t) * 1000.0
                        # Record peer-to-peer transmission latency via server relay
                        if csv_writer:
                            csv_writer.writerow([f"{recv_t:.6f}", msg.get("player_id"), msg.get("seq"), f"{latency_ms:.3f}"])
                except BlockingIOError:
                    break
                except Exception:
                    break

            time.sleep(0.001)

    except KeyboardInterrupt:
        print(f"\n[{player_id}] Terminated by user.")
    finally:
        if csv_file:
            csv_file.close()
        sock.close()
        print(f"[{player_id}] Finished execution. Sent {seq - 1} frames.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SDN Game UDP Client")
    parser.add_argument("--id", required=True, help="Player identifier (e.g., P1)")
    parser.add_argument("--server-ip", default="127.0.0.1", help="Target server IP")
    parser.add_argument("--server-port", type=int, default=5000, help="Target server UDP port")
    parser.add_argument("--rate", type=float, default=20.0, help="Update frequency in Hz (default: 20)")
    parser.add_argument("--duration", type=float, default=0, help="Execution duration in seconds (0 = infinite)")
    parser.add_argument("--csv", default="", help="Path to write execution logs")
    args = parser.parse_args()

    run_player(args.id, args.server_ip, args.server_port, args.rate, args.duration, args.csv)
