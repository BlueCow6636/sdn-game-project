#!/usr/bin/env python3
"""
OpenFlow 1.3 Ryu controller, D2 version: DYNAMIC queue-based prioritization.

WHAT IT DOES
1. Installs forwarding rules (see RULES below).
2. Every POLL_INTERVAL seconds it asks the bottleneck switch for PORT STATISTICS
   (an OpenFlow message) and works out how much traffic the bottleneck port sent.
3. If that traffic reaches ON_THRESHOLD of the link capacity, the link is
   saturated, so the controller reinstalls the game rules WITH "set_queue 1":
   game packets now use the high-priority queue and skip the background queue.
4. When traffic stays below OFF_THRESHOLD for OFF_COUNT polls in a row, the
   controller reinstalls the game rules WITHOUT set_queue (back to normal).
   Using two different thresholds (hysteresis) stops it flapping on and off.
5. Every decision is logged and written to results/controller_log.csv.

RULES (higher priority number wins)
  priority   1  anything                           -> NORMAL
  priority 100  UDP dst port 5000 / src port 5000 -> [set_queue 1] + NORMAL
  priority 200  same, for a registered player's IP -> [set_queue 1] + NORMAL
                (installed when the game SERVER reports the player: Socket-SDN link)
  NORMAL = forward like an ordinary learning switch.
  The [set_queue 1] part is present only while prioritization is ON.

MODE (environment variable PRIORITY, read once at start)
  dynamic (default)  prioritization switches on/off by itself (this file's purpose)
  on                 always on  (static prioritization, used as a comparison)
  off                always off (no prioritization, used as the baseline)

TUNING (environment variables, all optional)
  BOTTLENECK_IF=s1-eth5  BOTTLENECK_DPID=1  LINK_MBPS=10  POLL_INTERVAL=0.5
  ON_THRESHOLD=0.8  OFF_THRESHOLD=0.5  ON_COUNT=1  OFF_COUNT=4  LOG_DIR=results
  BOTTLENECK_PORT=<number>  (skips the lookup by name, if the name lookup ever fails)

SOCKET-SDN CHANNEL: the game server sends JSON datagrams to UDP CTRL_PORT:
  {"event": "PLAYER_REGISTER", "player_id": "P1", "ip": "10.0.0.1", "port": 5000}
  {"event": "LOSS_ALERT", "player_id": "P1", "loss_pct": 7.5}
"""
import csv
import json
import os
import socket
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

GAME_PORT = int(os.environ.get("GAME_PORT", 5000))
CTRL_PORT = int(os.environ.get("CTRL_PORT", 9999))
GAME_QUEUE = int(os.environ.get("GAME_QUEUE", 1))
MODE = os.environ.get("PRIORITY", "dynamic").lower()
BOTTLENECK_DPID = int(os.environ.get("BOTTLENECK_DPID", 1))
BOTTLENECK_IF = os.environ.get("BOTTLENECK_IF", "s1-eth5")
LINK_MBPS = float(os.environ.get("LINK_MBPS", 10))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", 0.5))
ON_THRESHOLD = float(os.environ.get("ON_THRESHOLD", 0.8))
OFF_THRESHOLD = float(os.environ.get("OFF_THRESHOLD", 0.5))
ON_COUNT = int(os.environ.get("ON_COUNT", 1))
OFF_COUNT = int(os.environ.get("OFF_COUNT", 4))
LOG_DIR = os.environ.get("LOG_DIR", "results")
BOTTLENECK_PORT = int(os.environ.get("BOTTLENECK_PORT", 0))   # optional: skip the name lookup
ETH_IP = 0x0800
UDP = 17
COOKIE = 0x6001       # tag so our rules are easy to find


class GameSDNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(GameSDNController, self).__init__(*args, **kwargs)
        self.datapaths = {}              # dpid -> datapath (connected switches)
        self.players = {}                # player_id -> ip (from server events)
        self.alerts = {}                 # player_id -> number of loss alerts

        self.mode = MODE if MODE in ("on", "off", "dynamic") else "dynamic"
        if self.mode != MODE:
            self.logger.warning("[MODE] unknown PRIORITY=%s, using dynamic", MODE)
        self.priority_on = (self.mode == "on")   # dynamic mode starts OFF

        self.bn_port = BOTTLENECK_PORT or None   # OpenFlow port number of the bottleneck
        self.last_sample = None          # (switch uptime of port, tx_bytes)
        self.hi = 0                      # consecutive polls above ON_THRESHOLD
        self.lo = 0                      # consecutive polls below OFF_THRESHOLD

        os.makedirs(LOG_DIR, exist_ok=True)
        self.log_file = open(os.path.join(LOG_DIR, "controller_log.csv"), "w",
                             newline="")
        self.log_csv = csv.writer(self.log_file)
        self.log_csv.writerow(["timestamp", "tx_mbps", "util_pct",
                               "priority_on", "event"])
        self.log_file.flush()

        self.logger.info("[MODE] %s (game queue %d, link %.1f Mbps, poll %.2fs, "
                         "on>=%.0f%%, off<%.0f%% for %d polls)", self.mode,
                         GAME_QUEUE, LINK_MBPS, POLL_INTERVAL, ON_THRESHOLD * 100,
                         OFF_THRESHOLD * 100, OFF_COUNT)
        hub.spawn(self._listen_server)   # events from the game server
        hub.spawn(self._monitor)         # port-statistics polling

    # ---------------- OpenFlow side: rules ----------------
    def game_actions(self, dp):
        """Actions for game packets: set the queue only while prioritization is ON."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        actions = []
        if self.priority_on:
            actions.append(parser.OFPActionSetQueue(GAME_QUEUE))
        actions.append(parser.OFPActionOutput(ofp.OFPP_NORMAL))
        return actions

    def add_flow(self, dp, priority, match, actions):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        # OFPFC_ADD with an identical match and priority REPLACES the old rule,
        # which is how we change a rule's actions without deleting it first.
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=COOKIE,
                                      priority=priority, match=match,
                                      instructions=inst))

    def install_game_rules(self, dp):
        """(Re)install every rule whose actions depend on the prioritization state."""
        parser = dp.ofproto_parser
        game = self.game_actions(dp)
        # priority 100: any game traffic (players -> server, and server -> players)
        self.add_flow(dp, 100, parser.OFPMatch(
            eth_type=ETH_IP, ip_proto=UDP, udp_dst=GAME_PORT), game)
        self.add_flow(dp, 100, parser.OFPMatch(
            eth_type=ETH_IP, ip_proto=UDP, udp_src=GAME_PORT), game)
        # priority 200: per registered player
        for ip in self.players.values():
            self.install_player_rules(dp, ip)

    def install_player_rules(self, dp, ip):
        """Per-player rules: game packets sent BY or sent TO this player's IP."""
        parser = dp.ofproto_parser
        game = self.game_actions(dp)
        self.add_flow(dp, 200, parser.OFPMatch(
            eth_type=ETH_IP, ip_proto=UDP, ipv4_src=ip, udp_dst=GAME_PORT), game)
        self.add_flow(dp, 200, parser.OFPMatch(
            eth_type=ETH_IP, ip_proto=UDP, ipv4_dst=ip, udp_src=GAME_PORT), game)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        if dp.id == BOTTLENECK_DPID:
            self.last_sample = None
            # Ask for the port list so we can find the bottleneck port by name
            dp.send_msg(parser.OFPPortDescStatsRequest(dp, 0))

        # Start clean: delete any flows left from a previous run
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            match=parser.OFPMatch()))

        # Rule 1: default, forward normally (background traffic uses this)
        self.add_flow(dp, 1, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_NORMAL)])
        self.install_game_rules(dp)
        self.logger.info("[SWITCH JOINED] DPID=%s: rules installed "
                         "(queue prioritization %s)", dp.id,
                         "ON" if self.priority_on else "OFF")

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_handler(self, ev):
        dp = ev.msg.datapath
        if dp.id != BOTTLENECK_DPID:
            return
        seen = []
        for p in ev.msg.body:
            # The name arrives as fixed-size bytes: keep everything before the
            # first NUL byte (anything after it is padding).
            raw = p.name if isinstance(p.name, bytes) else p.name.encode()
            name = raw.split(b"\x00")[0].decode("utf-8", errors="ignore").strip()
            seen.append("%s=%d" % (name, p.port_no))
            if name == BOTTLENECK_IF:
                self.bn_port = p.port_no
                self.logger.info("[MONITOR] bottleneck %s is OpenFlow port %d on "
                                 "DPID %d (capacity %.1f Mbps)", BOTTLENECK_IF,
                                 p.port_no, dp.id, LINK_MBPS)
        if self.bn_port is None:
            self.logger.warning("[MONITOR] port %s not found on DPID %d. Ports seen: "
                                "%s. Set BOTTLENECK_IF or BOTTLENECK_PORT to match.",
                                BOTTLENECK_IF, dp.id, ", ".join(seen))

    # ---------------- OpenFlow side: monitoring and decisions ----------------
    def _monitor(self):
        """Background loop: request port statistics from the bottleneck switch."""
        while True:
            hub.sleep(POLL_INTERVAL)
            dp = self.datapaths.get(BOTTLENECK_DPID)
            if dp is None or self.bn_port is None:
                continue
            dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(
                dp, 0, dp.ofproto.OFPP_ANY))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_handler(self, ev):
        if ev.msg.datapath.id != BOTTLENECK_DPID or self.bn_port is None:
            return
        for st in ev.msg.body:
            if st.port_no != self.bn_port:
                continue
            # Use the switch's own clock for the interval: more accurate than ours
            uptime = st.duration_sec + st.duration_nsec / 1e9
            if self.last_sample is not None:
                dt = uptime - self.last_sample[0]
                if dt > 0:
                    mbps = (st.tx_bytes - self.last_sample[1]) * 8 / dt / 1e6
                    self.decide(time.time(), mbps, mbps / LINK_MBPS)
            self.last_sample = (uptime, st.tx_bytes)

    def decide(self, now, mbps, util):
        """Compare the measured bottleneck load with the thresholds."""
        event = ""
        if self.mode == "dynamic":
            if not self.priority_on:
                self.hi = self.hi + 1 if util >= ON_THRESHOLD else 0
                if self.hi >= ON_COUNT:
                    self.set_priority(True)
                    event = "CONGESTION_ON"
                    self.logger.warning(
                        "[DYNAMIC] CONGESTION DETECTED: bottleneck at %.2f Mbps "
                        "(%.0f%% of %.0f) -> game queue ON", mbps, util * 100,
                        LINK_MBPS)
            else:
                self.lo = self.lo + 1 if util < OFF_THRESHOLD else 0
                if self.lo >= OFF_COUNT:
                    self.set_priority(False)
                    event = "CONGESTION_OFF"
                    self.logger.warning(
                        "[DYNAMIC] CONGESTION CLEARED: bottleneck at %.2f Mbps "
                        "(%.0f%%) -> game queue OFF", mbps, util * 100)
        self.log_csv.writerow([f"{now:.6f}", f"{mbps:.3f}", f"{util * 100:.1f}",
                               int(self.priority_on), event])
        self.log_file.flush()

    def set_priority(self, on):
        """Switch prioritization on or off by reinstalling the game rules."""
        self.priority_on = on
        self.hi = self.lo = 0
        for dp in self.datapaths.values():
            self.install_game_rules(dp)

    # ---------------- Socket side (events from the game server) ----------------
    def _listen_server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", CTRL_PORT))
        self.logger.info("[CTRL] listening for game-server events on UDP %d", CTRL_PORT)
        while True:
            data, addr = sock.recvfrom(2048)
            try:
                self.handle_event(json.loads(data.decode("utf-8")), addr)
            except Exception as e:                # never let bad input kill the app
                self.logger.warning("[CTRL] bad event from %s: %s", addr, e)

    def handle_event(self, ev, addr):
        kind, pid = ev.get("event"), ev.get("player_id")
        if kind == "PLAYER_REGISTER":
            ip = ev["ip"]
            self.players[pid] = ip
            for dp in self.datapaths.values():
                self.install_player_rules(dp, ip)
            self.logger.info("[APP EVENT] %s registered (%s) -> priority-200 rules "
                             "installed on %d switches", pid, ip, len(self.datapaths))
        elif kind == "LOSS_ALERT":
            self.alerts[pid] = self.alerts.get(pid, 0) + 1
            self.logger.warning("[APP EVENT] LOSS ALERT %s: %.1f%% loss (alert #%d)",
                                pid, ev.get("loss_pct", 0), self.alerts[pid])
        else:
            self.logger.warning("[CTRL] unknown event: %s", ev)
