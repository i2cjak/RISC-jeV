"""Build and run SERV with memoized Jev Boolean classifications."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from jev import (
    GATES,
    MODEL,
    STATE,
    JevError,
    answer_bit,
    question,
    request,
    truth_tables,
)
from netlist import translate

ROOT = Path(__file__).resolve().parent


def add_jev_options(parser):
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--min-confidence", type=float, default=0.9)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    net = sub.add_parser("netlist", help="Generate C++ connectivity from Yosys JSON")
    net.add_argument("source", type=Path)
    net.add_argument("destination", type=Path)
    classify = sub.add_parser("classify", help="Ask Jev to classify expression,input-bits pairs")
    classify.add_argument("expressions", nargs="+", help="e.g. AB,01 A+B,01 !A,0 A^B,11 S?B:A,011")
    add_jev_options(classify)
    gates = sub.add_parser("gates", help="Classify and validate all 22 gate/input combinations")
    run = sub.add_parser("run", help="Run a compiled flat RV32I binary")
    run.add_argument("firmware", nargs="?", type=Path, default=ROOT / "build/demo.bin")
    run.add_argument("--backend", choices=("jev", "reference"), default="jev")
    run.add_argument("--max-cycles", type=int, default=200000)
    for child in (gates, run):
        child.add_argument("--cache", type=Path, default=ROOT / "build/jev_truth_tables.json")
        child.add_argument("--refresh", action="store_true", help="Fetch fresh classifications from Jev")
        add_jev_options(child)
    args = parser.parse_args()
    if hasattr(args, "min_confidence") and not 0 <= args.min_confidence <= 1:
        parser.error("--min-confidence must be between 0 and 1")

    if args.command == "netlist":
        stats = translate(args.source, args.destination)
        args.destination.with_suffix(".stats.json").write_text(json.dumps(stats, indent=2) + "\n")
        print(json.dumps(stats))
        return 0
    if args.command == "classify":
        specs = []
        for expression in args.expressions:
            expr, separator, bits = expression.rpartition(",")
            gate = next((name for name, spec in GATES.items() if spec[0] == expr), None)
            if not separator or gate is None or len(bits) != len(GATES[gate][1]) or any(b not in "01" for b in bits):
                parser.error(f"Unsupported expression/inputs: {expression}")
            specs.append((expr, bits, GATES[gate][1]))
        response, elapsed = request({
            "model": args.model, "state": STATE,
            "questions": {str(i): question(expr, variables, bits) for i, (expr, bits, variables) in enumerate(specs)},
        })
        for index, (expr, bits, _) in enumerate(specs):
            answer = response["answers"].get(str(index))
            bit = answer_bit(answer, f"{expr},{bits}", args.min_confidence)
            print(f"{expr},{bits} -> {str(bool(bit)).lower()} (confidence={answer['confidence']:.4f})")
        print(f"model={response.get('model')} elapsed={elapsed:.3f}s", file=sys.stderr)
        return 0
    if args.command == "run":
        if args.max_cycles <= 0:
            parser.error("--max-cycles must be positive")
        subprocess.run(["make", "build"], cwd=ROOT, check=True, stdout=sys.stderr)
        if not args.firmware.is_file():
            parser.error(f"Firmware not found: {args.firmware}")
    if args.command == "run" and args.backend == "reference":
        if args.refresh:
            parser.error("--refresh only applies to the Jev backend")
        tables = {gate: list(map(int, spec[2])) for gate, spec in GATES.items()}
        print("Backend: REFERENCE (local Boolean tables; no Jev requests)", file=sys.stderr)
    else:
        tables, record, cached = truth_tables(args.cache, args.refresh, args.model, args.min_confidence)
        response = record["response"]
        print(
            f"Backend: JEV ({'cached' if cached else 'fresh API response'}); "
            f"model={response['model']}; 22/22 classifications correct; "
            f"min_confidence={min(a['confidence'] for a in response['answers'].values()):.4f}",
            file=sys.stderr,
        )
        print(f"Classification evidence: {args.cache}", file=sys.stderr)
    if args.command == "gates":
        for gate, (_, variables, _) in GATES.items():
            print(f"{gate} ({variables}): {''.join(map(str, tables[gate]))}")
        return 0
    # Load answers at runtime so changing the backend never recompiles the CPU.
    table_file = ROOT / "build" / f"{args.backend}_tables.txt"
    table_file.write_text("\n".join(" ".join(map(str, tables[gate])) for gate in GATES) + "\n")
    return subprocess.run([
        str(ROOT / "build/serv_sim"), str(args.firmware.resolve()), str(table_file), str(args.max_cycles),
    ], check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (JevError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
