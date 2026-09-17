import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from jev import (
    GATES,
    JevError,
    answer_bit,
    load_cache,
    payload,
    save_cache,
    table_cases,
    table_values,
)
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

    def test_cache_preserves_unchecked_answers_and_matches_prompt(self):
        body = payload(table_cases())
        response = reference_response(body)
        response["answers"]["g_0"].update(choice="false", confidence=0.001)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "jev.json"
            save_cache(cache, body, response, 0.1)
            record = load_cache(cache, body)
            self.assertEqual(table_values(record["response"])["NOT"][0], 0)
            self.assertIsNone(load_cache(cache, payload(table_cases(), model="another-model")))



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

    def test_cached_choices_really_drive_cpu_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            table_file = Path(directory) / "tables.txt"
            tables = table_values(reference_response(payload(table_cases())))
            table_file.write_text("\n".join(" ".join(map(str, tables[gate])) for gate in GATES))
            result = subprocess.run([str(ROOT / "build/serv_sim"), str(ROOT / "build/demo.bin"), str(table_file), "200000"],
                                    input="go\n" * 1000, text=True, capture_output=True, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertIn("fib(10)=55", "".join(chr(e["byte"]) for e in events if e["type"] == "output"))
            self.assertEqual(len(events[-1]["gate_values"]), 4637)
            # Wrong table choices are consumed as-is, not replaced with native logic.
            table_file.write_text(" ".join("0" for _ in range(22)))
            result = subprocess.run([str(ROOT / "build/serv_sim"), str(ROOT / "build/demo.bin"), str(table_file), "100"],
                                    input="go\n" * 1000, text=True, capture_output=True, timeout=5, check=False)
            self.assertEqual(result.returncode, 2)
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
