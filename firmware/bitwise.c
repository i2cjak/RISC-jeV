#include "io.h"

volatile uint32_t a = 5, b = 3;

int main(void) {
    print_number(a & b);
    print_number(a | b);
    print_number(a ^ b);
    print_number(a << b);
    print_number(a >> 1);
    return 0;
}
