UV ?= uv
RISCV_PREFIX ?= riscv64-unknown-elf-
RISCV_CC = $(RISCV_PREFIX)gcc
RISCV_OBJCOPY = $(RISCV_PREFIX)objcopy
RISCV_OBJDUMP = $(RISCV_PREFIX)objdump
RVFLAGS = -march=rv32i -mabi=ilp32 -O2 -ffreestanding -fno-builtin -fno-pic -msmall-data-limit=0 -Wall -Wextra -Werror
RVLINK = -nostdlib -nostartfiles -Wl,--no-relax -T firmware/link.ld
ENVFILE = $(if $(wildcard .env),--env-file .env,)
RTL = $(wildcard serv/rtl/*.v)

.PHONY: all build run gates classify test check-rtl serve clean
all: build
build: build/serv_sim build/compiler_sandbox build/demo.bin build/isa.bin build/fibonacci.bin build/sum.bin build/bitwise.bin

build/compiler_sandbox: sandbox.c
	mkdir -p build
	$(CC) -O2 -std=c11 -Wall -Wextra -Werror sandbox.c -o $@

build/serv.json: $(RTL) Makefile pyproject.toml uv.lock
	mkdir -p build
	$(UV) run --locked yowasp-yosys -Q -T -p 'read_verilog -D SERV_CLEAR_RAM serv/rtl/*.v; hierarchy -check -top serv_rf_top -chparam WITH_CSR 0; synth -top serv_rf_top -flatten -noabc; dffunmap; abc -g AND,OR,XOR,MUX; clean; check -assert; write_json build/serv.json' > build/synthesis.log 2>&1

build/serv_netlist.h: build/serv.json netlist.py jev.py run.py
	$(UV) run --locked python run.py netlist $< $@

build/serv_sim: sim.cpp build/serv_netlist.h
	$(CXX) -O3 -std=c++17 -Wall -Wextra -Werror -I build sim.cpp -o $@

build/%.elf: firmware/%.c firmware/start.S firmware/link.ld firmware/io.h
	mkdir -p build
	$(RISCV_CC) $(RVFLAGS) $(RVLINK) -Wl,-Map,$(@:.elf=.map) firmware/start.S $< -o $@
	$(RISCV_OBJDUMP) -d $@ > $(@:.elf=.dis)

build/%.bin: build/%.elf
	$(RISCV_OBJCOPY) -O binary $< $@

build/isa.elf: firmware/isa.S firmware/start.S firmware/link.ld
	mkdir -p build
	$(RISCV_CC) $(RVFLAGS) $(RVLINK) firmware/start.S $< -o $@
	$(RISCV_OBJDUMP) -d $@ > $(@:.elf=.dis)

run: build
	$(UV) run --locked $(ENVFILE) python run.py run

gates:
	$(UV) run --locked $(ENVFILE) python run.py gates

classify:
	$(UV) run --locked $(ENVFILE) python run.py classify 'AB,01' 'A+B,01'

test: build
	$(UV) run --locked python -m unittest discover -s tests -v

check-rtl: build
	$(UV) run --locked python tests/check_rtl.py

BIND ?= 127.0.0.1:5077
serve: build
	$(UV) run --locked $(ENVFILE) gunicorn --workers 1 --threads 12 --timeout 120 --bind $(BIND) app:app

clean:
	rm -rf build

.SECONDARY:
