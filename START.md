# How to start the project

Two ways to run it. Pick one.

> **Golden rule:** run `sudo mn -c` only **before** starting Ryu. `sudo mn -c` also kills any running Ryu, which makes every switch stop forwarding.

---

## A. Run all experiments automatically (about 10 minutes)

1. Close any open Mininet and Ryu (type `exit` at `mininet>`, press Ctrl+C in the Ryu terminal).
2. Run:

```bash
cd ~/sdn_game_project
sudo python3 experiments/run_experiments.py
```

3. Leave the VM alone until it prints the summary table.
4. Draw the graphs (no sudo needed):

```bash
python3 analysis/analyze_results.py
```

Useful options:

```bash
# one condition, one run (about 2 minutes), good for a quick test
sudo python3 experiments/run_experiments.py --conditions congested_sdn_dynamic --runs 1

# conditions: normal, congested_no_sdn, congested_sdn_static, congested_sdn_dynamic
```

Results: `results/*.csv`, `results/summary.csv`, `results/raw/`, `results/graphs/`.

---

## B. Manual run (for the live demo)

**0. Clean up (normal terminal)**

```bash
sudo mn -c
sudo pkill -f ryu-manager
```

**1. Terminal 1: Ryu controller**

```bash
source ~/ryu-venv/bin/activate
cd ~/sdn_game_project
ryu-manager controller/sdn_controller.py --verbose
```

Wait for `[MODE] dynamic`. Other modes: `PRIORITY=off ryu-manager ...` (no prioritization) and `PRIORITY=on ryu-manager ...` (always on).

**2. Terminal 2: Mininet** (no `sudo mn -c` here)

```bash
cd ~/sdn_game_project
sudo python3 topo/topology.py --controller-port 6653
```

Wait until Ryu prints `[SWITCH JOINED]` for DPID=1 and DPID=2, and `[MONITOR] bottleneck s1-eth5 is OpenFlow port 5`.

**3. At the `mininet>` prompt** (one line at a time)

```
pingall
```
```
source experiments/congested_sdn_dynamic_run1.mn
```

This runs a 30 s test with a flood from 10 to 20 s. Watch the Ryu terminal:

- about 10 s in: `[DYNAMIC] CONGESTION DETECTED ... game queue ON`
- about 12 s after that: `[DYNAMIC] CONGESTION CLEARED ... game queue OFF`

At the end it prints the phase tables, the queue counters (`tc`) and the controller's ON/OFF lines.

Other manual test files in `experiments/`:

| File | Controller mode to start Ryu with |
|---|---|
| `congested_no_sdn_run1.mn` | `PRIORITY=off` |
| `congested_sdn_run1.mn` | `PRIORITY=on` |
| `congested_sdn_dynamic_run1.mn` | default (dynamic) |

**4. Useful checks at `mininet>`**

```
sh ovs-vsctl show
sh ovs-vsctl list qos
sh tc -s class show dev s1-eth5
sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

**5. Stop everything**

At `mininet>`:

```
sh pkill -f game_server.py
exit
```

Then press Ctrl+C in the Ryu terminal, and in a normal terminal:

```bash
sudo mn -c
```

---

## Quick fixes

| Problem | Fix |
|---|---|
| `ryu-manager: command not found` | `source ~/ryu-venv/bin/activate` |
| `pingall` shows X | Ryu is not connected. Check it is running, then redo the clean start (section B, step 0) |
| `Address already in use` (port 5000) | `sh pkill -f game_server.py` |
| `Address already in use` (6653 or 9999) | `sudo pkill -f ryu-manager` |
| Open vSwitch not running | `sudo systemctl restart openvswitch-switch` |
| No `[MONITOR]` line | Read the Ryu warning: it lists the port names it saw |
