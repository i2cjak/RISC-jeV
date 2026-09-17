"""Build and run SERV with cached Jev Boolean lookup tables."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from engine import Simulation
from jev import GATES, MODEL, JevError, answer_bit, payload, request
from netlist import translate

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    net = sub.add_parser("netlist", help="Generate C++ connectivity from Yosys JSON")
    net.add_argument("source", type=Path)
    net.add_argument("destination", type=Path)
    classify = sub.add_parser("classify", help="Ask Jev to classify expression,input-bits pairs")
    classify.add_argument("expressions", nargs="+", help="e.g. AB,01 A+B,01 !A,0 A^B,11 S?B:A,011")
    classify.add_argument("--model", default=MODEL)
    run = sub.add_parser("run", help="Run a compiled flat RV32I binary with cached Jev gates")
    run.add_argument("firmware", nargs="?", type=Path, default=ROOT / "build/demo.bin")
    run.add_argument("--model", default=MODEL)
    run.add_argument("--fresh", action="store_true", help="Replace the cached lookup tables with new Jev choices")
    run.add_argument("--max-cycles", type=int, default=200000)
    args = parser.parse_args()
    if args.command == "netlist":
        stats = translate(args.source, args.destination)
        args.destination.with_suffix(".stats.json").write_text(json.dumps(stats, indent=2) + "\n")
        print(json.dumps(stats))
        return 0
    if args.command == "classify":
        batch = []
        for index, expression in enumerate(args.expressions):
            expr, separator, bits = expression.rpartition(",")
            gate = next((name for name, spec in GATES.items() if spec[0] == expr), None)
            if not separator or gate is None or len(bits) != len(GATES[gate][1]) or any(b not in "01" for b in bits):
                parser.error(f"Unsupported expression/inputs: {expression}")
            batch.append({"id": index, "gate": gate, "bits": bits})
        response, elapsed = request(payload(batch, args.model))
        for gate, expression in zip(batch, args.expressions):
            bit = answer_bit(response["answers"].get(f"g_{gate['id']}"), expression)
            print(f"{expression} -> {str(bool(bit)).lower()}")
        print(f"model={response.get('model')} elapsed={elapsed:.3f}s", file=sys.stderr)
        return 0
    if args.max_cycles <= 0:
        parser.error("--max-cycles must be positive")
    subprocess.run(["make", "build"], cwd=ROOT, check=True, stdout=sys.stderr)
    if not args.firmware.is_file():
        parser.error(f"Firmware not found: {args.firmware}")

    def log(event):
        if event["type"] == "output":
            print(chr(event["byte"]), end="", flush=True)
        elif event["type"] == "response":
            print(f"Jev tables: {event['source']}; input cost=${event['input_cost_usd']:.9f}", file=sys.stderr)
        elif event["type"] == "halt":
            print(f"exit={event['exit_code']}", file=sys.stderr)

    Simulation(args.firmware.resolve(), log, model=args.model, max_cycles=args.max_cycles, fresh=args.fresh).run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (JevError, RuntimeError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
