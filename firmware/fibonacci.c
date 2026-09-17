#include "io.h"

volatile uint32_t count = 10;

int main(void) {
    uint32_t a = 0, b = 1;
    for (uint32_t i = 0; i < count; ++i) {
        uint32_t next = a + b;
        a = b;
        b = next;
        print_number(a);
    }
    return a == 55 ? 0 : 1;
}
