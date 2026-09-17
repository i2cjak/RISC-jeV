"""Drive SERV with a fresh model choice for every combinational gate evaluation."""

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from jev import MODEL, JevError, answer_bit, payload, request

ROOT = Path(__file__).resolve().parent
MAX_SECONDS = 300.0
PRICE_PER_MILLION = 0.042
MAX_INPUT_COST = 0.05
MAX_INPUT_TOKENS = int(MAX_INPUT_COST * 1000000 / PRICE_PER_MILLION)


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
    def __init__(self, binary, emit, permission=lambda: True, model=MODEL, max_cycles=250000):
        self.binary = binary
        self.emit = emit
        self.permission = permission
        self.model = model
        self.max_cycles = max_cycles
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

    def run(self):
        self.started = time.monotonic()
        try:
            self.process = subprocess.Popen([
                str(ROOT / "build/serv_sim"), str(self.binary), str(self.max_cycles),
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            threading.Thread(target=self.watchdog, daemon=True).start()
            for line in self.process.stdout:
                if not self.check():
                    break
                event = json.loads(line)
                if event["type"] != "gates":
                    self.emit(event)
                    continue
                if not self.permission() or not self.check():
                    break
                batch = event.pop("batch")
                event.pop("type")
                body = payload(batch, self.model)
                reservation = input_reservation(body)
                if self.input_tokens + reservation > MAX_INPUT_TOKENS:
                    raise CostLimit("$0.05 estimated input budget: stopped before the next batch.")
                self.emit({"type": "request", "request": body, "batch": batch,
                           "reserved_input_cost_usd": reservation * PRICE_PER_MILLION / 1000000, **event})
                reply = self.classify(body)
                if reply is None:
                    break
                response, duration = reply
                tokens = response.get("usage", {}).get("input_tokens")
                if type(tokens) is not int or tokens < 0:
                    raise JevError("Jev returned no input-token usage; stopped to enforce the run budget.")
                self.input_tokens += tokens
                bits = [answer_bit(response["answers"].get(f"g_{gate['id']}"), f"g_{gate['id']}") for gate in batch]
                if not self.check():
                    break
                self.process.stdin.write("".join(map(str, bits)) + "\n")
                self.process.stdin.flush()
                self.emit({
                    **event, "type": "response", "response": response, "duration": duration,
                    "results": [dict(gate, output=bit) for gate, bit in zip(batch, bits)],
                    "gates": event["gates"] + len(bits),
                    "input_tokens": tokens, "input_cost_usd": tokens * PRICE_PER_MILLION / 1000000,
                    "input_price_per_million": PRICE_PER_MILLION,
                })
                if self.input_tokens >= MAX_INPUT_TOKENS:
                    raise CostLimit("$0.05 estimated input budget reached.")
            self.check()
            if not self.stopped.is_set():
                self.process.stdin.close()
                self.process.wait(timeout=1)
                if self.process.returncode:
                    raise RuntimeError(self.process.stderr.read().strip() or "Simulator exited with an error")
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
