import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_runner import fixture_request

import app as web
from jev import JevError


class WebTests(unittest.TestCase):
    def setUp(self):
        web.rate_limits.clear()
        web.compile_limits.clear()
        web.compile_times.clear()
        self.client = web.app.test_client()
        self.addCleanup(patch.stopall)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patch('engine.CACHE_FILE', Path(self.directory.name) / 'jev.json').start()
        self.api = patch('engine.request', side_effect=fixture_request).start()
        patch('engine.MAX_SECONDS', 5).start()

    def wait(self, job, predicate):
        with job.condition:
            self.assertTrue(job.condition.wait_for(predicate, timeout=8), list(job.events)[-2:])

    def start(self, **options):
        response = self.client.post('/api/runs', json={'program': 'sum', **options})
        self.assertEqual(response.status_code, 201)
        job = web.jobs[response.json['id']]

        def stop():
            job.control('stop')
            with job.condition:
                job.condition.wait_for(lambda: job.done, timeout=8)

        self.addCleanup(stop)
        return job

    def test_page_and_font_are_local(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Clock speed', response.data)
        self.assertNotIn(b'apikey_', response.data)
        self.assertIn(b'Cached Jev', response.data)
        self.assertIn(b'little-je', response.data)
        self.assertIn(b'https://x.com/i2cjak', response.data)
        self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])
        font = self.client.get('/static/fonts/berkeley-mono-variable.woff2')
        self.assertEqual(font.status_code, 200 if (web.ROOT / 'static/fonts/berkeley-mono-variable.woff2').exists() else 404)
        font.close()

    def test_program_cache_costs_and_event_stream(self):
        job = self.start()
        self.wait(job, lambda job=job: job.done)
        self.assertEqual(job.state, 'complete')
        response = self.client.get(f'/api/runs/{job.id}/events')
        events = [event for line in response.text.splitlines() if line.startswith('data: ') for event in json.loads(line[6:])]
        self.assertEqual(''.join(chr(e['byte']) for e in events if e['type'] == 'output'), '3\n10\n21\n40\n42\n')
        reply = next(e for e in events if e['type'] == 'response')
        self.assertEqual(reply['source'], 'api')
        self.assertEqual(reply['input_tokens'], 2118)
        self.assertAlmostEqual(reply['input_cost_usd'], 0.000088956)
        self.assertEqual(len(reply['results']), 22)
        halt = next(e for e in events if e['type'] == 'halt')
        self.assertEqual(len(halt['wave']), 64)
        self.assertEqual(len(halt['gate_values']), 4637)
        self.assertEqual(halt['wave'][-1][0], halt['cycle'] - 1)
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        resumed = self.client.get(f'/api/runs/{job.id}/events', headers={'Last-Event-ID': str(events[-2]['seq'])})
        self.assertNotIn('"type":"request"', resumed.text)
        self.assertIn('"type":"done"', resumed.text)
        other = self.start()
        self.wait(other, lambda: other.done)
        self.assertEqual(other.state, 'complete')
        cached = next(e for e in other.events if e['type'] == 'response')
        self.assertEqual(cached['source'], 'cache')
        self.assertEqual(cached['input_tokens'], 0)
        self.assertEqual(cached['input_cost_usd'], 0)
        self.assertEqual(self.api.call_count, 1)

    def test_fresh_option_replaces_cache(self):
        for fresh in (False, False, True, False):
            job = self.start(fresh=fresh)
            self.wait(job, lambda job=job: job.done)
            self.assertEqual(job.state, 'complete')
        self.assertEqual(self.api.call_count, 2)

    def test_concurrent_cache_misses_share_one_request(self):
        first, second = self.start(), self.start()
        self.wait(first, lambda: first.done)
        self.wait(second, lambda: second.done)
        self.assertEqual((first.state, second.state), ('complete', 'complete'))
        self.assertEqual(self.api.call_count, 1)

    def test_step_pause_resume_and_stop(self):
        job = self.start(mode='step')
        self.wait(job, lambda: job.state == 'paused')
        self.assertEqual(job.fetched, 2)
        paused_elapsed = job.elapsed()
        time.sleep(0.1)
        self.assertAlmostEqual(job.elapsed(), paused_elapsed, delta=0.005)
        self.client.post(f'/api/runs/{job.id}/control', json={'action': 'step'})
        self.wait(job, lambda: job.state == 'paused' and job.fetched == 3)
        self.client.post(f'/api/runs/{job.id}/control', json={'action': 'run'})
        self.wait(job, lambda job=job: job.done)
        self.assertEqual(job.state, 'complete')
        other = self.start(mode='step')
        self.wait(other, lambda: other.state == 'paused')
        self.client.post(f'/api/runs/{other.id}/control', json={'action': 'stop'})
        self.wait(other, lambda: other.done)
        self.assertEqual(other.state, 'stopped')
        self.assertIsNotNone(other.simulation.process.poll())

    def test_network_failure_has_no_native_fallback(self):
        with patch('engine.request', side_effect=JevError('TypeSafe HTTP 401.')):
            job = self.start()
            self.wait(job, lambda job=job: job.done)
        self.assertEqual(job.state, 'error')
        self.assertIsNone(job.simulation.process)
        self.assertFalse(any(e['type'] == 'response' for e in job.events))

    def test_wrong_model_choices_are_cached_without_checks(self):
        def wrong(body, **kwargs):
            response, duration = fixture_request(body)
            for answer in response['answers'].values():
                answer.update(choice='true', confidence=0.001, probabilities={'false': 1, 'true': 0})
            return response, duration

        with patch('engine.request', side_effect=wrong) as api:
            for expected_source in ('api', 'cache'):
                job = self.start()
                self.wait(job, lambda job=job: job.done)
                reply = next(e for e in job.events if e['type'] == 'response')
                self.assertEqual(reply['source'], expected_source)
                self.assertTrue(all(gate['output'] == 1 for gate in reply['results']))
                self.assertEqual(len(reply['results']), 22)
            self.assertEqual(api.call_count, 1)

    def test_budget_is_checked_before_sending_request(self):
        with patch('engine.MAX_INPUT_TOKENS', 1):
            job = self.start()
            self.wait(job, lambda job=job: job.done)
        self.assertEqual(job.state, 'limit')
        self.assertEqual(self.api.call_count, 0)

    def test_deadline_and_stop_do_not_wait_for_stalled_api(self):
        released = threading.Event()
        entered = threading.Event()

        def stalled(body, **kwargs):
            entered.set()
            released.wait(timeout=3)
            return fixture_request(body)

        try:
            with patch('engine.request', side_effect=stalled), patch('engine.MAX_SECONDS', 0.2):
                started = time.monotonic()
                job = self.start()
                self.assertTrue(entered.wait(timeout=1))
                self.wait(job, lambda job=job: job.done)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(job.state, 'limit')
                self.assertFalse(any(e['type'] == 'response' for e in job.events))
                other = self.start()
                self.wait(other, lambda: any(e['type'] == 'request' for e in other.events))
                self.client.post(f'/api/runs/{other.id}/control', json={'action': 'stop'})
                self.wait(other, lambda: other.done)
                self.assertEqual(other.state, 'stopped')
        finally:
            released.set()

    def test_invalid_program_and_cross_origin_controls_rejected(self):
        self.assertEqual(self.client.post('/api/runs', json={'program': '../../.env'}).status_code, 400)
        self.assertEqual(self.client.post('/api/runs', json={'program': 'sum'}, headers={'Origin': 'https://outside.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/runs', data='{}').status_code, 415)
        self.assertEqual(self.client.get('/.env').status_code, 404)
        self.assertEqual(self.client.post('/api/runs', json=[]).status_code, 400)

    def test_visitor_code_compiles_and_executes(self):
        job = self.start(source='#include "io.h"\nint main(void) { print_number(123); return 0; }')
        self.wait(job, lambda job=job: job.done)
        self.assertEqual(job.state, 'complete', list(job.events)[-2:])
        self.assertTrue(any(e['type'] == 'compiled' for e in job.events))
        self.assertEqual(''.join(chr(e['byte']) for e in job.events if e['type'] == 'output'), '123\n')
        self.assertFalse(job.binary.exists())

    def test_compiler_cannot_read_host_files(self):
        job = self.start(source='#include "/app/.env"\nint main(void) {return 0;}')
        self.wait(job, lambda job=job: job.done)
        self.assertEqual(job.state, 'error')
        self.assertIsNone(job.simulation)
        self.assertTrue(any('No such file' in e.get('message', '') for e in job.events))

    def test_syntax_error_is_returned_before_jev_request(self):
        job = self.start(source='int main( { not C')
        self.wait(job, lambda job=job: job.done)
        self.assertEqual(job.state, 'error')
        self.assertFalse(any(e['type'] == 'request' for e in job.events))

    def test_infinite_loop_is_terminated(self):
        with patch('engine.MAX_SECONDS', 0.1):
            job = self.start(source='int main(void) { for (;;) { __asm__ volatile ("nop"); } }')
            self.wait(job, lambda job=job: job.done)
        self.assertEqual(job.state, 'limit')
        self.assertIsNotNone(job.simulation.process.poll())

    def test_compilation_cooldown_is_server_enforced(self):
        job = self.start(source='int main(void) { return 0; }')
        self.wait(job, lambda job=job: job.done)
        self.assertEqual(job.state, 'complete')
        response = self.client.post('/api/runs', json={'program': 'sum', 'source': 'int main(void) {return 1;}'})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(response.json['retry_after'], 0)
        self.assertEqual(response.headers['Retry-After'], str(response.json['retry_after']))
        self.assertGreater(self.client.get('/api/limits').json['compile_retry_after'], 0)
        example = self.start()
        self.wait(example, lambda: example.done)
        self.assertEqual(example.state, 'complete')

    def test_global_compilation_cap_applies_to_another_ip(self):
        web.compile_times.extend([web.time.monotonic()] * 20)
        response = self.client.post('/api/runs', json={'program': 'sum', 'source': 'int main(void) {return 0;}'}, environ_overrides={'REMOTE_ADDR': '192.0.2.2'})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(response.json['retry_after'], 0)


if __name__ == '__main__':
    unittest.main()
