#include <stdint.h>

static void puts_mmio(const char *text) {
    while (*text) *(volatile uint8_t *)0x10000000 = (uint8_t)*text++;
}

static volatile uint32_t iterations = 10;
static volatile uint32_t values[10];
static volatile int32_t negative = -123;
static volatile uint32_t shift = 3;
static volatile uint8_t bytes[4];
static volatile uint16_t halves[2];

int main(void) {
    uint32_t a = 0, b = 1;
    for (uint32_t i = 0; i < iterations; ++i) {
        uint32_t next = a + b;
        a = b;
        b = next;
        values[i] = a;
    }
    if (a != 55 || values[7] != 21) return 1;
    if ((negative >> shift) != -16) return 2;
    if (((uint32_t)negative >> shift) != 0x1ffffff0u) return 3;
    if ((a << shift) != 440 || (a ^ b) != 110) return 4;
    if ((a & b) != 17 || (a | b) != 127 || b - a != 34) return 5;
    if (negative >= 0 || (uint32_t)negative < a) return 6;
    bytes[0] = 0x80; bytes[1] = 0xa5; bytes[2] = 0x5a; bytes[3] = 0xff;
    halves[0] = 0x8123; halves[1] = 0xabcd;
    if (bytes[0] != 128 || bytes[1] != 165 || bytes[2] != 90 || bytes[3] != 255) return 7;
    if (halves[0] != 0x8123 || halves[1] != 0xabcd) return 8;
    if (*(volatile int8_t *)&bytes[0] != -128 || *(volatile int16_t *)&halves[0] != -32477) return 9;
    puts_mmio("Hello from Jev-backed SERV!\n");
    puts_mmio("RV32I checks passed: fib(10)=55\n");
    return 0;
}
