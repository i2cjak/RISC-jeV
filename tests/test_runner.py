import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev import GATES, JevError, cases, payload, truth_tables, validate_response
from netlist import translate

ROOT = Path(__file__).resolve().parents[1]


def reference_response():
    """Test fixture, never evidence of a real API call."""
    answers = {}
    for name, gate, _, _, bits in cases():
        bit = GATES[gate][2][int(bits, 2)]
        choice = "true" if bit == "1" else "false"
        answers[name] = {
            "type": "choice", "choice": choice, "confidence": 1.0,
            "probabilities": {"false": float(bit == "0"), "true": float(bit == "1")},
        }
    return {"model": "test-fixture", "answers": answers, "usage": {"input_tokens": 2118, "output_tokens": 695}}


class ClassifierTests(unittest.TestCase):
    def test_independent_questions_include_expression_and_inputs(self):
        questions = payload()["questions"]
        self.assertEqual(len(questions), 22)
        self.assertIn("AB,01", questions["AND_01"]["instructions"])
        self.assertIn("A+B,01", questions["OR_01"]["instructions"])
        self.assertIn("S=1", questions["MUX_011"]["instructions"])

    def test_every_truth_table_row(self):
        tables = validate_response(reference_response())
        self.assertEqual(tables["AND"][1], 0)
        self.assertEqual(tables["OR"][1], 1)
        for a in (0, 1):
            self.assertEqual(tables["NOT"][a], 1 - a)
            for b in (0, 1):
                self.assertEqual(tables["AND"][2 * a + b], a & b)
                self.assertEqual(tables["OR"][2 * a + b], a | b)
                self.assertEqual(tables["XOR"][2 * a + b], a ^ b)
                for s in (0, 1):
                    self.assertEqual(tables["MUX"][4 * a + 2 * b + s], b if s else a)

    def test_wrong_answer_is_rejected_never_corrected(self):
        response = reference_response()
        response["answers"]["AND_01"] = copy.deepcopy(response["answers"]["OR_01"])
        with self.assertRaisesRegex(JevError, "misclassified"):
            validate_response(response)

    def test_missing_answer_rejected(self):
        response = reference_response()
        del response["answers"]["NOT_0"]
        with self.assertRaisesRegex(JevError, "Incomplete"):
            validate_response(response)

    def test_low_confidence_and_invalid_probability_rejected(self):
        for bad_value in (0.1, float("nan"), 1.1):
            response = reference_response()
            response["answers"]["AND_00"]["confidence"] = bad_value
            with self.assertRaises(JevError):
                validate_response(response)
        response = reference_response()
        response["answers"]["AND_00"]["probabilities"]["false"] = 0
        with self.assertRaises(JevError):
            validate_response(response)

    def test_cache_reuse_refresh_and_model_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "tables.json"
            with patch("jev.request", return_value=(reference_response(), 0.1)) as api:
                _, _, cached = truth_tables(cache)
                self.assertFalse(cached)
                _, _, cached = truth_tables(cache)
                self.assertTrue(cached)
                self.assertEqual(api.call_count, 1)
                truth_tables(cache, refresh=True)
                self.assertEqual(api.call_count, 2)
                with self.assertRaisesRegex(JevError, "model/prompt"):
                    truth_tables(cache, model="different-model")

    def test_corrupt_cached_answer_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "tables.json"
            with patch("jev.request", return_value=(reference_response(), 0.1)):
                truth_tables(cache)
            record = json.loads(cache.read_text())
            record["response"]["answers"]["NOT_0"]["choice"] = "false"
            cache.write_text(json.dumps(record))
            with self.assertRaises(JevError):
                truth_tables(cache)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tables = Path(self.directory.name) / "reference.txt"
        self.tables.write_text("\n".join(" ".join(spec[2]) for spec in GATES.values()))

    def simulate(self, firmware="demo", limit=200000):
        return subprocess.run([
            str(ROOT / "build/serv_sim"), str(ROOT / f"build/{firmware}.bin"),
            str(self.tables), str(limit),
        ], text=True, capture_output=True, timeout=30, check=False)

    def test_compiled_c_program(self):
        result = self.simulate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RV32I checks passed: fib(10)=55", result.stdout)
        self.assertIn("SERV PASS: exit=0", result.stderr)

    def test_explicit_rv32i_instructions(self):
        result = self.simulate("isa")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SERV PASS: exit=0", result.stderr)

    def test_cycle_limit_is_failure(self):
        result = self.simulate(limit=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("TIMEOUT", result.stderr)

    def test_invalid_tables_cannot_run(self):
        self.tables.write_text("1 0 0 0\n")
        result = self.simulate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid truth tables", result.stderr)

    def test_gate_results_really_control_execution(self):
        # Bypass the Python validator on purpose: the simulator must actually
        # use loaded table bits, not quietly compute the right Boolean answer.
        self.tables.write_text(" ".join("0" for _ in range(22)))
        result = self.simulate(limit=100)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SERV PASS", result.stderr)


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
