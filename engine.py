"""Drive SERV using cached Boolean lookup tables supplied by Jev."""

import json
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from jev import (
    GATES,
    MODEL,
    JevError,
    load_cache,
    payload,
    request,
    save_cache,
    table_cases,
    table_values,
)

ROOT = Path(__file__).resolve().parent
MAX_SECONDS = 300.0
PRICE_PER_MILLION = 0.042
MAX_INPUT_COST = 0.05
MAX_INPUT_TOKENS = int(MAX_INPUT_COST * 1000000 / PRICE_PER_MILLION)
CACHE_FILE = ROOT / "build/jev_truth_tables.json"
classifier_lock = threading.Lock()


class ExecutionTimeout(RuntimeError):
    pass


class CostLimit(RuntimeError):
    pass


def input_reservation(body):
    # Budget before sending. Use UTF-8 bytes (not optimistic characters/4),
    # allow the state to be charged for every question, and leave room for
    # provider framing. This is an estimate; billing belongs to the provider.
    return len(json.dumps(body).encode()) + len(body["questions"]) * (len(body["state"].encode()) + 512)


class Simulation:
    def __init__(self, binary, emit, permission=lambda: True, model=MODEL, max_cycles=250000, fresh=False):
        self.binary = binary
        self.emit = emit
        self.permission = permission
        self.model = model
        self.max_cycles = max_cycles
        self.fresh = fresh
        self.process = None
        self.started = None
        self.pause_started = None
        self.paused_seconds = 0
        self.finished_at = None
        self.stopped = threading.Event()
        self.changed = threading.Condition()
        self.timed_out = False
        self.input_tokens = 0

    def elapsed(self):
        if self.started is None:
            return 0.0
        now = self.finished_at or time.monotonic()
        pause = now - self.pause_started if self.pause_started is not None else 0
        return max(0.0, now - self.started - self.paused_seconds - pause)

    def pause(self, paused):
        with self.changed:
            if paused and self.pause_started is None:
                self.pause_started = time.monotonic()
            elif not paused and self.pause_started is not None:
                self.paused_seconds += time.monotonic() - self.pause_started
                self.pause_started = None
            self.changed.notify_all()

    def stop(self):
        self.stopped.set()
        with self.changed:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
            self.changed.notify_all()

    def check(self):
        if self.timed_out or self.elapsed() >= MAX_SECONDS:
            raise ExecutionTimeout("5-minute execution limit reached (including Jev requests).")
        return not self.stopped.is_set()

    def watchdog(self):
        with self.changed:
            while not self.stopped.is_set():
                remaining = MAX_SECONDS - self.elapsed()
                if self.pause_started is None and remaining <= 0:
                    self.timed_out = True
                    self.stop()
                    return
                self.changed.wait(timeout=max(0.001, remaining) if self.pause_started is None else None)

    def classify(self, body):
        # A daemon I/O worker lets Stop/the hard deadline release the simulator
        # even if the remote connection stalls. Late replies never drive wires.
        replies = queue.Queue(maxsize=1)
        remaining = max(0.001, MAX_SECONDS - self.elapsed())

        def fetch():
            try:
                replies.put((request(body, timeout=min(15, remaining)), None))
            except (JevError, OSError, ValueError) as error:
                replies.put((None, error))

        threading.Thread(target=fetch, daemon=True).start()
        while self.check():
            try:
                value, error = replies.get(timeout=min(0.025, max(0.001, MAX_SECONDS - self.elapsed())))
            except queue.Empty:
                continue
            if not self.check():
                return None
            if error:
                raise error
            return value
        return None

    def lookup_tables(self):
        batch = table_cases()
        body = payload(batch, self.model)
        while not classifier_lock.acquire(timeout=0.025):
            if not self.check():
                return None
        try:
            if not self.check():
                return None
            record = None if self.fresh else load_cache(CACHE_FILE, body)
            cached = record is not None
            source = "cache" if cached else "api"
            if not cached and input_reservation(body) > MAX_INPUT_TOKENS:
                raise CostLimit("$0.05 estimated input budget: request would exceed the limit.")
            self.emit({"type": "request", "source": source, "request": body, "batch": batch})
            if not cached:
                reply = self.classify(body)
                if reply is None:
                    return None
                response, duration = reply
                tokens = response.get("usage", {}).get("input_tokens")
                if type(tokens) is not int or tokens < 0:
                    raise JevError("Jev returned no input-token usage; stopped to enforce the run budget.")
                self.input_tokens = tokens
                record = save_cache(CACHE_FILE, body, response, duration)
            response = record["response"]
            tables = table_values(response)
            self.emit({
                "type": "response", "source": source, "response": response,
                "duration": 0 if cached else record["elapsed_seconds"], "created_at": record["created_at"],
                "results": [dict(gate, output=tables[gate["gate"]][int(gate["bits"], 2)]) for gate in batch],
                "input_tokens": self.input_tokens, "input_cost_usd": self.input_tokens * PRICE_PER_MILLION / 1000000,
                "input_price_per_million": PRICE_PER_MILLION,
            })
            if self.input_tokens > MAX_INPUT_TOKENS:
                raise CostLimit("$0.05 estimated input budget reached.")
            return tables
        finally:
            classifier_lock.release()

    def run(self):
        self.started = time.monotonic()
        threading.Thread(target=self.watchdog, daemon=True).start()
        try:
            tables = self.lookup_tables()
            if tables is None or not self.check():
                return
            with tempfile.TemporaryDirectory(prefix="jev-run-") as directory:
                table_file = Path(directory) / "tables.txt"
                table_file.write_text("\n".join(" ".join(map(str, tables[gate])) for gate in GATES) + "\n")
                self.process = subprocess.Popen([
                    str(ROOT / "build/serv_sim"), str(self.binary), str(table_file), str(self.max_cycles),
                ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                for line in self.process.stdout:
                    if not self.check():
                        break
                    event = json.loads(line)
                    self.emit(event)
                    if event["type"] == "fetch":
                        if not self.permission() or not self.check():
                            break
                        self.process.stdin.write("go\n")
                        self.process.stdin.flush()
                self.check()
                if not self.stopped.is_set():
                    self.process.stdin.close()
                    self.process.wait(timeout=1)
                    if self.process.returncode:
                        detail = self.process.stderr.read().strip()
                        if self.process.returncode == 2:
                            raise ExecutionTimeout(detail or "Cycle limit reached.")
                        raise RuntimeError(detail or "Simulator exited with an error")
        finally:
            self.finished_at = time.monotonic()
            self.stop()
            if self.process is not None:
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if pipe and not pipe.closed:
                        pipe.close()
