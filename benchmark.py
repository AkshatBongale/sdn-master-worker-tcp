#!/usr/bin/env python3
# benchmark.py - runs static vs SDN comparison automatically
# usage: sudo python3 benchmark.py
# outputs go to results/ folder (csv, summary, chart)

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# scenario -> (seconds after dispatch start at which the event is injected, description)
SCENARIOS = {
    'normal':       (None, "no failure (overhead / load-aware steering only)"),
    'link_failure': (4.0,  "link s1-s2 goes down 4 s into the run"),
    'worker_kill':  (3.0,  "Worker-2 process is killed 3 s into the run"),
    'task_hang':    (None, "Worker-3 hangs forever on task 110 (task-timeout path)"),
}
DEFAULT_SCENARIOS = ['normal', 'link_failure', 'worker_kill']
WORKERS = [('h2', 'Worker-2'), ('h3', 'Worker-3'), ('h4', 'Worker-4')]

CSV_FIELDS = ['scenario', 'mode', 'run', 'valid', 'done', 'completed', 'total', 'tct_s',
              'max_stall_s', 'recovery_s', 'avg_rtt_ms', 'task_timeouts', 'workers_dead',
              'overload_events']


# analysis helpers
def load_events(path):
    events = []
    if not os.path.exists(path):
        return events
    with open(path) as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except ValueError:
                pass            # partially written last line
    return events


def analyze(events, inject_ts, total_tasks, timeout_s):
    """Turn the master's event log into the metrics reported in the evaluation."""
    start = next((e for e in events if e['ev'] == 'DISPATCH_START'), None)
    if start is None:
        return None
    t0 = start['ts']
    results = sorted(e['ts'] for e in events if e['ev'] == 'RESULT')
    done = next((e for e in events if e['ev'] == 'DONE'), None)

    # longest period with no task finishing (a stall); if the run never finished, the stall
    # lasts until the deadline
    marks = [t0] + results
    gaps = [b - a for a, b in zip(marks, marks[1:])]
    if done is None:
        gaps.append(max(0.0, (t0 + timeout_s) - marks[-1]))

    recovery = None
    if inject_ts is not None:
        after = [t for t in results if t > inject_ts]
        recovery = round(after[0] - inject_ts, 2) if after else None

    rtts = [e['ms'] for e in events if e['ev'] == 'RTT']
    count = lambda name: sum(1 for e in events if e['ev'] == name)
    return {
        'done': done is not None,
        'completed': len(results),
        'total': total_tasks,
        'tct_s': round(done['ts'] - t0, 2) if done else None,
        'max_stall_s': round(max(gaps), 2) if gaps else 0.0,
        'recovery_s': recovery,
        'avg_rtt_ms': round(sum(rtts) / len(rtts), 1) if rtts else None,
        'task_timeouts': count('TASK_TIMEOUT'),
        'workers_dead': count('WORKER_DEAD'),
        'overload_events': count('WORKER_OVERLOAD'),
    }


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _fmt(v, digits=1):
    return '-' if v is None else f"{v:.{digits}f}"


def summarize(rows):
    """Group rows by (scenario, mode) -> aggregated dict."""
    groups = {}
    for r in rows:
        groups.setdefault((r['scenario'], r['mode']), []).append(r)
    out = {}
    for key, rs in groups.items():
        out[key] = {
            'runs': len(rs),
            'success': sum(1 for r in rs if r['done']),
            'completed': _mean([r['completed'] for r in rs]),
            'total': rs[0]['total'],
            'tct_s': _mean([r['tct_s'] for r in rs]),
            'max_stall_s': _mean([r['max_stall_s'] for r in rs]),
            'recovery_s': _mean([r['recovery_s'] for r in rs]),
            'avg_rtt_ms': _mean([r['avg_rtt_ms'] for r in rs]),
            'task_timeouts': _mean([r['task_timeouts'] for r in rs]),
        }
    return out


def write_summary_md(summary, path, timeout_s):
    lines = ["# Benchmark summary (static routing vs dynamic SDN)", "",
             f"Run timeout: {timeout_s:.0f} s after dispatch start. Values are means over the runs.", "",
             "| Scenario | Mode | Runs finished | Tasks completed | TCT (s) | Longest stall (s) "
             "| Recovery after event (s) | Avg heartbeat RTT (ms) | Task timeouts |",
             "|---|---|---|---|---|---|---|---|---|"]
    for (scenario, mode) in sorted(summary):
        s = summary[(scenario, mode)]
        lines.append(f"| {scenario} | {mode} | {s['success']}/{s['runs']} | "
                     f"{_fmt(s['completed'])}/{s['total']} | {_fmt(s['tct_s'])} | {_fmt(s['max_stall_s'])} | "
                     f"{_fmt(s['recovery_s'], 2)} | {_fmt(s['avg_rtt_ms'])} | {_fmt(s['task_timeouts'])} |")
    lines += ["", "Legend: TCT = task completion time of the whole 20-task batch ('-' = never finished). "
              "Longest stall = longest period with no task finishing. Recovery = time from the injected "
              "event to the next finished task ('-' = no task finished afterwards).", ""]
    with open(path, 'w') as f:
        f.write("\n".join(lines))


def write_chart(summary, path, timeout_s):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception:
        print("[benchmark] matplotlib not installed - skipping chart (sudo apt install python3-matplotlib)")
        return
    scenarios = sorted({k[0] for k in summary})
    modes = ['static', 'sdn']
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    width = 0.35
    for ax, key, title, unit in ((axes[0], 'tct_s', 'Task completion time (20 tasks)', 'seconds'),
                                 (axes[1], 'avg_rtt_ms', 'Average heartbeat RTT', 'ms')):
        for i, mode in enumerate(modes):
            vals, hatch = [], []
            for sc in scenarios:
                s = summary.get((sc, mode))
                v = s[key] if s else None
                failed = key == 'tct_s' and (v is None)
                vals.append(timeout_s if failed else (v or 0))
                hatch.append('//' if failed else None)
            bars = ax.bar([x + (i - 0.5) * width for x in range(len(scenarios))], vals, width,
                          label=mode)
            for b, h in zip(bars, hatch):
                if h:
                    b.set_hatch(h)
        ax.set_xticks(range(len(scenarios)))
        ax.set_xticklabels(scenarios)
        ax.set_ylabel(unit)
        ax.set_title(title)
        ax.legend()
    fig.text(0.01, 0.01, "Hatched bar = did not finish (bar height = timeout)", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    print(f"[benchmark] chart saved to {path}")


# Mininet driver
def find_ryu(arg):
    if arg:
        return arg
    candidates = []
    if os.environ.get('SUDO_USER'):
        candidates.append(f"/home/{os.environ['SUDO_USER']}/sdn_env/bin/ryu-manager")
    candidates += [os.path.expanduser('~/sdn_env/bin/ryu-manager'), shutil.which('ryu-manager')]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    sys.exit("Cannot find ryu-manager. Pass --ryu /path/to/sdn_env/bin/ryu-manager")


def wait_for_text(path, text, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path) and text in open(path, errors='ignore').read():
            return True
        time.sleep(0.3)
    return False


def start_controller(ryu_bin, mode, logpath):
    env = dict(os.environ, SDN_MODE=mode)
    proc = subprocess.Popen([ryu_bin, 'sdn_controller.py'], cwd=HERE, env=env,
                            stdout=open(logpath, 'w'), stderr=subprocess.STDOUT,
                            preexec_fn=os.setsid)
    if not wait_for_text(logpath, 'UNIX socket listening', 25):
        stop_controller(proc)
        raise RuntimeError("Ryu did not start - see " + logpath)
    return proc


def stop_controller(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def run_once(scenario, mode, run_idx, args):
    from mininet.clean import cleanup
    from topology import build_network

    tag = f"{scenario}_{mode}_{run_idx}"
    logdir = os.path.join(args.out, 'logs', tag)
    os.makedirs(logdir, exist_ok=True)
    events_path = os.path.join(HERE, 'events.jsonl')
    ryu_log = os.path.join(logdir, 'ryu.log')
    inject_delay = SCENARIOS[scenario][0]

    print(f"\n[benchmark] ===== {tag} =====")
    cleanup()                                   # NOTE: this kills any running ryu-manager, so do it first
    for stale in ('events.jsonl', 'master.log'):
        if os.path.exists(os.path.join(HERE, stale)):
            os.remove(os.path.join(HERE, stale))

    ryu = net = None
    inject_ts = None
    valid = True
    try:
        ryu = start_controller(args.ryu, mode, ryu_log)
        net = build_network()
        if not wait_for_text(ryu_log, f"({4}/{4})", 20):
            print("[benchmark] WARNING: not all switches connected to Ryu")
            valid = False
        time.sleep(2)                           # let the proactive flows be installed
        if net.pingAll() > 0:
            print("[benchmark] WARNING: pingAll shows packet loss before the run - run marked invalid")
            valid = False

        h1 = net.get('h1')
        h1.cmd(f"python3 -u master.py 3 {args.tasks} > master.log 2>&1 &")
        for host, wid in WORKERS:
            env = "HANG_TASK_ID=110 " if (scenario == 'task_hang' and wid == 'Worker-3') else ""
            net.get(host).cmd(f"{env}python3 -u worker.py {wid} > {host}.log 2>&1 &")

        # wait until dispatching has started
        t_end = time.time() + 30
        while time.time() < t_end and not any(e['ev'] == 'DISPATCH_START' for e in load_events(events_path)):
            time.sleep(0.3)
        t0 = time.time()

        if inject_delay is not None:
            time.sleep(inject_delay)
            inject_ts = time.time()
            if scenario == 'link_failure':
                print("[benchmark] >>> injecting: link s1-s2 DOWN")
                net.configLinkStatus('s1', 's2', 'down')
            elif scenario == 'worker_kill':
                print("[benchmark] >>> injecting: killing Worker-2")
                net.get('h2').cmd('pkill -f "worker.py Worker-2"')

        # wait for DONE or the deadline
        while time.time() - t0 < args.timeout:
            if any(e['ev'] == 'DONE' for e in load_events(events_path)):
                break
            time.sleep(0.5)

        metrics = analyze(load_events(events_path), inject_ts, args.tasks, args.timeout)
        if metrics is None:
            raise RuntimeError("master never started dispatching (workers did not register?)")
    finally:
        try:
            for name in ('master.log', 'events.jsonl', 'h2.log', 'h3.log', 'h4.log'):
                src = os.path.join(HERE, name)
                if os.path.exists(src):
                    shutil.copy(src, os.path.join(logdir, name))
        except Exception:
            pass
        if net is not None:
            try:
                net.get('h1').cmd('pkill -f master.py')
                for host, _ in WORKERS:
                    net.get(host).cmd('pkill -f worker.py')
                net.stop()
            except Exception:
                pass
        if ryu is not None:
            stop_controller(ryu)

    row = {'scenario': scenario, 'mode': mode, 'run': run_idx, 'valid': valid}
    row.update(metrics)
    print(f"[benchmark] result: done={row['done']} completed={row['completed']}/{row['total']} "
          f"tct={row['tct_s']} stall={row['max_stall_s']} recovery={row['recovery_s']} rtt={row['avg_rtt_ms']}")
    return row


def main():
    ap = argparse.ArgumentParser(description="static vs SDN benchmark runner")
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--tasks', type=int, default=20)
    ap.add_argument('--timeout', type=float, default=45.0, help='seconds after dispatch start before giving up')
    ap.add_argument('--scenarios', default=','.join(DEFAULT_SCENARIOS),
                    help='comma list from: ' + ', '.join(SCENARIOS))
    ap.add_argument('--modes', default='static,sdn')
    ap.add_argument('--ryu', default=None, help='path to ryu-manager inside your venv')
    ap.add_argument('--out', default=os.path.join(HERE, 'results'))
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo: sudo python3 benchmark.py")
    from mininet.log import setLogLevel
    setLogLevel('warning')
    os.chdir(HERE)
    args.ryu = find_ryu(args.ryu)
    os.makedirs(args.out, exist_ok=True)

    scenarios = [s.strip() for s in args.scenarios.split(',') if s.strip()]
    modes = [m.strip() for m in args.modes.split(',') if m.strip()]
    for s in scenarios:
        if s not in SCENARIOS:
            sys.exit(f"unknown scenario {s}")

    rows = []
    csv_path = os.path.join(args.out, 'results.csv')
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for scenario in scenarios:
            for run_idx in range(1, args.runs + 1):
                for mode in modes:                     # alternate modes to cancel out drift
                    try:
                        row = run_once(scenario, mode, run_idx, args)
                    except Exception as e:
                        print(f"[benchmark] run {scenario}/{mode}/{run_idx} FAILED: {e}")
                        continue
                    writer.writerow({k: row.get(k) for k in CSV_FIELDS})
                    f.flush()
                    rows.append(row)

    summary = summarize(rows)
    write_summary_md(summary, os.path.join(args.out, 'summary.md'), args.timeout)
    write_chart(summary, os.path.join(args.out, 'chart.png'), args.timeout)
    print(f"\n[benchmark] finished. See {args.out}/summary.md and results.csv")


if __name__ == '__main__':
    main()
