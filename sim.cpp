#include "serv_netlist.h"
#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>

#ifdef SERV_REFERENCE_TEST
#include "tests/reference_gate.h"
#endif

// Wiring, storage and host bus services are native. Each combinational gate
// evaluation waits for its own fresh Jev choice before driving an output wire.
class CPU {
public:
    std::array<uint8_t, NET_COUNT> nets{};
    std::array<uint8_t, FLOPS.size()> next{};
    uint64_t evaluations = 0;

    CPU() {
        nets[1] = 1;
        for (auto init : INITIAL) nets[init.q] = init.d;
    }

    template<size_t N> uint32_t read(const std::array<unsigned, N> &port) const {
        uint32_t value = 0;
        for (unsigned i = 0; i < N; ++i) value |= uint32_t(nets[port[i]]) << i;
        return value;
    }

    template<size_t N> void write(const std::array<unsigned, N> &port, uint32_t value) {
        for (unsigned i = 0; i < N; ++i) nets[port[i]] = (value >> i) & 1;
    }

    void evaluate(uint64_t cycle, unsigned phase) {
#ifdef SERV_REFERENCE_TEST
        (void)cycle;
        (void)phase;
        for (auto gate : GATES) {
            nets[gate.y] = reference_gate(gate.op, nets[gate.a], nets[gate.b], nets[gate.s]);
            ++evaluations;
        }
#else
        constexpr std::array<const char *, 5> names{"NOT", "AND", "OR", "XOR", "MUX"};
        for (unsigned start = 0; start < GATES.size();) {
            unsigned end = start + 1;
            // Only gates in one dependency level may share a request. Never
            // merge gates with identical expressions/inputs or reuse answers.
            while (end < GATES.size() && end - start < 64 && GATES[end].level == GATES[start].level) ++end;
            std::cout << "{\"type\":\"gates\",\"cycle\":" << cycle << ",\"phase\":" << phase
                      << ",\"level\":" << GATES[start].level << ",\"gates\":" << evaluations
                      << ",\"total_gates\":" << GATES.size() << ",\"batch\":[";
            for (unsigned index = start; index < end; ++index) {
                const auto gate = GATES[index];
                if (index != start) std::cout << ',';
                std::cout << "{\"id\":" << index << ",\"gate\":\"" << names[gate.op] << "\",\"bits\":\""
                          << unsigned(nets[gate.a]);
                if (gate.arity >= 2) std::cout << unsigned(nets[gate.b]);
                if (gate.arity == 3) std::cout << unsigned(nets[gate.s]);
                std::cout << "\"}";
            }
            std::cout << "]}" << std::endl;
            std::string choices;
            if (!std::getline(std::cin, choices)) throw std::runtime_error("Gate input pipe closed");
            if (choices.size() != end - start || choices.find_first_not_of("01") != std::string::npos)
                throw std::runtime_error("Expected one returned bit per gate");
            for (unsigned index = start; index < end; ++index) nets[GATES[index].y] = choices[index - start] - '0';
            evaluations += end - start;
            start = end;
        }
#endif
    }

    void edge() {
        // Sample every D before updating any Q: simultaneous positive edge.
        for (unsigned i = 0; i < FLOPS.size(); ++i) next[i] = nets[FLOPS[i].d];
        for (unsigned i = 0; i < FLOPS.size(); ++i) nets[FLOPS[i].q] = next[i];
    }
};

class Waveform {
    std::array<std::array<int64_t, 7>, 64> samples{};
    uint64_t count = 0;
public:
    void sample(const CPU &cpu, uint64_t cycle) {
        const unsigned ring = cpu.read(trace_count_ring);
        int bit = -1;
        for (unsigned lane = 0; lane < 4; ++lane)
            if (ring & (1u << lane)) bit = 4 * cpu.read(trace_count_high) + lane;
        samples[count++ % samples.size()] = {
            int64_t(cycle), cpu.read(o_ibus_cyc), cpu.read(o_dbus_cyc),
            ring != 0, cpu.read(trace_rs1), cpu.read(trace_rd), bit,
        };
    }
    void json() const {
        std::cout << "[";
        const uint64_t start = count > samples.size() ? count - samples.size() : 0;
        for (uint64_t index = start; index < count; ++index) {
            if (index != start) std::cout << ',';
            std::cout << '[';
            for (unsigned field = 0; field < 7; ++field) {
                if (field) std::cout << ',';
                std::cout << samples[index % samples.size()][field];
            }
            std::cout << ']';
        }
        std::cout << "]";
    }
};

int main(int argc, char **argv) try {
    if (argc != 3) throw std::runtime_error("Usage: serv_sim FIRMWARE.bin MAX_CYCLES");
#ifdef SERV_REFERENCE_TEST
    constexpr bool interactive = false;
#else
    constexpr bool interactive = true;
#endif
    const bool trace = std::getenv("SERV_TRACE") != nullptr;
    const std::string cycles_arg(argv[2]);
    if (cycles_arg.empty() || cycles_arg.find_first_not_of("0123456789") != std::string::npos)
        throw std::runtime_error("MAX_CYCLES must be a positive integer");
    const uint64_t limit = std::stoull(cycles_arg);
    if (!limit) throw std::runtime_error("MAX_CYCLES must be positive");
    std::ifstream firmware(argv[1], std::ios::binary);
    if (!firmware) throw std::runtime_error("Cannot open firmware");
    std::vector<uint8_t> ram(std::istreambuf_iterator<char>(firmware), {});
    if (ram.empty() || ram.size() > 65536) throw std::runtime_error("Firmware must fit in 64 KiB RAM");
    ram.resize(65536, 0);
    CPU cpu;
    Waveform waveform;
#ifdef SERV_REFERENCE_TEST
    const auto execution_started = std::chrono::steady_clock::now();
#endif
    uint64_t fetches = 0, loads = 0, stores = 0;
    auto word = [&](uint32_t address) {
        address &= ~3u;
        if (address > ram.size() - 4) throw std::runtime_error("Read outside RAM at " + std::to_string(address));
        return uint32_t(ram[address]) | uint32_t(ram[address + 1]) << 8 |
               uint32_t(ram[address + 2]) << 16 | uint32_t(ram[address + 3]) << 24;
    };

    for (uint64_t cycle = 0; cycle < limit; ++cycle) {
#ifdef SERV_REFERENCE_TEST
        if ((cycle & 255u) == 0 && std::chrono::steady_clock::now() - execution_started >= std::chrono::seconds(5)) {
            std::cerr << "SERV TIMEOUT: 5-second execution limit reached; cycles=" << cycle << '\n';
            return 2;
        }
#endif
        const bool reset = cycle < 8;
        cpu.write(i_rst, reset);
        cpu.write(i_ibus_ack, 0);
        cpu.write(i_dbus_ack, 0);
        cpu.write(i_ibus_rdt, 0);
        cpu.write(i_dbus_rdt, 0);
        cpu.evaluate(cycle, 0);
        bool halted = false;
        uint32_t exit_code = 0;
        if (!reset && cpu.read(o_ibus_cyc)) {
            uint32_t address = cpu.read(o_ibus_adr);
            if (trace) std::cerr << "I " << cycle << " " << std::hex << address << std::dec << '\n';
            if (address & 3) throw std::runtime_error("Misaligned instruction fetch");
            uint32_t instruction = word(address);
            if (interactive) {
                std::cout << "{\"type\":\"fetch\",\"pc\":" << address << ",\"word\":" << instruction
                          << ",\"cycle\":" << cycle << ",\"gates\":" << cpu.evaluations << ",\"wave\":";
                waveform.json();
                std::cout << "}" << std::endl;
            }
            cpu.write(i_ibus_rdt, instruction);
            cpu.write(i_ibus_ack, 1);
            ++fetches;
        }
        if (!reset && cpu.read(o_dbus_cyc)) {
            const uint32_t address = cpu.read(o_dbus_adr) & ~3u;
            const uint32_t data = cpu.read(o_dbus_dat);
            const uint32_t select = cpu.read(o_dbus_sel);
            if (trace) std::cerr << "D " << cycle << " " << cpu.read(o_dbus_we) << " " << std::hex << address << " " << data << " " << select << std::dec << '\n';
            if (cpu.read(o_dbus_we)) {
                ++stores;
                if (address == 0x10000000) {
                    if (select & 1) {
                        if (interactive) std::cout << "{\"type\":\"output\",\"byte\":" << (data & 0xff) << "}" << std::endl;
                        else std::cout << char(data & 0xff) << std::flush;
                    }
                } else if (address == 0x10000004 && select == 15) {
                    halted = true;
                    exit_code = data;
                } else if (address <= ram.size() - 4) {
                    for (unsigned lane = 0; lane < 4; ++lane)
                        if ((select >> lane) & 1) ram[address + lane] = (data >> (8 * lane)) & 0xff;
                } else {
                    throw std::runtime_error("Write outside RAM/MMIO at " + std::to_string(address));
                }
            } else {
                ++loads;
                // Unmapped data reads return the bus default, zero. SERV's
                // FENCE decoding can issue an ignored read using stale address
                // bits; it must still receive an acknowledgement.
                cpu.write(i_dbus_rdt, address <= ram.size() - 4 ? word(address) : 0);
            }
            cpu.write(i_dbus_ack, 1);
        }
        cpu.evaluate(cycle, 1);
        if (interactive) waveform.sample(cpu, cycle);
        cpu.edge();
        if (interactive) {
            std::cout << "{\"type\":\"cycle\",\"cycle\":" << cycle + 1 << ",\"gates\":" << cpu.evaluations << ",\"wave\":";
            waveform.json();
            std::cout << "}" << std::endl;
        }
        if (halted) {
            if (interactive) {
                std::cout << "{\"type\":\"halt\",\"exit_code\":" << exit_code
                    << ",\"cycle\":" << cycle + 1 << ",\"gates\":" << cpu.evaluations << ",\"wave\":";
                waveform.json();
                std::cout << "}" << std::endl;
            }
            std::cerr << "SERV " << (exit_code ? "FAIL" : "PASS") << ": exit=" << exit_code
                      << " cycles=" << cycle + 1 << " fetches=" << fetches << " loads=" << loads
                      << " stores=" << stores << " gate_evaluations=" << cpu.evaluations << '\n';
            return exit_code ? 1 : 0;
        }
    }
    std::cerr << "SERV TIMEOUT: cycles=" << limit << " pc=0x" << std::hex << cpu.read(o_ibus_adr)
              << std::dec << " gate_evaluations=" << cpu.evaluations << '\n';
    return 2;
} catch (const std::exception &error) {
    std::cerr << "Simulation error: " << error.what() << '\n';
    return 1;
}
