"""TypeSafe Boolean classifications. Expected values validate; never repair answers."""

import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
# Input order is A, B, S; A is the most significant bit of the table index.
GATES = {
    "NOT": ("!A", "A", "10"),
    "AND": ("AB", "AB", "0001"),
    "OR": ("A+B", "AB", "0111"),
    "XOR": ("A^B", "AB", "0110"),
    "MUX": ("S?B:A", "ABS", "00011011"),
}
STATE = (
    "Evaluate single-bit Boolean logic. 0 is false and 1 is true. "
    "AB means A AND B; A+B means A OR B; !A means NOT A; "
    "A^B means exclusive OR (the inputs differ). "
    "S?B:A is a multiplexer: select B when S=1, otherwise select A. "
    "The comma separates an expression from its input bits, ordered as stated. "
    "Each question supplies its own independent inputs. Classify the output bit."
)


class JevError(RuntimeError):
    pass


def cases():
    for gate, (expression, variables, expected) in GATES.items():
        for index in range(len(expected)):
            bits = f"{index:0{len(variables)}b}"
            yield f"{gate}_{bits}", gate, expression, variables, bits


def question(expression, variables, bits):
    assignments = ", ".join(f"{name}={bit}" for name, bit in zip(variables, bits))
    return {
        "type": "choice",
        "instructions": (
            f"What is the Boolean output of {expression},{bits}? "
            f"Input order: {variables}. Inputs: {assignments}."
        ),
        "criteria": {"false": "Output bit is 0.", "true": "Output bit is 1."},
    }


def payload(model=MODEL):
    return {
        "model": model,
        "state": STATE,
        "questions": {
            name: question(expr, variables, bits)
            for name, _, expr, variables, bits in cases()
        },
    }


def request(body, timeout=60, attempts=3):
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise JevError("Set TYPESAFE_API_KEY, or run with uv run --env-file .env.")
    encoded = json.dumps(body).encode()
    started = time.monotonic()
    for attempt in range(attempts):
        req = urllib.request.Request(
            ENDPOINT, data=encoded,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                result = json.load(response)
            if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                raise JevError("TypeSafe returned an invalid response.")
            return result, time.monotonic() - started
        except urllib.error.HTTPError as error:
            status = error.code
            retry_after = error.headers.get("Retry-After", "")
            error.close()
            if status not in (429, 500, 502, 503, 504, 529) or attempt == attempts - 1:
                # Do not expose headers, request bodies, or echoed credentials.
                raise JevError(f"TypeSafe HTTP {status}; classification stopped.") from None
            delay = min(float(retry_after), 30) if retry_after.isdigit() else 2 ** attempt
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise JevError("TypeSafe connection failed or timed out.") from None
            time.sleep(2 ** attempt)
        except (ValueError, UnicodeError):
            raise JevError("TypeSafe returned malformed JSON.") from None
    raise JevError("No TypeSafe response.")


def answer_bit(answer, name, min_confidence=0.9):
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise JevError(f"Missing or invalid Choice answer: {name}")
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    probabilities = answer.get("probabilities")
    if choice not in ("false", "true"):
        raise JevError(f"Invalid Boolean choice: {name}")
    if not isinstance(confidence, (float, int)) or not math.isfinite(confidence):
        raise JevError(f"Invalid confidence: {name}")
    if not 0 <= confidence <= 1 or confidence < min_confidence:
        raise JevError(f"Low confidence for {name}: {confidence:.6f}")
    if not isinstance(probabilities, dict) or set(probabilities) != {"false", "true"}:
        raise JevError(f"Invalid probabilities: {name}")
    values = list(probabilities.values())
    if any(not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1 for p in values):
        raise JevError(f"Invalid probability value: {name}")
    if abs(sum(values) - 1) > 0.01 or probabilities[choice] <= probabilities[{"true": "false", "false": "true"}[choice]]:
        raise JevError(f"Inconsistent probabilities: {name}")
    return int(choice == "true")


def validate_response(response, min_confidence=0.9):
    answers = response.get("answers", {})
    required = {case[0] for case in cases()}
    if set(answers) != required or not response.get("model"):
        raise JevError("Incomplete Jev truth tables or missing model identifier.")
    tables = {gate: [] for gate in GATES}
    for name, gate, expr, _, bits in cases():
        bit = answer_bit(answers[name], name, min_confidence)
        expected = int(GATES[gate][2][int(bits, 2)])
        if bit != expected:
            raise JevError(f"Jev misclassified {expr},{bits}: got {bit}, expected {expected}; refusing to run.")
        tables[gate].append(bit)
    return tables


def truth_tables(cache: Path, refresh=False, model=MODEL, min_confidence=0.9):
    body = payload(model)
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    if cache.exists() and not refresh:
        try:
            record = json.loads(cache.read_text())
        except (ValueError, OSError):
            raise JevError(f"Invalid cache {cache}; use --refresh to replace it.") from None
        if record.get("request_sha256") != fingerprint or record.get("endpoint") != ENDPOINT:
            raise JevError("Cache does not match the current model/prompt; use --refresh.")
        tables = validate_response(record["response"], min_confidence)
        return tables, record, True
    response, elapsed = request(body)
    tables = validate_response(response, min_confidence)
    record = {
        "endpoint": ENDPOINT,
        "request_sha256": fingerprint,
        "requested_model": model,
        "created_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": elapsed,
        "response": response,
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(cache)
    return tables, record, False
