import socket
import json
import time
import threading
import sys
import queue

HOST      = '10.0.0.1'   # master IP inside Mininet
PORT      = 8888
WORKER_ID = sys.argv[1] if len(sys.argv) > 1 else "Worker-1"

send_lock    = threading.Lock()
last_rtt_ms  = None
task_inbox   = queue.Queue()


def send_msg(sock, msg):
    with send_lock:
        sock.sendall((json.dumps(msg) + '\n').encode())


def connect_with_retry():
    while True:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect((HOST, PORT))
            return client
        except OSError as e:
            client.close()
            print(f"[*] {WORKER_ID} waiting for master at {HOST}:{PORT} ({e}). Retrying in 1s...")
            time.sleep(1)


def send_heartbeats(sock):
    while True:
        try:
            time.sleep(2)
            send_msg(sock, {"type": "HEARTBEAT", "worker_id": WORKER_ID,
                            "ts": time.time(), "rtt_ms": last_rtt_ms})
        except Exception:
            break


def process_tasks(sock):
    while True:
        task    = task_inbox.get()
        task_id = task.get("task_id")
        numbers = task.get("numbers")
        print(f"[{WORKER_ID}] Processing task {task_id} with data {numbers}...")
        time.sleep(1)
        result = sum(numbers)
        try:
            send_msg(sock, {"type": "RESULT", "task_id": task_id, "result": result})
        except Exception:
            return
        print(f"[{WORKER_ID}] Sent result for task {task_id}.")


def start_worker():
    global last_rtt_ms
    client = connect_with_retry()
    print(f"[*] {WORKER_ID} connected to Master Server at {HOST}:{PORT}")

    send_msg(client, {"type": "REGISTER", "worker_id": WORKER_ID})
    threading.Thread(target=send_heartbeats, args=(client,), daemon=True).start()
    threading.Thread(target=process_tasks,   args=(client,), daemon=True).start()

    f = client.makefile('r')
    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("type") == "HEARTBEAT_ACK":
                last_rtt_ms = (time.time() - msg["ts"]) * 1000.0
            else:
                task_inbox.put(msg)
    except Exception as e:
        print(f"[-] {WORKER_ID} disconnected: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    start_worker()
