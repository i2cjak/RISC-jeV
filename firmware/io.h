#include <stdint.h>
static inline void putchar_mmio(char c) {
    *(volatile uint8_t *)0x10000000 = (uint8_t)c;
}
static inline void print_number(uint32_t value) {
    // Subtraction avoids a division runtime on RV32I without the M extension.
    uint32_t divisor = 1000000000;
    int started = 0;
    while (divisor) {
        unsigned digit = 0;
        while (value >= divisor) { value -= divisor; ++digit; }
        if (digit || started || divisor == 1) {
            putchar_mmio('0' + digit);
            started = 1;
        }
        // Constant division is provided explicitly; no libgcc dependency.
        switch (divisor) {
            case 1000000000: divisor = 100000000; break;
            case 100000000: divisor = 10000000; break;
            case 10000000: divisor = 1000000; break;
            case 1000000: divisor = 100000; break;
            case 100000: divisor = 10000; break;
            case 10000: divisor = 1000; break;
            case 1000: divisor = 100; break;
            case 100: divisor = 10; break;
            case 10: divisor = 1; break;
            default: divisor = 0;
        }
    }
    putchar_mmio('\n');
}
