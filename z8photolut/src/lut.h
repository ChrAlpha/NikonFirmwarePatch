/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef Z8_PHOTO_LUT_H
#define Z8_PHOTO_LUT_H
#include <stdint.h>
#include <stddef.h>

#define Z8_LUT_MAX_LOOKS 8u
#define Z8_LUT_BUNDLE_LIMIT 0x200000u
#define Z8_LUT_MAGIC UINT64_C(0x32303054554c385a)
#define Z8_PREVIEW_GRID 65u
#define Z8_PREVIEW_TABLE_NODES (256u*Z8_PREVIEW_GRID*Z8_PREVIEW_GRID)
#define Z8_PREVIEW_TABLE_BYTES (Z8_PREVIEW_TABLE_NODES*8u)

typedef struct {
    uint32_t offset, bytes, grid, identity, crc32, reserved;
    char title[64];
    uint8_t padding[8];
} Z8LookEntry;

typedef struct {
    uint64_t magic;
    uint32_t version, bytes, count, initial, crc32, reserved;
    uint8_t padding[32];
    Z8LookEntry looks[Z8_LUT_MAX_LOOKS];
} Z8LookBundle;

typedef struct {
    const uint16_t *samples;
    uint32_t grid, identity, strength;
} Z8Look;

typedef struct {
    uint8_t *y, *uv;
    uint64_t y_bytes, uv_bytes;
    uint32_t width, height, y_stride, uv_stride, uv_divisor;
} Z8Surface;

enum {
    Z8_LUT_OK, Z8_LUT_BAD_BUNDLE, Z8_LUT_BAD_SELECTION,
    Z8_LUT_BAD_GEOMETRY, Z8_LUT_BAD_SPAN, Z8_LUT_BAD_LOOK, Z8_LUT_CANCELLED,
    Z8_LUT_NO_WORKER
};

uint32_t z8_lut_crc32(const void *, uint32_t);
unsigned z8_lut_validate(const Z8LookBundle *, uint32_t available);
unsigned z8_lut_select(const Z8LookBundle *, uint32_t choice, uint32_t strength, Z8Look *);
void z8_lut_rgb(const Z8Look *, const uint8_t rgb[3], uint8_t output[3]);
unsigned z8_lut_surface(const Z8Surface *, const Z8Look *);
unsigned z8_lut_preview_build(const Z8Look *,uint64_t *table,uint32_t bytes);
unsigned z8_lut_preview_surface(const Z8Surface *,const uint64_t *table,uint32_t bytes);
unsigned z8_lut_surface_cancel(const Z8Surface *,const Z8Look *,const volatile uint32_t *cancel);
unsigned z8_lut_preview_build_cancel(const Z8Look *,uint64_t *,uint32_t,const volatile uint32_t *cancel);
unsigned z8_lut_preview_surface_cancel(const Z8Surface *,const uint64_t *,uint32_t,const volatile uint32_t *cancel);

_Static_assert(sizeof(Z8LookEntry)==96, "look entry ABI");
_Static_assert(sizeof(Z8LookBundle)==832, "bundle ABI");
#endif
