# Demo & Test Case Guide

> **Always follow the standard setup before every TC.**

---

## Standard Setup

**Terminal 1 — Controller:**
```bash
sudo mn -c
source ~/sdn_env/bin/activate
ryu-manager sdn_controller.py
```

**Terminal 2 — Mininet:**
```bash
sudo python3 topology.py
```

Wait for `Diamond Topology Ready. Entering Mininet CLI.` and Ryu showing `(4/4)` switches before running any TC.

**After every TC:**
```bash
sudo mn -c
```
This kills Ryu too — always restart Ryu after `mn -c`.

---

## TC1 — Connectivity

**Goal:** Verify all hosts reach each other through the SDN network.

```bash
mininet> source testcases/tc1.cmds
```

**Expected:** `0% dropped (12/12 received)`

---

## TC2 — Registration Barrier

**Goal:** Master waits for all 3 workers before dispatching any task.

```bash
mininet> source testcases/tc2.cmds
```

**Expected:** `(1/3)... (2/3)... (3/3)` then `dispatching 20 tasks` — no tasks sent before all 3 register.

---

## TC3 — Load Balancing

**Goal:** Tasks are distributed evenly across all 3 workers.

```bash
mininet> source testcases/tc3.cmds
```

**Expected:** `[DONE] All tasks finished` with a roughly even split e.g. `7/7/6`.

---

## TC4 — Worker Crash & Recovery

**Goal:** Master detects a dead worker, re-queues its tasks, SDN quarantines it.

```bash
mininet> source testcases/tc4.cmds
```

**Watch:** Ryu terminal + master.log printed at the end.

**Expected:** `[RECOVERY]` tasks re-queued. Ryu: `[WORKER DEAD]` quarantine for 10s. All 20 tasks complete.

---

## TC5 — Heartbeat Timeout

**Goal:** Master detects an unresponsive worker via missed heartbeats and removes it.

```bash
mininet> source testcases/tc5.cmds
```

**Expected:** `[ALERT] Worker-3 timed out! Marking as DEAD.` Tasks re-queued to remaining workers.

---

## TC6 — Task Timeout

**Goal:** Master re-queues a task that a worker hangs on indefinitely.

> Uses `HANG_TASK_ID=110` env var to make Worker-3 hang on task 110.

```bash
mininet> source testcases/tc6.cmds
```

**Expected:** `[TASK TIMEOUT] Task 110 on Worker-3 exceeded 10s — re-queued.` All 20 tasks complete.

---

## TC7 — Link Failure & Reroute

**Goal:** SDN reroutes traffic instantly when the primary link s1-s2 goes down.

```bash
mininet> source testcases/tc7.cmds
```

**Watch:** Ryu terminal + master.log printed at the end.

**Expected:** Ryu: `[LINK DOWN] s1<->s2` → `[REROUTE]` via s3 path `[1,3,4]`. All 20 tasks complete.

---

## TC8 — Link Restore

**Goal:** SDN restores the default path when the failed link comes back up.

```bash
mininet> source testcases/tc8.cmds
```

**Watch:** Ryu terminal.

**Expected:** Ryu: `[LINK DOWN]` then `[LINK UP] s1<->s2`. Paths restored to default `[1,2,4]`.

---

## TC9 — Load-Aware Path Steering

**Goal:** SDN switches overloaded workers to the low-delay path via s3.

```bash
mininet> source testcases/tc9.cmds
```

**Watch:** Ryu terminal while tasks run.

**Expected:** Ryu: `[SOCKET EVENT] Worker OVERLOADED — steering to low-delay path [1,3,4]`. When back to normal: path reverts to `[1,2,4]`.

---

## TC10 — Controller Absent

**Goal:** System works gracefully even without Ryu running.

> **Skip Terminal 1 entirely — do NOT start ryu-manager.** Only run `topology.py`.

```bash
mininet> source testcases/tc10.cmds
```

**Expected:** `Controller not reachable; continuing without SDN.` All 20 tasks still complete.

---

## Performance Benchmark

> Do **not** start Ryu manually — the script does it automatically.

```bash
sudo python3 benchmark.py
```

Results land in `results/results.csv`, `results/summary.md`, and `results/chart.png`.
