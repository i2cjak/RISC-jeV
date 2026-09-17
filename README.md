# RISC-jeV

Jev is Turing complete. I made Jev parse boolean expressions. I tortured Jev into being a RISC-V CPU.

**[Run it](https://jev-riscv-production.up.railway.app)**

Write C, compile it, and run it on a simulated [SERV](https://github.com/olofk/serv) CPU. Watch the clock, instruction trace, signals, Jev requests, and cost.

Jev answers 22 Boolean gate/input combinations. Those answers become cached lookup tables used by every simulated logic gate. Cached runs make no new API calls. Fresh Jev replaces the tables with a new response. No correctness checks or confidence threshold.

Each run gets up to 5 minutes and a $0.05 estimated input budget at $0.042/MTok. It stops before sending a batch that may exceed the remaining budget. Output charges are not included.

Local setup: [docs/architecture.md](docs/architecture.md).
