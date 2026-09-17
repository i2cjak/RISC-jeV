OUTPUT_ARCH(riscv)
ENTRY(_start)
MEMORY { RAM (rwx) : ORIGIN = 0, LENGTH = 64K }
SECTIONS {
    .text : { KEEP(*(.text.start)) *(.text .text.*) } > RAM
    .rodata : { *(.rodata .rodata.*) } > RAM
    .data : {
        __global_pointer$ = . + 0x800;
        *(.sdata .sdata.* .data .data.*)
    } > RAM
    .bss (NOLOAD) : {
        . = ALIGN(4);
        _bss_start = .;
        *(.sbss .sbss.* .bss .bss.*) *(COMMON)
        . = ALIGN(4);
        _bss_end = .;
    } > RAM
    _stack_top = ORIGIN(RAM) + LENGTH(RAM);
    ASSERT(_bss_end <= _stack_top - 4096, "Firmware leaves less than 4 KiB for stack")
}
