import json
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import Simulation
from jev import JevError, answer_bit, payload
from netlist import translate

ROOT = Path(__file__).resolve().parents[1]


def reference_response(body):
    """Native logic for tests only. Never called by the app or live engine."""
    answers = {}
    for name, question in body["questions"].items():
        expr, bits = re.search(r"output of (.*),([01]+)\?", question["instructions"]).groups()
        values = list(map(int, bits))
        a = values[0]
        b = values[1] if len(values) > 1 else 0
        select = values[2] if len(values) > 2 else 0
        value = {"!A": 1 - a, "AB": a & b, "A+B": a | b, "A^B": a ^ b, "S?B:A": b if select else a}[expr]
        answers[name] = {"type": "choice", "choice": "true" if value else "false", "confidence": 0.01}
    return {"model": "test-fixture", "answers": answers, "usage": {"input_tokens": 2118}}


def fixture_request(body, **kwargs):
    return reference_response(body), 0.001


class ClassifierTests(unittest.TestCase):
    def test_identical_gates_get_distinct_questions(self):
        body = payload([{"id": 3, "gate": "AND", "bits": "01"}, {"id": 9, "gate": "AND", "bits": "01"}])
        self.assertEqual(list(body["questions"]), ["g_3", "g_9"])
        self.assertEqual(body["questions"]["g_3"], body["questions"]["g_9"])
        self.assertIn("AB,01", body["questions"]["g_3"]["instructions"])

    def test_wrong_low_confidence_choice_is_used_unchanged(self):
        # AB,01 should be false; use the model's true anyway, including with
        # absent, contradictory or nonsensical confidence/probability metadata.
        for extra in ({}, {"confidence": 0.01}, {"confidence": float("nan"), "probabilities": {"false": 1, "true": 0}}):
            self.assertEqual(answer_bit({"choice": "true", **extra}, "AB,01"), 1)
        self.assertEqual(answer_bit({"choice": "false"}, "A+B,01"), 0)

    def test_missing_wire_value_has_no_fallback(self):
        for answer in (None, {}, {"choice": "maybe"}):
            with self.assertRaises(JevError):
                answer_bit(answer, "gate")

    def test_repeated_batches_make_new_requests(self):
        body = payload([{"id": 0, "gate": "AND", "bits": "01"}])
        simulation = Simulation(ROOT / "build/demo.bin", lambda event: None)
        simulation.started = time.monotonic()
        with patch("engine.request", side_effect=fixture_request) as api:
            simulation.classify(body)
            simulation.classify(body)
            self.assertEqual(api.call_count, 2)


class ExecutionTests(unittest.TestCase):
    def simulate(self, firmware="demo", limit=200000):
        return subprocess.run([
            str(ROOT / "build/serv_reference"), str(ROOT / f"build/{firmware}.bin"), str(limit),
        ], text=True, capture_output=True, timeout=10, check=False)

    def test_compiled_c_program_with_test_only_native_gates(self):
        result = self.simulate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RV32I checks passed: fib(10)=55", result.stdout)
        self.assertIn("SERV PASS: exit=0", result.stderr)

    def test_explicit_rv32i_instructions(self):
        result = self.simulate("isa")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SERV PASS: exit=0", result.stderr)

    def test_cycle_limit(self):
        result = self.simulate(limit=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("TIMEOUT", result.stderr)

    def test_live_gate_choices_drive_dependent_inputs_without_reuse(self):
        header = (ROOT / "build/serv_netlist.h").read_text().split(" GATES = {{", 1)[1].split("}};", 1)[0]
        gates = [tuple(map(int, row.split(','))) for row in re.findall(r"\{([0-9,]+)\}", header)]
        process = subprocess.Popen([str(ROOT / "build/serv_sim"), str(ROOT / "build/demo.bin"), "1"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        observed = {}
        checked = 0
        passes = set()
        try:
            for line in process.stdout:
                event = json.loads(line)
                if event["type"] != "gates":
                    continue
                phase = event["phase"]
                passes.add(phase)
                if event["batch"][0]["id"] == 0:
                    observed = {}
                output = "1" if phase == 0 else "0"
                for gate in event["batch"]:
                    _, arity, a, b, s, y, level = gates[gate["id"]]
                    self.assertEqual(level, event["level"])
                    for wire, bit in zip((a, b, s)[:arity], gate["bits"]):
                        if wire in observed:
                            self.assertEqual(bit, observed[wire])
                            checked += 1
                    observed[y] = output
                process.stdin.write(output * len(event["batch"]) + "\n")
                process.stdin.flush()
            process.wait(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
        self.assertGreater(checked, 1000)
        self.assertEqual(passes, {0, 1})  # Same gates are asked again before the first clock edge.
        self.assertEqual(process.returncode, 2)  # One-cycle limit, not an answer rejection.


class NetlistTests(unittest.TestCase):
    def test_unsupported_cells_fail_closed(self):
        module = {
            "ports": {"clk": {"direction": "input", "bits": [2]}},
            "netnames": {},
            "cells": {"latch": {"type": "$_DLATCH_P_", "connections": {"D": [3], "Q": [4], "E": [2]}}},
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "in.json"
            source.write_text(json.dumps({"modules": {"serv_rf_top": module}}))
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                translate(source, Path(directory) / "out.h")


if __name__ == "__main__":
    unittest.main()
