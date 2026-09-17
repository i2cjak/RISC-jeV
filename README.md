# RISC-jeV

Jev is Turing complete. I made Jev parse boolean expressions. I tortured Jev into being a RISC-V CPU.

**[Run it](https://jev-riscv-production.up.railway.app)**

Write C, compile it, and run it on a simulated [SERV](https://github.com/olofk/serv) CPU. Watch the clock, instruction trace, signals, Jev requests, and cost.

Every logic gate evaluation asks Jev for its output. No cached answers, no correctness checks, no confidence threshold. Wrong answers become wire values. Independent gates share a request; each gets its own question.

Each run gets up to 5 minutes and a $0.05 estimated input budget at $0.042/MTok. It stops before sending a batch that may exceed the remaining budget. Output charges are not included.

Local setup: [docs/architecture.md](docs/architecture.md).
