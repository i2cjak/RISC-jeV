"""Web UI for the real SERV gate simulator. Run with one threaded worker."""

import json
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
from jev import GATES, JevError, payload, truth_tables

ROOT = Path(__file__).resolve().parent
PROGRAMS = {"fibonacci": "Fibonacci", "sum": "Sum", "bitwise": "Bitwise", "demo": "Checks"}
app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=16000, SEND_FILE_MAX_AGE_DEFAULT=86400)
if os.environ.get("RAILWAY_ENVIRONMENT_ID"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
jobs = {}
jobs_lock = threading.Lock()
classifier_lock = threading.Lock()
compiler_lock = threading.Lock()
rate_limits = {}


def program_data():
    return [{"id": name, "name": label, "source": (ROOT / f"firmware/{name}.c").read_text()} for name, label in PROGRAMS.items()]


class Run:
    def __init__(self, program, fresh, mode, source=None):
        self.id = uuid.uuid4().hex
        self.program = program
        self.fresh = fresh
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
        self.process = None
        self.state = "compiling" if source is not None else "classifying"
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
                if self.process is not None and self.process.poll() is None:
                    self.process.terminate()
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
                    self.state = "paused"
                    self.emit({"type": "status", "state": "paused", "cycle": self.cycle})
                self.condition.wait(timeout=30)
                if time.monotonic() - self.pause_start > 1200:
                    self.cancelled = True
            if self.pause_start is not None:
                self.paused_seconds += time.monotonic() - self.pause_start
                self.pause_start = None
            if self.cancelled:
                return False
            if self.credits:
                self.credits -= 1
            if self.state != "running":
                self.state = "running"
                self.emit({"type": "status", "state": "running", "cycle": self.cycle})
            return True

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
            self.state = "classifying"
            self.emit({"type": "status", "state": "classifying"})
            cache = ROOT / "build/jev_truth_tables.json"
            with classifier_lock:
                if self.cancelled:
                    return
                needs_api = self.fresh or not cache.exists()
                body = payload()
                self.emit({"type": "request", "source": "api" if needs_api else "cache", "request": body})
                tables, record, cached = truth_tables(cache, refresh=self.fresh)
                self.emit({
                    "type": "response", "source": "cache" if cached else "api",
                    "response": record["response"], "duration": record["elapsed_seconds"],
                    "created_at": record["created_at"],
                    "input_tokens": 0 if cached else record["response"].get("usage", {}).get("input_tokens", 0),
                    "input_cost_usd": 0 if cached else record["response"].get("usage", {}).get("input_tokens", 0) * 0.042 / 1000000,
                    "input_price_per_million": 0.042,
                })
            if self.cancelled:
                return
            table_file = ROOT / "build" / f"web-{self.id}.tables"
            table_file.write_text("\n".join(" ".join(map(str, tables[gate])) for gate in GATES) + "\n")
            try:
                self.process = subprocess.Popen([
                    str(ROOT / "build/serv_sim"), str(self.binary),
                    str(table_file), "250000", "--interactive",
                ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                for line in self.process.stdout:
                    if self.cancelled:
                        break
                    event = json.loads(line)
                    if event["type"] == "fetch":
                        self.cycle = event["cycle"]
                        self.fetched += 1
                        event["assembly"] = self.disassembly.get(event["pc"], "?")
                        event["fetched"] = self.fetched
                    elif event["type"] == "halt":
                        self.cycle = event["cycle"]
                    self.emit(event)
                    if event["type"] == "fetch":
                        if not self.wait_for_permission():
                            break
                        self.process.stdin.write("go\n")
                        self.process.stdin.flush()
                self.process.stdin.close()
                self.process.wait(timeout=10)
                if self.process.returncode and not self.cancelled:
                    detail = self.process.stderr.read().strip()
                    raise RuntimeError(detail or "Simulator exited with an error")
            finally:
                if self.process is not None:
                    if self.process.poll() is None:
                        self.process.terminate()
                        self.process.wait(timeout=5)
                    for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                        if pipe and not pipe.closed:
                            pipe.close()
                table_file.unlink(missing_ok=True)
            self.state = "stopped" if self.cancelled else "complete"
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
    return render_template("index.html", programs=program_data(), version=version)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/runs")
def start_run():
    data = request.get_json()
    if not isinstance(data, dict) or data.get("program") not in PROGRAMS:
        abort(400, "Unknown program")
    if data.get("mode", "run") not in ("run", "step") or type(data.get("fresh", True)) is not bool:
        abort(400, "Invalid mode or gate source")
    source = data.get("source")
    if source is not None and (not isinstance(source, str) or not source.strip() or len(source.encode()) > 12000):
        abort(400, "Enter a C program up to 12 KB")
    with jobs_lock:
        now = time.monotonic()
        visitor = request.remote_addr or "unknown"
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
        job = Run(data["program"], data.get("fresh", True), data.get("mode", "run"), source)
        jobs[job.id] = job
    threading.Thread(target=job.work, daemon=True).start()
    return {"id": job.id}, 201


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
