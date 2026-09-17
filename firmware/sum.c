#include "io.h"

volatile uint32_t numbers[] = {3, 7, 11, 19, 2};

int main(void) {
    uint32_t sum = 0;
    for (unsigned i = 0; i < 5; ++i) {
        sum += numbers[i];
        print_number(sum);
    }
    return sum == 42 ? 0 : 1;
}
