#!/usr/bin/env python3
"""
Mininet topology, D2 version (queue-based prioritization).

   h1..h4 --- s1 ===== bottleneck (s1-eth5) ===== s2 --- hserver (game server)
   (players)                                       |---- hsink   (background sink)
   h3 also sends the background flood              '---- root    (the VM, 10.0.0.200,
                                                            where Ryu listens)

WHAT CHANGED FROM D1
1. The s1-s2 link is now a plain link. Its rate limit AND its queues are created
   by OPEN VSWITCH (QoS type linux-htb), not by Mininet's TCLink.
   Why: the OpenFlow action set_queue only works on queues that Open vSwitch
   itself created on that port.
2. The propagation delay moved from the bottleneck to the link in front of
   hserver (delay + 1 ms). Why: Open vSwitch replaces the tc setup on the port it
   manages, which would remove the delay emulation. The one-way delay from a
   player to the server is unchanged (1 ms + 11 ms = 12 ms, as before).

QUEUES ON THE BOTTLENECK PORT (s1-eth5)
   queue 0 : default queue. Background traffic goes here. Lower priority.
   queue 1 : game queue.   Game packets go here when the controller sends them
                           with set_queue:1. Higher priority, guaranteed 20% of rate.
   HTB = Hierarchical Token Bucket, a Linux traffic shaper that enforces the rate
   limit and serves the queues according to their priority.

Run as root from the project folder:
   sudo python3 topo/topology.py --controller-port 6653
"""
import argparse
import os
import subprocess
import sys
import time

from mininet.cli import CLI
from mininet.link import Link, TCLink
from mininet.log import error, info, setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController


def parse_args():
    p = argparse.ArgumentParser(description="SDN game topology (D2)")
    p.add_argument("--controller-ip", default="127.0.0.1")
    p.add_argument("--controller-port", type=int, default=6653)
    p.add_argument("--players", type=int, default=4, help="number of player hosts")
    p.add_argument("--bw", type=float, default=10, help="bottleneck rate, Mbps")
    p.add_argument("--delay", type=float, default=10,
                   help="path delay to the server, ms (put on the server link)")
    p.add_argument("--loss", type=float, default=0,
                   help="random loss on the server link, percent (both directions)")
    p.add_argument("--queue-len", type=int, default=50,
                   help="bottleneck queue length, packets")
    p.add_argument("--no-queues", action="store_true",
                   help="skip the Open vSwitch queue setup (debugging only)")
    return p.parse_args()


def run(cmd):
    """Run a command as root and raise a readable error if it fails."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout


def setup_queues(ifname, bw_mbps, queue_len):
    """Create two HTB queues on the bottleneck port using Open vSwitch QoS."""
    rate = int(bw_mbps * 1_000_000)          # bits per second
    game_min = int(rate * 0.2)               # guaranteed rate for the game queue
    other_min = int(rate * 0.1)              # guaranteed rate for the default queue

    # The HTB leaf queues take their length from txqueuelen, so set it FIRST.
    run(["ip", "link", "set", "dev", ifname, "txqueuelen", str(queue_len)])

    run(["ovs-vsctl", "set", "port", ifname, "qos=@newqos", "--",
         "--id=@newqos", "create", "qos", "type=linux-htb",
         f"other-config:max-rate={rate}", "queues=0=@q0,1=@q1", "--",
         "--id=@q0", "create", "queue", f"other-config:min-rate={other_min}",
         f"other-config:max-rate={rate}", "other-config:priority=2", "--",
         "--id=@q1", "create", "queue", f"other-config:min-rate={game_min}",
         f"other-config:max-rate={rate}", "other-config:priority=1"])


def build(args):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                  autoStaticArp=True)
    net.addController("c0", controller=RemoteController,
                      ip=args.controller_ip, port=args.controller_port)
    s1 = net.addSwitch("s1", protocols="OpenFlow13")
    s2 = net.addSwitch("s2", protocols="OpenFlow13")

    hserver = net.addHost("hserver", ip="10.0.0.100/24", mac="00:00:00:00:00:64")
    hsink = net.addHost("hsink", ip="10.0.0.101/24", mac="00:00:00:00:00:65")

    # Server link carries the path delay (and optional random loss).
    server_link = {"bw": 100, "delay": f"{args.delay + 1:g}ms"}
    if args.loss > 0:
        server_link["loss"] = args.loss
    net.addLink(hserver, s2, cls=TCLink, **server_link)
    net.addLink(hsink, s2, cls=TCLink, bw=100, delay="1ms")

    for i in range(1, args.players + 1):
        h = net.addHost(f"h{i}", ip=f"10.0.0.{i}/24", mac=f"00:00:00:00:00:{i:02x}")
        net.addLink(h, s1, cls=TCLink, bw=100, delay="1ms")

    bottleneck = net.addLink(s1, s2, cls=Link)       # plain link, no tc

    # Management host: puts the VM itself (where Ryu runs) on the network.
    root = Node("root", inNamespace=False)
    root_link = net.addLink(root, s2, cls=Link)
    return net, bottleneck, root, root_link


def main():
    if os.geteuid() != 0:
        sys.exit("Run with sudo (Mininet needs root).")
    args = parse_args()
    setLogLevel("info")
    net, bottleneck, root, root_link = build(args)
    try:
        net.start()

        root_if = root_link.intf1.name
        root.cmd(f"ip addr add 10.0.0.200/24 dev {root_if}")
        root.cmd(f"ip link set {root_if} up")

        bn_if = bottleneck.intf1.name                # s1's side of the bottleneck
        if args.no_queues:
            info(f"*** Skipping queue setup. Bottleneck port is {bn_if}\n")
        else:
            setup_queues(bn_if, args.bw, args.queue_len)
            info(f"*** Bottleneck port {bn_if}: {args.bw:g} Mbps, "
                 f"queue {args.queue_len} packets, Open vSwitch queues 0 (default) "
                 f"and 1 (game) created\n")

        info("*** Waiting 3s for the switches to connect to the controller\n")
        time.sleep(3)
        CLI(net)
    except RuntimeError as e:
        error(f"*** Setup failed:\n{e}\n")
    finally:
        net.stop()


if __name__ == "__main__":
    main()
