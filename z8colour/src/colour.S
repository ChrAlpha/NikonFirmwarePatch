/* Shared owner of the five recording colour-chain sites (M1, RGB curves, NLG, M2, Cpxs).
 *
 * CONFIG is read-only composition data:
 *   +00h  LOG_TYPE byte address, 0 without log-colour
 *   +08h  RGB curve tables, Log types 1 and 2       +18h  NLG tables, types 1 and 2
 *   +28h  M2 rows, types 1 and 2                     +38h  M1 bases, types 1 and 2
 *   +48h  Cpxs tables, types 1 and 2
 *   +58h  photo-lut provider block, 0 without photo-lut
 *
 * The provider block holds two lanes of 38h bytes, each +00h M1/M2 build context, +08h curve build
 * context, then the table pointers +10h RGB, +18h NLG, +20h M2 row, +28h M1 base, +30h Cpxs. A lane
 * serves only the build whose context (x19) equals the one it names; a table pointer of 0 keeps the
 * native or Log table. Lane 0 serves the still develop and lane 1 live view; they are independent, so
 * builds on different CPUs are served at the same time.
 *
 * The base part (up to colour_base_end) serves lane 0, then the movie Log selection. Every composition
 * installs it and, without photo-lut, only it. With photo-lut the sites enter the lane hooks after it,
 * which serve lane 1 and otherwise continue in the base hook of the same site.
 *
 * Movie Log selection: a native N-Log build (tone 1, M1 selector 1, M2 row 1) takes the type's table
 * when LOG_TYPE is 1 or 2; other LOG_TYPE values read as N-Log and keep the stock table.
 * Each hook replays its displaced instruction and returns to the original continuation.
 */
#define CONFIG 0x40981F00
#define P_CTX_M 0x00
#define P_CTX_C 0x08
#define P_RGB   0x10
#define P_NLG   0x18
#define P_M2    0x20
#define P_M1    0x28
#define P_CPXS  0x30
#define LANE1   0x38

/* x\t = type 1 or 2 from LOG_TYPE, or branch to \none; x\c = CONFIG. Uses only x\c and x\t. */
.macro LOGTYPE c, t, none
    ldr x\c, =CONFIG
    ldr x\t, [x\c]
    cbz x\t, \none
    ldrb w\t, [x\t]
    sub w\t, w\t, #1
    cmp w\t, #1
    b.hi \none
.endm

/* x\p = lane \lane table at \slot for the build context in x19 with context slot \ctx, or branch to \none. */
.macro PROVIDER p, s, ctx, slot, none, lane=0
    ldr x\p, =CONFIG
    ldr x\p, [x\p, #0x58]
    cbz x\p, \none
    ldr x\s, [x\p, #\lane+\ctx]
    cmp x\s, x19
    b.ne \none
    ldr x\p, [x\p, #\lane+\slot]
    cbz x\p, \none
.endm

.text
.balign 16
.global colour_rgb_hook
colour_rgb_hook:                    // 4111A508: movk x8, #0, lsl #48 (x8 = RGB table base, x9 = tone; flags set again at 4111A514)
    movk x8, #0, lsl #48
    stp x10, x11, [sp, #-16]!
    PROVIDER 10, 11, P_CTX_C, P_RGB, 1f
    mov x8, x10
    b 9f
1:  cmp x9, #1
    b.ne 9f
    LOGTYPE 10, 11, 9f
    add x10, x10, x11, lsl #3
    ldr x8, [x10, #0x08]
9:  ldp x10, x11, [sp], #16
    b original_4111A50C

.balign 16
.global colour_nlg_hook
colour_nlg_hook:                    // 4111A6D4: madd x1, x9, x2, x10 (x9, x10 dead after; x2 = 1CAh copy length)
    madd x1, x9, x2, x10
    stp x11, x12, [sp, #-16]!
    PROVIDER 10, 11, P_CTX_C, P_NLG, 1f
    mov x1, x10
    b 9f
1:  cmp w9, #1
    b.ne 9f
    LOGTYPE 10, 11, 9f
    add x10, x10, x11, lsl #3
    ldr x1, [x10, #0x18]
9:  ldp x11, x12, [sp], #16
    b original_4111A6D8

.balign 16
.global colour_m2_hook
colour_m2_hook:                     // 41116CB4: madd x11, x8, x11, x9 (x9 = 441A56A8 row base, x8 = row)
    stp x12, x13, [sp, #-32]!
    stp x14, x15, [sp, #16]
    mrs x15, nzcv
    cmp x8, #1
    b.ne 8f
    PROVIDER 12, 13, P_CTX_M, P_M2, 1f
    sub x9, x12, #0x12
    b 8f
1:  LOGTYPE 12, 13, 8f
    add x12, x12, x13, lsl #3
    ldr x9, [x12, #0x28]
    sub x9, x9, #0x12
8:  msr nzcv, x15
    ldp x14, x15, [sp, #16]
    ldp x12, x13, [sp], #32
    madd x11, x8, x11, x9
    b original_41116CB8

.balign 16
.global colour_m1_hook
colour_m1_hook:                     // 41116010: movk x14, #0, lsl #48 (x14 = 441A0A4A base, x13 = row, w11 = S+BCh)
    movk x14, #0, lsl #48
    stp x15, x16, [sp, #-32]!
    stp x17, x18, [sp, #16]
    mrs x16, nzcv
    cmp w11, #1
    b.ne 8f                         // N-Log: S+BCh = 1, row 2
    PROVIDER 15, 17, P_CTX_M, P_M1, 1f
    sub x14, x15, #0x24
    b 8f
1:  LOGTYPE 15, 17, 8f
    add x15, x15, x17, lsl #3
    ldr x14, [x15, #0x38]
    sub x14, x14, #0x24
8:  msr nzcv, x16
    ldp x17, x18, [sp, #16]
    ldp x15, x16, [sp], #32
    b original_41116014

.balign 16
.global colour_cpxs_hook
colour_cpxs_hook:                   // 4111A198: ldrb w21, [x19, #0x28], right after bl 411179CC (record -> ctx+A8h)
    stp x8, x9, [sp, #-32]!
    stp x10, x11, [sp, #16]
    mrs x11, nzcv
    PROVIDER 8, 9, P_CTX_C, P_CPXS, 1f
    str x8, [x19, #0xb0]            // the selected record's Pa..Pf table pointer
    b 8f
1:  ldr x8, [x19, #0x98]            // T, read by the stock code at 4111A188 too
    ldrb w9, [x8, #0x31]
    cmp w9, #1
    b.ne 8f
    LOGTYPE 8, 9, 8f
    add x8, x8, x9, lsl #3
    ldr x8, [x8, #0x48]
    str x8, [x19, #0xb0]
8:  msr nzcv, x11
    ldp x10, x11, [sp, #16]
    ldp x8, x9, [sp], #32
    ldrb w21, [x19, #0x28]
    b original_4111A19C
.ltorg
.global colour_base_end
colour_base_end:

/* Lane 1, installed with photo-lut: each hook serves the live-view lane or continues in the base hook,
 * which replays the displaced instruction itself. Registers and, where the base hook keeps them, the
 * flags are preserved on both paths. */
.balign 16
.global colour_lane_rgb_hook
colour_lane_rgb_hook:               // 4111A508 (flags set again at 4111A514)
    stp x10, x11, [sp, #-16]!
    PROVIDER 10, 11, P_CTX_C, P_RGB, 1f, LANE1
    mov x8, x10
    ldp x10, x11, [sp], #16
    b original_4111A50C
1:  ldp x10, x11, [sp], #16
    b colour_rgb_hook

.balign 16
.global colour_lane_nlg_hook
colour_lane_nlg_hook:               // 4111A6D4 (x2, x9, x10 still feed the displaced madd)
    stp x11, x12, [sp, #-16]!
    PROVIDER 11, 12, P_CTX_C, P_NLG, 1f, LANE1
    mov x1, x11
    ldp x11, x12, [sp], #16
    b original_4111A6D8
1:  ldp x11, x12, [sp], #16
    b colour_nlg_hook

.balign 16
.global colour_lane_m2_hook
colour_lane_m2_hook:                // 41116CB4
    stp x12, x13, [sp, #-16]!
    stp x14, x15, [sp, #-16]!
    mrs x14, nzcv
    cmp x8, #1
    b.ne 1f
    PROVIDER 12, 13, P_CTX_M, P_M2, 1f, LANE1
    sub x9, x12, #0x12
    madd x11, x8, x11, x9
    msr nzcv, x14
    ldp x14, x15, [sp], #16
    ldp x12, x13, [sp], #16
    b original_41116CB8
1:  msr nzcv, x14
    ldp x14, x15, [sp], #16
    ldp x12, x13, [sp], #16
    b colour_m2_hook

.balign 16
.global colour_lane_m1_hook
colour_lane_m1_hook:                // 41116010
    stp x15, x16, [sp, #-16]!
    stp x17, x18, [sp, #-16]!
    mrs x17, nzcv
    cmp w11, #1
    b.ne 1f
    PROVIDER 15, 16, P_CTX_M, P_M1, 1f, LANE1
    sub x14, x15, #0x24
    msr nzcv, x17
    ldp x17, x18, [sp], #16
    ldp x15, x16, [sp], #16
    b original_41116014
1:  msr nzcv, x17
    ldp x17, x18, [sp], #16
    ldp x15, x16, [sp], #16
    b colour_m1_hook

.balign 16
.global colour_lane_cpxs_hook
colour_lane_cpxs_hook:              // 4111A198
    stp x8, x9, [sp, #-16]!
    stp x10, x11, [sp, #-16]!
    mrs x10, nzcv
    PROVIDER 8, 9, P_CTX_C, P_CPXS, 1f, LANE1
    str x8, [x19, #0xb0]
    msr nzcv, x10
    ldp x10, x11, [sp], #16
    ldp x8, x9, [sp], #16
    ldrb w21, [x19, #0x28]
    b original_4111A19C
1:  msr nzcv, x10
    ldp x10, x11, [sp], #16
    ldp x8, x9, [sp], #16
    b colour_cpxs_hook
.ltorg
