# Jev / SERV

Public site: https://jev-riscv-production.up.railway.app

The source editor compiles C on the server, then runs the result on the simulated
CPU. Share program copies a URL containing the source; the recipient can edit
and run it. No shared program runs automatically when its link is opened.

Visitor compilation uses a separate filesystem, no network, an empty environment,
and CPU/memory/output/time limits. Railway uses a chroot with a dedicated
unprivileged UID and seccomp; local Linux uses Bubblewrap plus seccomp. A compiler
error is returned before making a Jev request. Runs stop after five minutes of
execution (including API latency), 250,000 cycles, or a $0.05 estimated input
budget, whichever comes first. Paused stepping time is
excluded. The server allows six concurrent runs and 12 starts per IP per minute.
Custom compilation has a 30-second per-IP cooldown, a shared limit of 20 per
minute across the server, and one active compiler. Precompiled examples do not
consume the compilation quota. The browser shows the cooldown; the server
enforces it even if the browser is bypassed or reloaded.

Compiles C to RV32I and runs it on a gate-level simulation of the SERV CPU in
`serv/`. Jev classifies Boolean gate outputs through TypeSafe's
[System One API](https://docs.typesafe.ai/api). The black-and-white Flask site
shows execution, measured clock speed, input cost, timing traces, and request logs.

## Run

Requirements: `uv`, `make`, a C++17 compiler, and `riscv64-unknown-elf-gcc` with
binutils. Yosys, Flask, and Gunicorn are installed locally from `uv.lock`.

```sh
uv sync --locked
make build
make classify
make run
make serve                         # http://127.0.0.1:5077
```

Set `TYPESAFE_API_KEY` in the environment, or in the ignored `.env` file with
permissions `0600`. The Make targets load `.env` when present. The key stays on
the server; request logs contain no authentication headers.

The current detached server listens on localhost and the Tailnet address:

- http://127.0.0.1:5077
- http://100.64.84.41:5077

Start the same detached configuration after a reboot:

```sh
uv run --env-file .env gunicorn --daemon \
  --workers 1 --threads 12 --timeout 120 \
  --bind 127.0.0.1:5077 --bind 100.64.84.41:5077 \
  --pid build/web.pid --error-logfile build/web.log app:app
```

Use one worker: runs and event streams share in-process state. The server binds
only to localhost and the Tailnet interface. `make clean` removes build outputs,
and the server PID/log files; stop the server first.

## Site

- **Run / Pause / Step / Reset:** controls the actual simulator. Step executes
  one instruction, then pauses at the next fetch. The highlighted instruction
  is fetched but has not executed yet while paused.
- **Cached Jev / Fresh Jev:** cached mode is the default and makes no new API
  calls when tables are available. Fresh Jev requests all 22 choices again and
  replaces the shared cache. A missing cache is populated automatically.
- **Execution:** real instruction fetches, disassembly, PC and cycle count.
  The browser retains the last 300 rows; the server retains up to 12,000 events.
- **Clock speed:** simulated cycles per wall-clock second. While running it is
  sampled every 250 ms; after completion it shows the full-run average. Jev
  latency and simulator/control overhead are included; user pause time is
  excluded. There is no artificial execution delay.
- **Gate activity:** each cell is a real gate output sampled at instruction
  fetches and halt: white is 1, gray is 0, dark means no sample yet. The outputs
  are computed using the cached Jev lookup tables.
- **Timing:** the last 64 sampled cycles (32 on mobile). CLK depicts the
  simulator's clock phases. FETCH, DATA, SERIAL, RS1 and RD come from actual
  netlist signals sampled before each rising edge. Hover to inspect a cycle and
  the active bit in SERV's 32-bit serial datapath. SERIAL marks an active bit
  counter; RS1 and RD are the one-bit register read/write data signals.
- **Est. input cost:** reported input tokens × `$0.042 / 1,000,000`, using the
  supplied estimate. This excludes output-token pricing and other charges.
  Only responses received before the deadline contribute reported usage. An
  in-flight request at the deadline may still be billed by the provider.
- **Jev requests:** model, latency, classifications, and full request/response
  JSON. Cached evidence is labeled, and cached runs show zero new tokens and
  input cost. No API key reaches the browser.

The interface uses the Berkeley Mono font copied from the local PiCarrier
project. The font binary is excluded from the public repository. To use a
licensed copy locally, place it at `static/fonts/berkeley-mono-variable.woff2`;
otherwise the interface falls back to a system monospace font. It uses local
assets, plain JavaScript, server-sent events, bounded
tables, and canvas rendering without a frontend framework or build step.

## How execution works

1. GCC compiles the examples for `-march=rv32i -mabi=ilp32`.
2. Yosys flattens `serv_rf_top` with CSRs disabled and maps its logic to NOT,
   AND, OR, XOR and MUX gates. The register-file RAM is also mapped into gates
   and flip-flops. The current netlist has 4,637 gates and 1,190 flip-flops.
3. Jev answers all 22 combinations of gate type and input bits in one request:
   2 NOT, 4 AND, 4 OR, 4 XOR and 8 MUX choices.
4. Those `true` / `false` choices become runtime lookup tables. There is no
   comparison with Boolean truth, confidence threshold, correction or retry
   based on an answer. An absent or non-Boolean choice cannot represent a bit
   and stops the run.
5. Every combinational gate uses the corresponding lookup entry on each of the
   two evaluation passes per cycle. Flip-flop updates, wiring, memory and
   memory-mapped output are handled locally.

The cache in `build/jev_truth_tables.json` stores the original response, usage,
request fingerprint and timestamp. It is reused across programs and visitors
with the same prompt/model selection. Concurrent cache misses share one fill.
A fresh request replaces the cache; already-running simulations keep their own
snapshot. Restarting the process retains the on-disk cache; replacing the
container can discard it. It is excluded from Git and deployment uploads.

The public run limit remains five active wall-clock minutes, 250,000 cycles and
$0.05 of estimated new input cost at $0.042/MTok. Pauses do not consume runtime.
Cached runs have no new Jev input cost. Before a fresh classification request,
the server reserves a conservative token allowance; reported usage replaces it
after the response. Output charges and unreported in-flight requests are excluded.
The simulator's clock speed counts completed simulated cycles only.

Native Boolean evaluation exists only in the separate `serv_reference` test
executable. The deployed simulator always uses Jev's table values; it has no
native fallback. Startup and synthesis don't-care values are zero.

The custom simulator board has 64 KiB RAM at address zero, byte console output at
`0x10000000`, and a 32-bit exit status at `0x10000004`. Unmapped data reads return
zero and are acknowledged, including SERV's ignored FENCE read. This build
does not enable compressed instructions, CSRs, interrupts, or hardware multiply
and divide. No physical MCU or FPGA is flashed.

## Commands

```sh
uv run --env-file .env python run.py classify 'AB,01' 'A+B,01'
uv run --env-file .env python run.py run build/fibonacci.bin
uv run --env-file .env python run.py run build/fibonacci.bin --fresh
```

To add a program, save `firmware/name.c`, then run `make build/name.bin` and
`uv run --env-file .env python run.py run build/name.bin`. `firmware/io.h`
provides console output without a C library. Add its name to `PROGRAMS` in
`app.py` and the Makefile's `build` target to expose it in the site.

## Verification

```sh
make test
node --check static/app.js
uvx ruff check app.py engine.py jev.py netlist.py run.py tests
make check-rtl                     # requires Icarus Verilog (iverilog + vvp)
```

Tests cover compiled code and RV32I instructions with the separate native test
executable, cached gate values, fresh requests, acceptance of wrong and low-confidence
choices, execution deadlines, instruction stepping, compiler isolation and
cooldowns. Routine tests mock API calls and do not spend API tokens.

`check-rtl` runs the original SERV Verilog independently and compares program
output, cycle counts and bus transaction counts against the gate simulator.
You can override `IVERILOG` and `VVP` with local executable paths. The original
`serv/` checkout is unchanged.
