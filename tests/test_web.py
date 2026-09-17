import json
import threading
import time
import unittest
from unittest.mock import patch

from test_runner import fixture_request

import app as web
from engine import input_reservation
from jev import JevError, payload


class WebTests(unittest.TestCase):
    def setUp(self):
        web.rate_limits.clear()
        web.compile_limits.clear()
        web.compile_times.clear()
        self.client = web.app.test_client()
        self.api = patch("engine.request", side_effect=fixture_request).start()
        self.budget = patch("engine.MAX_SECONDS", 0.2).start()
        self.addCleanup(patch.stopall)

    def wait(self, job, predicate):
        with job.condition:
            self.assertTrue(job.condition.wait_for(predicate, timeout=5), list(job.events)[-3:])

    def start(self, **options):
        response = self.client.post("/api/runs", json={"program": "sum", **options})
        self.assertEqual(response.status_code, 201)
        job = web.jobs[response.json["id"]]
        self.addCleanup(job.control, "stop")
        return job

    def test_page_and_font_are_local(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Clock speed", response.data)
        self.assertNotIn(b"apikey_", response.data)
        self.assertNotIn(b"Cached Jev", response.data)
        self.assertIn(b'little-je', response.data)
        self.assertIn(b'https://x.com/i2cjak', response.data)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        font = self.client.get("/static/fonts/berkeley-mono-variable.woff2")
        self.assertEqual(font.status_code, 200 if (web.ROOT / "static/fonts/berkeley-mono-variable.woff2").exists() else 404)
        font.close()

    def test_live_requests_costs_and_event_stream(self):
        job = self.start()
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "limit")
        response = self.client.get(f"/api/runs/{job.id}/events")
        events = [event for line in response.text.splitlines() if line.startswith("data: ") for event in json.loads(line[6:])]
        self.assertEqual(events[-1]["type"], "done")
        replies = [e for e in events if e["type"] == "response"]
        requests = [e for e in events if e["type"] == "request"]
        self.assertGreater(len(replies), 1)
        self.assertGreaterEqual(len(requests), len(replies))
        for reply in replies:
            self.assertEqual(reply["input_tokens"], 2118)
            self.assertAlmostEqual(reply["input_cost_usd"], 0.000088956)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        resumed = self.client.get(f"/api/runs/{job.id}/events", headers={"Last-Event-ID": str(events[-2]["seq"])})
        self.assertNotIn('"type":"request"', resumed.text)
        self.assertIn('"type":"done"', resumed.text)
        self.assertIsNotNone(job.simulation.process.poll())

    def test_step_pause_resume_and_stop(self):
        job = self.start(mode="step")
        self.wait(job, lambda: job.state == "paused")
        replies = lambda: sum(e["type"] == "response" for e in job.events)
        self.assertEqual(replies(), 1)
        paused_elapsed = job.elapsed()
        time.sleep(0.25)  # Pausing beyond the budget must not expire execution.
        self.assertAlmostEqual(job.elapsed(), paused_elapsed, delta=0.005)
        self.assertFalse(job.done)
        self.client.post(f"/api/runs/{job.id}/control", json={"action": "step"})
        self.wait(job, lambda: job.state == "paused" and replies() == 2)
        self.client.post(f"/api/runs/{job.id}/control", json={"action": "run"})
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "limit")
        other = self.start(mode="step")
        self.wait(other, lambda: other.state == "paused")
        self.client.post(f"/api/runs/{other.id}/control", json={"action": "stop"})
        self.wait(other, lambda: other.done)
        self.assertEqual(other.state, "stopped")
        self.assertIsNotNone(other.simulation.process.poll())

    def test_network_failure_has_no_fallback(self):
        with patch("engine.request", side_effect=JevError("TypeSafe HTTP 401.")):
            job = self.start()
            self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "error")
        self.assertIsNotNone(job.simulation.process.poll())
        self.assertFalse(any(e["type"] == "response" for e in job.events))

    def test_wrong_model_outputs_are_applied_without_checks(self):
        def wrong(body, **kwargs):
            response, duration = fixture_request(body)
            for answer in response["answers"].values():
                answer.update(choice="true", confidence=0.001, probabilities={"false": 1, "true": 0})
            return response, duration

        with patch("engine.request", side_effect=wrong):
            job = self.start(mode="step")
            self.wait(job, lambda: job.state == "paused")
            reply = next(e for e in job.events if e["type"] == "response")
            self.assertTrue(all(gate["output"] == 1 for gate in reply["results"]))
            self.assertEqual(reply["gates"], 64)
            job.control("stop")
            self.wait(job, lambda: job.done)

    def test_budget_is_checked_before_sending_requests(self):
        # Start below the first reservation: nothing is sent or billed.
        with patch("engine.MAX_INPUT_TOKENS", 1):
            job = self.start()
            self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "limit")
        self.assertEqual(self.api.call_count, 0)

    def test_reported_usage_reduces_budget_for_next_batch(self):
        # Learn the real first batch shape without allowing a second request.
        job = self.start(mode="step")
        self.wait(job, lambda: job.state == "paused")
        first = next(e for e in job.events if e["type"] == "request")
        reservation = input_reservation(payload(first["batch"]))
        job.control("stop")
        self.wait(job, lambda: job.done)
        self.api.reset_mock()
        with patch("engine.MAX_INPUT_TOKENS", reservation + 100):
            other = self.start()
            self.wait(other, lambda: other.done)
        self.assertEqual(other.state, "limit")
        self.assertEqual(self.api.call_count, 1)
        self.assertEqual(other.simulation.input_tokens, 2118)

    def test_deadline_and_stop_do_not_wait_for_stalled_api(self):
        released = threading.Event()
        entered = threading.Event()

        def stalled(body, **kwargs):
            entered.set()
            released.wait(timeout=3)
            return fixture_request(body)

        try:
            with patch("engine.request", side_effect=stalled):
                started = time.monotonic()
                job = self.start()
                self.assertTrue(entered.wait(timeout=1))
                self.wait(job, lambda: job.done)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(job.state, "limit")
                self.assertFalse(any(e["type"] == "response" for e in job.events))
                other = self.start()
                self.wait(other, lambda: any(e["type"] == "request" for e in other.events))
                self.client.post(f"/api/runs/{other.id}/control", json={"action": "stop"})
                self.wait(other, lambda: other.done)
                self.assertEqual(other.state, "stopped")
        finally:
            released.set()

    def test_invalid_program_and_cross_origin_controls_rejected(self):
        self.assertEqual(self.client.post("/api/runs", json={"program": "../../.env"}).status_code, 400)
        self.assertEqual(self.client.post("/api/runs", json={"program": "sum"}, headers={"Origin": "https://outside.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/runs", data="{}").status_code, 415)
        self.assertEqual(self.client.get("/.env").status_code, 404)
        self.assertEqual(self.client.post("/api/runs", json=[]).status_code, 400)

    def test_visitor_code_compiles_then_uses_live_gates(self):
        job = self.start(source='#include "io.h"\nint main(void) { print_number(123); return 0; }')
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "limit", list(job.events)[-3:])
        self.assertTrue(any(e["type"] == "compiled" for e in job.events))
        self.assertTrue(any(e["type"] == "response" for e in job.events))
        self.assertFalse(job.binary.exists())

    def test_compiler_cannot_read_host_files(self):
        job = self.start(source='#include "/app/.env"\nint main(void) {return 0;}')
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "error")
        self.assertIsNone(job.simulation)
        self.assertTrue(any("No such file" in e.get("message", "") for e in job.events))

    def test_syntax_error_is_returned_before_jev_request(self):
        job = self.start(source='int main( { not C')
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "error")
        self.assertFalse(any(e["type"] == "request" for e in job.events))

    def test_infinite_loop_is_terminated(self):
        job = self.start(source='int main(void) { for (;;) { __asm__ volatile ("nop"); } }')
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "limit")
        self.assertIsNotNone(job.simulation.process.poll())

    def test_compilation_cooldown_is_server_enforced(self):
        job = self.start(source='int main(void) { return 0; }')
        self.wait(job, lambda: job.done)
        response = self.client.post("/api/runs", json={"program": "sum", "source": "int main(void) {return 1;}"})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(response.json["retry_after"], 0)
        self.assertEqual(response.headers["Retry-After"], str(response.json["retry_after"]))
        self.assertGreater(self.client.get("/api/limits").json["compile_retry_after"], 0)
        example = self.start()
        self.wait(example, lambda: example.done)
        self.assertEqual(example.state, "limit")

    def test_global_compilation_cap_applies_to_another_ip(self):
        web.compile_times.extend([web.time.monotonic()] * 20)
        response = self.client.post("/api/runs", json={"program": "sum", "source": "int main(void) {return 0;}"}, environ_overrides={"REMOTE_ADDR": "192.0.2.2"})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(response.json["retry_after"], 0)


if __name__ == "__main__":
    unittest.main()
