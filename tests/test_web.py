import json
import unittest
from unittest.mock import patch

from test_runner import reference_response

import app as web
from jev import JevError, validate_response


def classified(*args, **kwargs):
    response = reference_response()
    return validate_response(response), {
        "response": response, "elapsed_seconds": 0.01, "created_at": "test-fixture",
    }, not kwargs.get("refresh", False)


class WebTests(unittest.TestCase):
    def setUp(self):
        web.rate_limits.clear()
        web.compile_limits.clear()
        web.compile_times.clear()
        self.client = web.app.test_client()
        self.patcher = patch("app.truth_tables", side_effect=classified)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def wait(self, job, predicate):
        with job.condition:
            self.assertTrue(job.condition.wait_for(predicate, timeout=10), list(job.events)[-3:])

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
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        font = self.client.get("/static/fonts/berkeley-mono-variable.woff2")
        self.assertEqual(font.status_code, 200 if (web.ROOT / "static/fonts/berkeley-mono-variable.woff2").exists() else 404)
        font.close()

    def test_complete_program_and_event_stream(self):
        job = self.start(fresh=True)
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "complete")
        response = self.client.get(f"/api/runs/{job.id}/events")
        batches = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        events = [event for batch in batches for event in batch]
        self.assertEqual("".join(chr(e["byte"]) for e in events if e["type"] == "output"), "3\n10\n21\n40\n42\n")
        self.assertEqual(events[-1]["type"], "done")
        self.assertGreater(events[-1]["cycle"], 30000)
        self.assertGreater(events[-1]["elapsed"], 0)
        self.assertEqual(len([e for e in events if e["type"] == "request"]), 1)
        reply = next(e for e in events if e["type"] == "response")
        self.assertEqual(reply["input_tokens"], 2118)
        self.assertAlmostEqual(reply["input_cost_usd"], 0.000088956)
        fetch = next(e for e in events if e["type"] == "fetch")
        self.assertIn("assembly", fetch)
        halt = next(e for e in events if e["type"] == "halt")
        self.assertEqual(len(halt["wave"]), 64)
        self.assertEqual(halt["wave"][-1][0], halt["cycle"] - 1)
        self.assertEqual(halt["wave"][-1][2], 1)  # Completion is a real data-bus write.
        self.assertTrue(all(len(row) == 7 and -1 <= row[6] <= 31 for row in halt["wave"]))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        resumed = self.client.get(f"/api/runs/{job.id}/events", headers={"Last-Event-ID": str(events[-2]["seq"])})
        self.assertNotIn('"type":"fetch"', resumed.text)
        self.assertIn('"type":"done"', resumed.text)

    def test_step_pause_resume_and_stop(self):
        job = self.start(mode="step", fresh=False)
        self.wait(job, lambda: job.state == "paused")
        cached_reply = next(e for e in job.events if e["type"] == "response")
        self.assertEqual(cached_reply["input_cost_usd"], 0)
        self.assertEqual(cached_reply["input_tokens"], 0)
        count = job.fetched
        self.assertEqual(count, 2)  # First instruction executed; next fetch waits.
        self.client.post(f"/api/runs/{job.id}/control", json={"action": "step"})
        self.wait(job, lambda: job.state == "paused" and job.fetched == count + 1)
        self.assertFalse(job.done)
        self.client.post(f"/api/runs/{job.id}/control", json={"action": "run"})
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "complete")
        other = self.start(mode="step", fresh=False)
        self.wait(other, lambda: other.state == "paused")
        self.client.post(f"/api/runs/{other.id}/control", json={"action": "stop"})
        self.wait(other, lambda: other.done)
        self.assertEqual(other.state, "stopped")
        self.assertIsNotNone(other.process.poll())

    def test_api_failure_does_not_start_simulator(self):
        with patch("app.truth_tables", side_effect=JevError("TypeSafe HTTP 401; classification stopped.")):
            job = self.start()
            self.wait(job, lambda: job.done)
            self.assertEqual(job.state, "error")
            self.assertIsNone(job.process)
            self.assertTrue(any(e["type"] == "error" for e in job.events))

    def test_invalid_program_and_cross_origin_controls_rejected(self):
        self.assertEqual(self.client.post("/api/runs", json={"program": "../../.env"}).status_code, 400)
        self.assertEqual(self.client.post("/api/runs", json={"program": "sum"}, headers={"Origin": "https://outside.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/runs", data="{}").status_code, 415)
        self.assertEqual(self.client.get("/.env").status_code, 404)
        self.assertEqual(self.client.post("/api/runs", json=[]).status_code, 400)

    def test_visitor_code_compiles_and_executes(self):
        job = self.start(source='#include "io.h"\nint main(void) { print_number(123); return 0; }', fresh=False)
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "complete", list(job.events)[-3:])
        self.assertTrue(any(e["type"] == "compiled" for e in job.events))
        self.assertEqual("".join(chr(e["byte"]) for e in job.events if e["type"] == "output"), "123\n")
        self.assertFalse(job.binary.exists())

    def test_compiler_cannot_read_host_files(self):
        job = self.start(source='#include "/app/.env"\nint main(void) {return 0;}', fresh=False)
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "error")
        self.assertIsNone(job.process)
        self.assertTrue(any("No such file" in e.get("message", "") for e in job.events))

    def test_syntax_error_is_returned_before_jev_request(self):
        job = self.start(source='int main( { not C', fresh=True)
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "error")
        self.assertFalse(any(e["type"] == "request" for e in job.events))

    def test_infinite_loop_is_terminated(self):
        job = self.start(source='int main(void) { for (;;) { __asm__ volatile ("nop"); } }', fresh=False)
        self.wait(job, lambda: job.done)
        self.assertEqual(job.state, "error")
        self.assertIsNotNone(job.process.poll())
        self.assertTrue(any("TIMEOUT" in e.get("message", "") for e in job.events))

    def test_compilation_cooldown_is_server_enforced(self):
        job = self.start(source='int main(void) { return 0; }', fresh=False)
        self.wait(job, lambda: job.done)
        response = self.client.post("/api/runs", json={"program": "sum", "source": "int main(void) {return 1;}"})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(response.json["retry_after"], 0)
        self.assertEqual(response.headers["Retry-After"], str(response.json["retry_after"]))
        self.assertGreater(self.client.get("/api/limits").json["compile_retry_after"], 0)
        example = self.start(fresh=False)
        self.wait(example, lambda: example.done)
        self.assertEqual(example.state, "complete")

    def test_global_compilation_cap_applies_to_another_ip(self):
        web.compile_times.extend([web.time.monotonic()] * 20)
        response = self.client.post("/api/runs", json={"program": "sum", "source": "int main(void) {return 0;}"}, environ_overrides={"REMOTE_ADDR": "192.0.2.2"})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(response.json["retry_after"], 0)


if __name__ == "__main__":
    unittest.main()
