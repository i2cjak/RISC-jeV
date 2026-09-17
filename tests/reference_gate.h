// Native Boolean gates are compiled only into the separate test executable.
inline unsigned reference_gate(unsigned op, unsigned a, unsigned b, unsigned s) {
    switch (op) {
        case 0: return !a;
        case 1: return a & b;
        case 2: return a | b;
        case 3: return a ^ b;
        default: return s ? b : a;
    }
}
