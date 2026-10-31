ENTRY(z8_photo_lut_jpeg_entry)
z8_photo_lut_jpeg_resume = 0x40e82094;
z8_photo_lut_boot_resume = 0x41184df4;
z8_photo_lut_frame_resume = 0x40e991b4;
z8_photo_lut_frame_success = 0x40e991ec;
z8_photo_lut_queue_resume = 0x40cd7d58;
z8_photo_lut_queue_skip = 0x40cd7f24;
z8_photo_lut_mode_resume = 0x410463e8;
z8_photo_lut_mode_exit_resume = 0x41046708;
z8_photo_lut_reset_resume = 0x40cd92ac;
z8_photo_lut_construct_resume = 0x40cd760c;
z8_photo_lut_native_y_setter = 0x402ab038;
z8_photo_lut_applied_resume = 0x402d14b4;
z8_photo_lut_applied_failure = 0x402d14d0;
z8_photo_lut_pool_init_resume = 0x41045ba4;
z8_photo_lut_pool_init_exit_resume = 0x41045df8;
z8_photo_lut_shutdown_resume = 0x40d67264;
z8_photo_lut_bank_resume = 0x41016354;
z8_photo_lut_bank_default_resume = 0x410169cc;
z8_photo_lut_current_default_resume = 0x41016878;
z8_photo_lut_bank_reset_result_resume = 0x40d68208;
SECTIONS {
    . = 0x40990000;
    .text : { KEEP(*(.text.hooks)) *(.text .text.*) }
    .rodata : { *(.rodata .rodata.*) }
    .data : { *(.data .data.*) }
    .bss (NOLOAD) : { *(.bss .bss.* COMMON) }
    ASSERT(SIZEOF(.bss)==0, "All mutable state must use the explicit private runtime")
    ASSERT(. <= 0x409a0000, "Photo LUT code exceeds its region")
    /DISCARD/ : { *(.comment .note* .eh_frame* .ARM.exidx*) }
}
