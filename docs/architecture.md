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

- **Run / Pause / Step gates / Reset:** controls the actual simulator. Step gates
  evaluates one batch of up to 64 independent gates, then pauses. Pause takes
  effect after the current request; Stop interrupts the run immediately.
- **Execution:** real instruction fetches, disassembly, PC and cycle count.
  The browser retains the last 300 rows; the server retains up to 12,000 events.
- **Clock speed:** simulated cycles per wall-clock second. While running it is
  sampled every 250 ms; after completion it shows the full-run average. Jev
  latency and simulator/control overhead are included; user pause time is
  excluded. There is no artificial execution delay.
- **Gate activity:** each cell is a real gate output in the current evaluation
  pass: white is 1, gray is 0, dark is unevaluated. Pending gates are highlighted.
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
  JSON, with separate entries for each batch. No API key reaches the browser.

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
3. The simulator walks gates in dependency order. Up to 64 gates at the same
   dependency level share an API request. Each physical gate has its own
   question, even when another gate has identical inputs. A dependent gate
   cannot be requested until its inputs have arrived from Jev.
4. The returned `true` or `false` choice directly drives that gate's wire.
   There is no comparison with Boolean truth, no confidence threshold, no
   correction, no retry of wrong answers and no result cache. Only an absent
   or non-Boolean answer is an error because it cannot represent a wire value.
5. Every combinational gate is asked again on both evaluation passes of every
   cycle, even when its inputs have not changed. Flip-flop updates, wiring,
   memory and memory-mapped output are handled locally.

The public run limit is five active wall-clock minutes, including API latency,
and $0.05 of estimated input cost at the supplied $0.042/MTok rate. Before each
request, the server reserves a conservative input-token allowance for the full
batch and refuses to send it if that would exceed the remaining budget. After
the response, reported usage replaces the reservation. Missing usage stops
the run. This is an input estimate, not a cap on the provider's complete invoice:
output charges are excluded and a request in flight when stopped may be billed.
The budget can expire during reset, before the first instruction.
The UI shows those real partial gate evaluations; it does not invent clock
progress or program output. User pauses do not consume the execution budget.
Clock speed counts completed simulated cycles only. Native Boolean evaluation
exists only in a separate `serv_reference` executable compiled by test targets;
`make build` and the deployed image do not build it, and the site cannot select it.
Startup and synthesis don't-care values are zero; this is a two-state simulation.

The custom simulator board has 64 KiB RAM at address zero, byte console output at
`0x10000000`, and a 32-bit exit status at `0x10000004`. Unmapped data reads return
zero and are acknowledged, including SERV's ignored FENCE read. This build
does not enable compressed instructions, CSRs, interrupts, or hardware multiply
and divide. No physical MCU or FPGA is flashed.

## Commands

```sh
uv run --env-file .env python run.py classify 'AB,01' 'A+B,01'
uv run --env-file .env python run.py run build/fibonacci.bin
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
executable, live gate dependencies, repeated questions, propagation of wrong and
low-confidence answers, execution deadlines, pause/stop, compiler isolation and
cooldowns. Routine tests mock API calls and do not spend API tokens.

`check-rtl` runs the original SERV Verilog independently and compares program
output, cycle counts and bus transaction counts against the gate simulator.
You can override `IVERILOG` and `VVP` with local executable paths. The original
`serv/` checkout is unchanged.
