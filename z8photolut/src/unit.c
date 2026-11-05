/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The photo LUT unit: one translation unit for the in-house linker. The hooks
 * (hooks.S) are placed in .text.entry, which the linker puts at the cave start. */
#include "lut.c"
#include "metadata.c"
#include "firmware.c"
__asm__(".include \"hooks.S\"");
