"""Web UI for the real SERV gate simulator. Run with one threaded worker."""

import json
import math
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from flask import Flask, Response, abort, render_template, request, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

from compiler import CompileError, compile_source
from engine import MAX_INPUT_COST, MAX_SECONDS, CostLimit, ExecutionTimeout, Simulation
from jev import JevError

ROOT = Path(__file__).resolve().parent
PROGRAMS = {"fibonacci": "Fibonacci", "sum": "Sum", "bitwise": "Bitwise", "demo": "Checks"}
app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=16000, SEND_FILE_MAX_AGE_DEFAULT=86400)
if os.environ.get("RAILWAY_ENVIRONMENT_ID"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
jobs = {}
jobs_lock = threading.Lock()
compiler_lock = threading.Lock()
rate_limits = {}
compile_limits = {}
compile_times = deque()
COMPILE_COOLDOWN = 30
COMPILES_PER_MINUTE = 20


def compilation_delay(visitor, now):
    while compile_times and now - compile_times[0] >= 60:
        compile_times.popleft()
    remaining = max(0, COMPILE_COOLDOWN - (now - compile_limits.get(visitor, -COMPILE_COOLDOWN)))
    if len(compile_times) >= COMPILES_PER_MINUTE:
        remaining = max(remaining, 60 - (now - compile_times[0]))
    return math.ceil(remaining)


def program_data():
    return [{"id": name, "name": label, "source": (ROOT / f"firmware/{name}.c").read_text()} for name, label in PROGRAMS.items()]


class Run:
    def __init__(self, program, mode, source=None):
        self.id = uuid.uuid4().hex
        self.program = program
        self.source = source
        self.binary = ROOT / f"build/{program}.bin"
        self.mode = mode
        self.credits = 1 if mode == "step" else 0
        self.condition = threading.Condition()
        self.events = deque(maxlen=12000)
        self.sequence = 0
        self.last_wave_event = None
        self.done = False
        self.cancelled = False
        self.started = time.monotonic()
        self.paused_seconds = 0
        self.pause_start = None
        self.simulation = None
        self.state = "compiling" if source is not None else "running"
        self.cycle = 0
        self.fetched = 0
        self.disassembly = {}
        if source is None:
            self.load_disassembly((ROOT / f"build/{program}.dis").read_text())

    def load_disassembly(self, text):
        for line in text.splitlines():
            match = re.match(r"\s*([0-9a-f]+):\s+([0-9a-f]{8})\s+(.+)", line)
            if match:
                self.disassembly[int(match[1], 16)] = match[3].replace("\t", " ")

    def elapsed(self):
        if self.simulation is not None:
            return self.simulation.elapsed()
        now = time.monotonic()
        current_pause = now - self.pause_start if self.pause_start is not None else 0
        return max(0, now - self.started - self.paused_seconds - current_pause)

    def emit(self, event):
        with self.condition:
            self.sequence += 1
            event = dict(event, seq=self.sequence, elapsed=self.elapsed())
            if "wave" in event:
                if self.last_wave_event is not None:
                    self.last_wave_event.pop("wave", None)
                self.last_wave_event = event
            self.events.append(event)
            self.condition.notify_all()

    def control(self, action):
        with self.condition:
            if self.done:
                return
            if action == "stop":
                self.cancelled = True
                if self.simulation is not None:
                    self.simulation.stop()
            elif action == "run":
                self.mode = "run"
                self.credits = 0
            elif action == "pause":
                self.mode = "pause"
                self.credits = 0
            elif action == "step":
                self.mode = "step"
                self.credits += 1
            self.condition.notify_all()

    def wait_for_permission(self):
        with self.condition:
            while not self.cancelled and self.mode != "run" and self.credits == 0:
                if self.pause_start is None:
                    self.pause_start = time.monotonic()
                    self.simulation.pause(True)
                    self.state = "paused"
                    self.emit({"type": "status", "state": "paused", "cycle": self.cycle})
                self.condition.wait(timeout=30)
                if time.monotonic() - self.pause_start > 1200:
                    self.cancelled = True
            if self.pause_start is not None:
                self.paused_seconds += time.monotonic() - self.pause_start
                self.pause_start = None
                self.simulation.pause(False)
            if self.cancelled:
                return False
            if self.credits:
                self.credits -= 1
            if self.state != "running":
                self.state = "running"
                self.emit({"type": "status", "state": "running", "cycle": self.cycle})
            return True

    def simulation_event(self, event):
        if "cycle" in event:
            self.cycle = event["cycle"]
        if event["type"] == "fetch":
            self.fetched += 1
            event["assembly"] = self.disassembly.get(event["pc"], "?")
            event["fetched"] = self.fetched
        self.emit(event)

    def work(self):
        try:
            if self.source is not None:
                self.emit({"type": "status", "state": "compiling"})
                with compiler_lock:
                    if self.cancelled:
                        return
                    self.binary, disassembly = compile_source(self.source, self.id)
                    self.load_disassembly(disassembly)
                self.emit({"type": "compiled", "bytes": self.binary.stat().st_size})
            if self.cancelled:
                return
            self.simulation = Simulation(self.binary, self.simulation_event, self.wait_for_permission)
            self.state = "running"
            self.emit({"type": "status", "state": "running"})
            self.simulation.run()
            self.state = "stopped" if self.cancelled else "complete"
        except (ExecutionTimeout, CostLimit) as error:
            self.state = "limit"
            self.emit({"type": "limit", "message": str(error)})
        except (JevError, CompileError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            self.state = "stopped" if self.cancelled else "error"
            if not self.cancelled:
                self.emit({"type": "error", "message": str(error)})
        finally:
            if self.source is not None and self.binary.name.startswith("custom-"):
                self.binary.unlink(missing_ok=True)
            if self.cancelled:
                self.state = "stopped"
            with self.condition:
                self.emit({"type": "done", "state": self.state, "cycle": self.cycle, "fetched": self.fetched})
                self.done = True
                self.condition.notify_all()


@app.before_request
def same_origin_writes():
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            abort(403)
        if not request.is_json:
            abort(415)


@app.after_request
def headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    version = max((ROOT / "static/app.js").stat().st_mtime_ns, (ROOT / "static/style.css").stat().st_mtime_ns)
    return render_template("index.html", programs=program_data(), version=version,
                           max_minutes=int(MAX_SECONDS / 60), input_budget=f"${MAX_INPUT_COST:.2f}")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/runs")
def start_run():
    data = request.get_json()
    if not isinstance(data, dict) or data.get("program") not in PROGRAMS:
        abort(400, "Unknown program")
    if data.get("mode", "run") not in ("run", "step"):
        abort(400, "Invalid mode")
    source = data.get("source")
    if source is not None and (not isinstance(source, str) or not source.strip() or len(source.encode()) > 12000):
        abort(400, "Enter a C program up to 12 KB")
    with jobs_lock:
        now = time.monotonic()
        visitor = request.remote_addr or "unknown"
        if source is not None:
            delay = compilation_delay(visitor, now)
            if delay:
                return {"error": f"Compilation cooldown: try again in {delay}s.", "retry_after": delay}, 429, {"Retry-After": str(delay)}
        recent = [stamp for stamp in rate_limits.get(visitor, []) if now - stamp < 60]
        if len(recent) >= 12:
            abort(429, "Limit: 12 runs per minute. Try again shortly.")
        if len(rate_limits) > 10000:
            rate_limits.clear()
        rate_limits[visitor] = recent + [now]
        if sum(not job.done for job in jobs.values()) >= 6:
            abort(429, "Too many active runs; stop an existing run first")
        for identifier in list(jobs):
            if len(jobs) >= 24 and jobs[identifier].done:
                del jobs[identifier]
        job = Run(data["program"], data.get("mode", "run"), source)
        jobs[job.id] = job
        if source is not None:
            if len(compile_limits) > 10000:
                compile_limits.clear()
            compile_limits[visitor] = now
            compile_times.append(now)
    threading.Thread(target=job.work, daemon=True).start()
    return {"id": job.id, "compile_cooldown_seconds": COMPILE_COOLDOWN if source is not None else 0}, 201


@app.get("/api/limits")
def limits():
    with jobs_lock:
        return {"compile_retry_after": compilation_delay(request.remote_addr or "unknown", time.monotonic()),
                "max_execution_seconds": MAX_SECONDS, "max_input_cost_usd": MAX_INPUT_COST}


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(413)
@app.errorhandler(429)
def api_error(error):
    return {"error": error.description}, error.code


@app.post("/api/runs/<identifier>/control")
def control(identifier):
    job = jobs.get(identifier)
    if job is None:
        abort(404)
    data = request.get_json()
    if not isinstance(data, dict) or data.get("action") not in ("run", "pause", "step", "stop"):
        abort(400, "Unknown action")
    job.control(data["action"])
    return {"ok": True}


@app.get("/api/runs/<identifier>/events")
def events(identifier):
    job = jobs.get(identifier)
    if job is None:
        abort(404)
    try:
        after = max(0, int(request.headers.get("Last-Event-ID", 0)))
    except ValueError:
        abort(400)

    @stream_with_context
    def stream():
        nonlocal after
        while True:
            with job.condition:
                batch = [event for event in job.events if event["seq"] > after]
                done = job.done
                if not batch and not done:
                    job.condition.wait(timeout=10)
                    continue
            if batch:
                after = batch[-1]["seq"]
                yield f"id: {after}\ndata: {json.dumps(batch, separators=(',', ':'))}\n\n"
            if done:
                return

    return Response(stream(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})
