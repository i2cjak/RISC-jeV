# RISC-jeV

Jev is Turing complete. I made Jev parse boolean expressions. I tortured Jev into being a RISC-V CPU.

**[Run it](https://jev-riscv-production.up.railway.app)**

Write C, compile it, and run it on a simulated [SERV](https://github.com/olofk/serv) CPU. Watch the clock, instruction trace, signals, Jev requests, and cost.

Jev classifies 22 gate/input combinations. The simulator caches those answers and uses them for every logic gate.

Local setup: [docs/architecture.md](docs/architecture.md).
