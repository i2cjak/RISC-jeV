// Exercise RV32I operations using explicit instructions and independent answers.
.section .text
.global main
main:
    li a0, 1
    li t0, -1
    li t1, 31
    srl t2, t0, t1
    li t3, 1
    bne t2, t3, fail
    sra t2, t0, t1
    bne t2, t0, fail
    sll t2, t3, t1
    li t4, 0x80000000
    bne t2, t4, fail

    li a0, 2
    li t1, 1
    add t2, t0, t1
    bne t2, zero, fail
    sub t2, zero, t1
    bne t2, t0, fail
    slt t2, t0, t1
    bne t2, t1, fail
    sltu t2, t0, t1
    bne t2, zero, fail
    slti t2, t0, 0
    bne t2, t1, fail
    sltiu t2, t0, 1
    bne t2, zero, fail

    li a0, 3
    li t0, 0x5a5a5a5a
    li t1, 0xf0f0f0f0
    xor t2, t0, t1
    li t3, 0xaaaaaaaa
    bne t2, t3, fail
    and t2, t0, t1
    li t3, 0x50505050
    bne t2, t3, fail
    or t2, t0, t1
    li t3, 0xfafafafa
    bne t2, t3, fail
    andi t2, t0, 255
    li t3, 90
    bne t2, t3, fail
    ori t2, t2, 128
    li t3, 218
    bne t2, t3, fail
    xori t2, t2, -1
    li t3, -219
    bne t2, t3, fail
    slli t2, t2, 8
    srai t2, t2, 8
    bne t2, t3, fail
    srli t2, t3, 24
    li t3, 255
    bne t2, t3, fail

    li a0, 4
    li t0, -2
    li t1, 1
    bge t0, t1, fail
    blt t1, t0, fail
    bltu t0, t1, fail
    bgeu t1, t0, fail
    beq t0, t1, fail
    beq t0, t0, 1f
    j fail
1:
    blt t0, t1, 2f
    j fail
2:
    bge t1, t0, 3f
    j fail
3:
    bltu t1, t0, 4f
    j fail
4:
    bgeu t0, t1, 5f
    j fail
5:
    li a0, 5
    la t0, scratch
    li t1, 0x80fe7f01
    sw t1, 0(t0)
    lw t2, 0(t0)
    bne t2, t1, fail
    lb t2, 3(t0)
    li t3, -128
    bne t2, t3, fail
    lbu t2, 2(t0)
    li t3, 254
    bne t2, t3, fail
    lh t2, 2(t0)
    li t3, -32514
    bne t2, t3, fail
    lhu t2, 2(t0)
    li t3, 33022
    bne t2, t3, fail
    li t1, 0xaa
    sb t1, 1(t0)
    li t1, 0xbbcc
    sh t1, 2(t0)
    lw t2, 0(t0)
    li t3, 0xbbccaa01
    bne t2, t3, fail

    li a0, 6
    auipc t0, 0
    la t1, pc_check
pc_check:
    sub t2, t1, t0
    li t3, 12
    bne t2, t3, fail
    jal t0, jal_target
jal_return:
    j fail
jal_target:
    la t1, jal_return
    bne t0, t1, fail
    la t1, jalr_target
    ori t1, t1, 1
    jalr t0, 0(t1)
jalr_return:
    j fail
jalr_target:
    la t1, jalr_return
    bne t0, t1, fail
    fence
    li a0, 0
fail:
    ret

.section .bss
.balign 4
scratch:
    .space 4
