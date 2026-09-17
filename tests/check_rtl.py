"""Compare original Verilog against the gate simulator using the same firmware."""

import os
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
iverilog = shlex.split(os.environ.get("IVERILOG", "iverilog"))
vvp = shlex.split(os.environ.get("VVP", "vvp"))
subprocess.run([
    *iverilog, "-g2012", "-DSERV_CLEAR_RAM", "-s", "rtl_tb", "-o", "build/rtl_sim",
    "tests/rtl_tb.v", *map(str, sorted(Path("serv/rtl").glob("*.v"))),
], cwd=ROOT, check=True)
table_path = ROOT / "build/rtl_reference_tables.txt"
table_path.write_text("1 0\n0 0 0 1\n0 1 1 1\n0 1 1 0\n0 0 0 1 1 0 1 1\n")
for name in ("demo", "isa"):
    image = (ROOT / f"build/{name}.bin").read_bytes()
    image += bytes(65536 - len(image))
    hex_path = ROOT / f"build/{name}.bytes.hex"
    hex_path.write_text("\n".join(f"{byte:02x}" for byte in image) + "\n")
    rtl = subprocess.run([*vvp, "build/rtl_sim", f"+firmware={hex_path}"], cwd=ROOT, text=True, capture_output=True, check=True)
    gates = subprocess.run(["build/serv_sim", f"build/{name}.bin", str(table_path), "200000"], cwd=ROOT, text=True, capture_output=True, check=True)
    # Compare program text and independent timing/bus transaction totals.
    if rtl.stdout.split("RTL PASS:")[0] != gates.stdout:
        raise AssertionError(f"Output mismatch for {name}: {rtl.stdout!r} vs {gates.stdout!r}")
    pattern = r"cycles=(\d+) fetches=(\d+) loads=(\d+) stores=(\d+)"
    rtl_stats = re.search(pattern, rtl.stdout)
    gate_stats = re.search(pattern, gates.stderr)
    if not rtl_stats or not gate_stats or rtl_stats.groups() != gate_stats.groups():
        raise AssertionError(f"Timing/bus mismatch for {name}: {rtl.stdout!r} vs {gates.stderr!r}")
    print(f"{name}: original SERV RTL matches gate simulation: {rtl_stats.group()}")
