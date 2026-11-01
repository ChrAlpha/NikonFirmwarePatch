/* SPDX-License-Identifier: GPL-3.0-or-later */
.section .text.hooks,"ax",%progbits
.balign 4
.macro save_registers
    sub sp, sp, #256
    stp x0, x1, [sp, #0]
    stp x2, x3, [sp, #16]
    stp x4, x5, [sp, #32]
    stp x6, x7, [sp, #48]
    stp x8, x9, [sp, #64]
    stp x10, x11, [sp, #80]
    stp x12, x13, [sp, #96]
    stp x14, x15, [sp, #112]
    stp x16, x17, [sp, #128]
    stp x18, x19, [sp, #144]
    stp x20, x21, [sp, #160]
    stp x22, x23, [sp, #176]
    stp x24, x25, [sp, #192]
    stp x26, x27, [sp, #208]
    stp x28, x29, [sp, #224]
    str x30, [sp, #240]
    mrs x9, nzcv
    str x9, [sp, #248]
.endm
.macro restore_registers
    ldr x9, [sp, #248]
    msr nzcv, x9
    ldp x0, x1, [sp, #0]
    ldp x2, x3, [sp, #16]
    ldp x4, x5, [sp, #32]
    ldp x6, x7, [sp, #48]
    ldp x8, x9, [sp, #64]
    ldp x10, x11, [sp, #80]
    ldp x12, x13, [sp, #96]
    ldp x14, x15, [sp, #112]
    ldp x16, x17, [sp, #128]
    ldp x18, x19, [sp, #144]
    ldp x20, x21, [sp, #160]
    ldp x22, x23, [sp, #176]
    ldp x24, x25, [sp, #192]
    ldp x26, x27, [sp, #208]
    ldp x28, x29, [sp, #224]
    ldr x30, [sp, #240]
    add sp, sp, #256
.endm
.macro invoke target, registers=0
    save_registers
    .if \registers
    mov x0, sp
    .endif
    bl \target
    restore_registers
.endm
.global z8_photo_lut_jpeg_entry
.type z8_photo_lut_jpeg_entry,%function
z8_photo_lut_jpeg_entry:
    b z8_photo_lut_jpeg
.size z8_photo_lut_jpeg_entry, .-z8_photo_lut_jpeg_entry
.global z8_photo_lut_output_alloc_entry
.type z8_photo_lut_output_alloc_entry,%function
z8_photo_lut_output_alloc_entry:
    /* The native output constructor retains this capture's settings in x19. */
    mov x3, x19
    b z8_photo_lut_output_alloc
.size z8_photo_lut_output_alloc_entry, .-z8_photo_lut_output_alloc_entry
.macro jpeg_preview_entry name
.global z8_photo_lut_\name\()_entry
.type z8_photo_lut_\name\()_entry,%function
z8_photo_lut_\name\()_entry:
    mov x2, x21
    mov x3, x22
    b z8_photo_lut_\name
.size z8_photo_lut_\name\()_entry, .-z8_photo_lut_\name\()_entry
.endm
jpeg_preview_entry medium
jpeg_preview_entry small
/* Both native JPEG APP1 XMP composer calls (0x406c35c4, 0x406c40ec) keep
 * the per-file header context in x19; pass it as the sixth argument. */
.global z8_photo_lut_xmp_entry
.type z8_photo_lut_xmp_entry,%function
z8_photo_lut_xmp_entry:
    mov x5, x19
    b z8_photo_lut_xmp
.size z8_photo_lut_xmp_entry, .-z8_photo_lut_xmp_entry
/* Replaces `bl 0x4032e19c` at 0x4032b748 in the kind-2 glyph builder; the
 * label text (x8) and its byte length (w9) are live there. */
.global z8_photo_lut_text_font_entry
.type z8_photo_lut_text_font_entry,%function
z8_photo_lut_text_font_entry:
    mov x2, x8
    mov w3, w9
    b z8_photo_lut_text_font
.size z8_photo_lut_text_font_entry, .-z8_photo_lut_text_font_entry
.global z8_photo_lut_boot_entry
.type z8_photo_lut_boot_entry,%function
z8_photo_lut_boot_entry:
    invoke z8_photo_lut_boot
    mov x19, #0xcfa4
    b z8_photo_lut_boot_resume
.size z8_photo_lut_boot_entry, .-z8_photo_lut_boot_entry

.global z8_photo_lut_frame_entry
.type z8_photo_lut_frame_entry,%function
z8_photo_lut_frame_entry:
    invoke z8_photo_lut_frame, 1
    /* Replay the displaced PC-relative tbnz using explicit native targets. */
    tbz w0, #0, 1f
    b z8_photo_lut_frame_success
1:  b z8_photo_lut_frame_resume
.size z8_photo_lut_frame_entry, .-z8_photo_lut_frame_entry

.global z8_photo_lut_queue_entry
.type z8_photo_lut_queue_entry,%function
z8_photo_lut_queue_entry:
    save_registers
    mov x0, sp
    bl z8_photo_lut_queue
    cbnz w0, 1f
    restore_registers
    b z8_photo_lut_queue_resume
1:  restore_registers
    mov w0, #0
    b z8_photo_lut_queue_skip
.size z8_photo_lut_queue_entry, .-z8_photo_lut_queue_entry

.global z8_photo_lut_mode_entry
.type z8_photo_lut_mode_entry,%function
z8_photo_lut_mode_entry:
    invoke z8_photo_lut_mode_enter
    .inst 0xd101c3ff /* native sub sp,sp,#0x70 */
    b z8_photo_lut_mode_resume
.size z8_photo_lut_mode_entry, .-z8_photo_lut_mode_entry

.global z8_photo_lut_mode_exit_entry
.type z8_photo_lut_mode_exit_entry,%function
z8_photo_lut_mode_exit_entry:
    invoke z8_photo_lut_mode_exit
    .inst 0x120002a0 /* native and w0,w21,#1 */
    b z8_photo_lut_mode_exit_resume
.size z8_photo_lut_mode_exit_entry, .-z8_photo_lut_mode_exit_entry

.global z8_photo_lut_reset_entry
.type z8_photo_lut_reset_entry,%function
z8_photo_lut_reset_entry:
    invoke z8_photo_lut_queue_reset, 1
    mov x0, x20
    b z8_photo_lut_reset_resume
.size z8_photo_lut_reset_entry, .-z8_photo_lut_reset_entry

.global z8_photo_lut_construct_entry
.type z8_photo_lut_construct_entry,%function
z8_photo_lut_construct_entry:
    invoke z8_photo_lut_queue_construct, 1
    mov x0, x21
    b z8_photo_lut_construct_resume
.size z8_photo_lut_construct_entry, .-z8_photo_lut_construct_entry

.global z8_photo_lut_will_apply_entry
.type z8_photo_lut_will_apply_entry,%function
z8_photo_lut_will_apply_entry:
    invoke z8_photo_lut_will_apply, 1
    /* Entered through BL: original setter returns to the native callsite. */
    b z8_photo_lut_native_y_setter
.size z8_photo_lut_will_apply_entry, .-z8_photo_lut_will_apply_entry

.global z8_photo_lut_applied_entry
.type z8_photo_lut_applied_entry,%function
z8_photo_lut_applied_entry:
    invoke z8_photo_lut_applied, 1
    cbnz w0, 1f
    b z8_photo_lut_applied_resume
1:  b z8_photo_lut_applied_failure
.size z8_photo_lut_applied_entry, .-z8_photo_lut_applied_entry

.global z8_photo_lut_pool_init_entry
.type z8_photo_lut_pool_init_entry,%function
z8_photo_lut_pool_init_entry:
    invoke z8_photo_lut_mode_enter
    .inst 0xa9bd5bf7
    b z8_photo_lut_pool_init_resume
.size z8_photo_lut_pool_init_entry, .-z8_photo_lut_pool_init_entry

.global z8_photo_lut_pool_init_exit_entry
.type z8_photo_lut_pool_init_exit_entry,%function
z8_photo_lut_pool_init_exit_entry:
    invoke z8_photo_lut_mode_exit
    .inst 0x12000260
    b z8_photo_lut_pool_init_exit_resume
.size z8_photo_lut_pool_init_exit_entry, .-z8_photo_lut_pool_init_exit_entry

.global z8_photo_lut_shutdown_entry
.type z8_photo_lut_shutdown_entry,%function
z8_photo_lut_shutdown_entry:
    invoke z8_photo_lut_shutdown, 1
    .inst 0xf81e0ff4
    b z8_photo_lut_shutdown_resume
.size z8_photo_lut_shutdown_entry, .-z8_photo_lut_shutdown_entry

.global z8_photo_lut_power_cancel_entry
.type z8_photo_lut_power_cancel_entry,%function
z8_photo_lut_power_cancel_entry:
    invoke z8_photo_lut_power_cancel
    ret
.size z8_photo_lut_power_cancel_entry, .-z8_photo_lut_power_cancel_entry

.global z8_photo_lut_bank_reset_result_entry
.type z8_photo_lut_bank_reset_result_entry,%function
z8_photo_lut_bank_reset_result_entry:
    invoke z8_photo_lut_bank_reset_result, 1
    .inst 0x5283ac95 /* native mov w21,#0x1d64 */
    b z8_photo_lut_bank_reset_result_resume
.size z8_photo_lut_bank_reset_result_entry, .-z8_photo_lut_bank_reset_result_entry
