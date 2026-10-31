import socket
import threading
import json
import time
import sys
import os

HOST = '0.0.0.0'
PORT = 8888

MIN_WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
NUM_TASKS   = int(sys.argv[2]) if len(sys.argv) > 2 else 20

DISPATCH_INTERVAL = 0.2
HEARTBEAT_TIMEOUT = 6
TASK_TIMEOUT      = 10
MAX_INFLIGHT      = 3
OVERLOAD_HIGH     = 3
OVERLOAD_LOW      = 1

SDN_SOCKET  = '/tmp/sdn_events.sock'
EVENTS_PATH = 'events.jsonl'

active_workers    = {}
task_queue        = [{"task_id": 100 + i, "numbers": [i * 10, i * 20]} for i in range(1, NUM_TASKS + 1)]
in_progress_tasks = {}
completed_by      = {}
dispatch_start    = None
workers_lock      = threading.Lock()

events_lock = threading.Lock()
events_file = open(EVENTS_PATH, 'w', buffering=1)


def log_event(ev, **fields):
    rec = {"ts": round(time.time(), 4), "ev": ev}
    rec.update(fields)
    with events_lock:
        events_file.write(json.dumps(rec) + "\n")


def notify_sdn_controller(event_type, worker_ip):
    def _send():
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(SDN_SOCKET)
            sock.sendall((json.dumps({"event": event_type, "ip": worker_ip}) + '\n').encode())
            sock.close()
            print(f"[SDN NOTIFY] Sent {event_type} for worker {worker_ip} to controller.")
        except Exception as e:
            print(f"[SDN NOTIFY] Controller not reachable ({e}); continuing without SDN.")
    threading.Thread(target=_send, daemon=True).start()


def send_to(info, msg):
    with info["send_lock"]:
        info["conn"].sendall((json.dumps(msg) + '\n').encode())


def update_load_state(worker_id, info):
    load = info["active_tasks"]
    if not info["overloaded"] and load >= OVERLOAD_HIGH:
        info["overloaded"] = True
        print(f"[LOAD] {worker_id} OVERLOADED (in-flight={load})")
        log_event("WORKER_OVERLOAD", worker=worker_id, load=load)
        notify_sdn_controller("WORKER_OVERLOAD", info["ip"])
    elif info["overloaded"] and load <= OVERLOAD_LOW:
        info["overloaded"] = False
        print(f"[LOAD] {worker_id} back to NORMAL (in-flight={load})")
        log_event("WORKER_NORMAL", worker=worker_id, load=load)
        notify_sdn_controller("WORKER_NORMAL", info["ip"])


def remove_worker(worker_id, reason, conn=None):
    info = active_workers.get(worker_id)
    if info is None:
        return
    if conn is not None and info["conn"] is not conn:
        return

    del active_workers[worker_id]
    log_event("WORKER_DEAD", worker=worker_id, reason=reason)
    if info.get("ip"):
        notify_sdn_controller("WORKER_DEAD", info["ip"])

    for t_id, t_info in list(in_progress_tasks.items()):
        if t_info["worker_id"] == worker_id:
            print(f"[RECOVERY] Re-queueing incomplete task {t_id} from dead {worker_id}")
            task_queue.insert(0, t_info["task_data"])
            del in_progress_tasks[t_id]
    try:
        info["conn"].close()
    except Exception:
        pass
    print(f"[-] Worker {worker_id} removed from active pool ({reason}).")


def assign_tasks():
    global dispatch_start
    while True:
        with workers_lock:
            ready = len(active_workers)
        if ready >= MIN_WORKERS:
            break
        time.sleep(0.2)
    dispatch_start = time.time()
    log_event("DISPATCH_START", tasks=len(task_queue), workers=ready)
    print(f"[*] {ready} workers registered - dispatching {len(task_queue)} tasks")

    while True:
        time.sleep(DISPATCH_INTERVAL)
        with workers_lock:
            if not task_queue:
                continue
            eligible = [w for w, i in active_workers.items()
                        if i["active_tasks"] < MAX_INFLIGHT and not i["suspect"]]
            if not eligible:
                continue
            target = min(eligible, key=lambda w: (active_workers[w]["active_tasks"],
                                                   active_workers[w]["assigned_total"]))
            info    = active_workers[target]
            task    = task_queue.pop(0)
            task_id = task["task_id"]
            in_progress_tasks[task_id] = {"worker_id": target, "task_data": task,
                                           "assigned_at": time.time()}
            info["active_tasks"]   += 1
            info["assigned_total"] += 1
            try:
                send_to(info, task)
                print(f"[ASSIGNED] Task {task_id} sent to {target} (Load: {info['active_tasks']})")
                update_load_state(target, info)
            except Exception as e:
                print(f"[-] Failed to send task {task_id} to {target}: {e}")
                info["active_tasks"]   -= 1
                info["assigned_total"] -= 1
                task_queue.insert(0, task)
                del in_progress_tasks[task_id]


def handle_worker(conn, addr):
    worker_id = None
    print(f"[+] Connected to worker at {addr}")
    f = conn.makefile('r')
    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            message  = json.loads(line)
            msg_type = message.get("type")

            if msg_type == "REGISTER":
                worker_id = message.get("worker_id")
                peer_ip   = addr[0]
                with workers_lock:
                    active_workers[worker_id] = {
                        "conn": conn, "ip": peer_ip, "last_seen": time.time(),
                        "active_tasks": 0, "assigned_total": 0,
                        "send_lock": threading.Lock(),
                        "overloaded": False, "suspect": False,
                    }
                    count = len(active_workers)
                print(f"[REGISTERED] Worker {worker_id} at {peer_ip}. ({count}/{MIN_WORKERS} needed)")

            elif msg_type == "HEARTBEAT":
                worker_id = message.get("worker_id")
                with workers_lock:
                    info = active_workers.get(worker_id)
                    if info:
                        info["last_seen"] = time.time()
                if info:
                    try:
                        send_to(info, {"type": "HEARTBEAT_ACK", "ts": message.get("ts")})
                    except Exception:
                        pass
                    if message.get("rtt_ms") is not None:
                        log_event("RTT", worker=worker_id, ms=round(message["rtt_ms"], 2))

            elif msg_type == "RESULT":
                task_id = message.get("task_id")
                result  = message.get("result")
                with workers_lock:
                    t_info = in_progress_tasks.get(task_id)
                    if t_info and t_info["worker_id"] == worker_id:
                        print(f"[RESULT] Task {task_id} completed by {worker_id}. Result: {result}")
                        info = active_workers.get(worker_id)
                        if info:
                            info["active_tasks"] = max(0, info["active_tasks"] - 1)
                            info["suspect"]      = False
                            update_load_state(worker_id, info)
                        del in_progress_tasks[task_id]
                        completed_by[worker_id] = completed_by.get(worker_id, 0) + 1
                        log_event("RESULT", task=task_id, worker=worker_id)
                        if not task_queue and not in_progress_tasks and dispatch_start:
                            elapsed = time.time() - dispatch_start
                            log_event("DONE", tct=round(elapsed, 3), per_worker=completed_by)
                            print(f"[DONE] All tasks finished in {elapsed:.1f}s | per worker: {completed_by}")
                    else:
                        print(f"[IGNORED] Late/duplicate result for task {task_id} from {worker_id}")

    except Exception as e:
        print(f"[-] Connection dropped for {worker_id}: {e}")
    finally:
        if worker_id:
            with workers_lock:
                remove_worker(worker_id, "connection closed", conn)
        conn.close()


def monitor_worker_health():
    while True:
        time.sleep(3)
        now = time.time()
        with workers_lock:
            dead = [w for w, i in active_workers.items()
                    if now - i["last_seen"] > HEARTBEAT_TIMEOUT]
            for worker_id in dead:
                print(f"\n[ALERT] Worker {worker_id} timed out! Marking as DEAD.")
                remove_worker(worker_id, "heartbeat timeout")


def monitor_task_timeouts():
    while True:
        time.sleep(1)
        now = time.time()
        with workers_lock:
            for t_id, t_info in list(in_progress_tasks.items()):
                if now - t_info["assigned_at"] > TASK_TIMEOUT:
                    w    = t_info["worker_id"]
                    info = active_workers.get(w)
                    print(f"[TASK TIMEOUT] Task {t_id} on {w} exceeded {TASK_TIMEOUT:.0f}s - re-queueing")
                    log_event("TASK_TIMEOUT", task=t_id, worker=w)
                    if info:
                        info["active_tasks"] = max(0, info["active_tasks"] - 1)
                        info["suspect"]      = True
                        update_load_state(w, info)
                    task_queue.insert(0, t_info["task_data"])
                    del in_progress_tasks[t_id]


def start_master():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(5)
    print(f"[*] Master listening on {HOST}:{PORT} | waiting for {MIN_WORKERS} workers, {NUM_TASKS} tasks queued")
    threading.Thread(target=monitor_worker_health, daemon=True).start()
    threading.Thread(target=monitor_task_timeouts, daemon=True).start()
    threading.Thread(target=assign_tasks,          daemon=True).start()
    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_worker, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    start_master()
