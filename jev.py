"""Live Boolean gate questions for Jev. Returned choices become wire values."""

import json
import os
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
# Variable order is also the simulator's input-bit order.
GATES = {
    "NOT": ("!A", "A"),
    "AND": ("AB", "AB"),
    "OR": ("A+B", "AB"),
    "XOR": ("A^B", "AB"),
    "MUX": ("S?B:A", "ABS"),
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


def payload(batch, model=MODEL):
    return {
        "model": model,
        "state": STATE,
        "questions": {
            f"g_{gate['id']}": question(*GATES[gate["gate"]], gate["bits"])
            for gate in batch
        },
    }


def request(body, timeout=5):
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise JevError("Set TYPESAFE_API_KEY, or run with uv run --env-file .env.")
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.load(response)
        if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            raise JevError("TypeSafe returned no answers.")
        return result, time.monotonic() - started
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise JevError(f"TypeSafe HTTP {status}.") from None
    except (urllib.error.URLError, TimeoutError):
        raise JevError("TypeSafe connection failed or timed out.") from None
    except (ValueError, UnicodeError):
        raise JevError("TypeSafe returned malformed JSON.") from None


def answer_bit(answer, name):
    # Only decode the wire's representation. No expected answer, confidence
    # threshold, probability check, correction, or retry based on the choice.
    choice = answer.get("choice") if isinstance(answer, dict) else None
    if choice not in ("false", "true"):
        raise JevError(f"No Boolean choice returned for {name}.")
    return int(choice == "true")
