/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef Z8_PHOTO_LUT_H
#define Z8_PHOTO_LUT_H
#include <stdint.h>
#include <stddef.h>

#define Z8_LUT_MAX_LOOKS 8u
#define Z8_LUT_BUNDLE_LIMIT 0x200000u
#define Z8_LUT_MAGIC UINT64_C(0x34303054554c385a) /* "Z8LUT004" */
#define Z8_LUT_VERSION 4u
#define Z8_LUT_NAME_BYTES 63u
#define Z8_PREVIEW_GRID 65u
#define Z8_PREVIEW_TABLE_NODES (256u*Z8_PREVIEW_GRID*Z8_PREVIEW_GRID)
#define Z8_PREVIEW_TABLE_BYTES (Z8_PREVIEW_TABLE_NODES*8u)

/* A look's identity is the SHA-256 of its applied transform (z8photolut/
 * library.py look_id, isp_look_id): for a cpu-3d look the semantics string,
 * grid and samples; for an isp+residual look the ISP semantics, basis key,
 * grid, ISP tables and residual samples. Validation recomputes it; it never
 * trusts the stored digest. */
#define Z8_LUT_SEMANTICS "input=nikon-rendered-sdr 8-bit full-range BT.601 YCbCr;" \
    "model=RGB tetrahedral uint16 grid;output=8-bit clipped;strength=1"
#define Z8_LUT_ISP_SEMANTICS "input=camera scene-linear through the native Log still develop;" \
    "isp=M1 base Q10,RGB 3x229 u16/32,NLG,M2 s16/512,Cpxs 32x6 Q8 slot order;" \
    "residual=RGB tetrahedral uint16 grid on 8-bit full-range BT.601 YCbCr;output=8-bit clipped;strength=1"
/* Rendering: cpu-3d applies the whole grid to the rendered JPEG input;
 * isp+residual develops the still through the native Log chain with the
 * look's ISP tables, then applies the residual grid. */
enum { Z8_RENDER_CPU, Z8_RENDER_ISP };
/* Input bases (z8photolut/basis.py codes and keys); 0 is nikon-sdr. */
#define Z8_BASES 8u
const char *z8_lut_basis_key(uint32_t basis);
/* Still-develop tables in their native formats (library.py ISP block). */
#define Z8_CURVE_NODES 229u
#define Z8_CURVE_LIMIT 32736u
typedef struct {
    uint16_t rgb[3][Z8_CURVE_NODES];    /* 1/32 10-bit code, non-decreasing */
    uint16_t pad0;
    uint16_t nlg[Z8_CURVE_NODES];       /* (green + 4) >> 3 */
    uint16_t pad1[3];
    int16_t m2[9];                      /* 1/512, curve codes -> internal Y'UV */
    uint16_t pad2[7];
    int16_t m1[9];                      /* base Q10, composed natively with B and gain */
    uint16_t pad3[7];
    int16_t cpxs[32][6];                /* Pa..Pf Q8, firmware ring-slot order */
    uint16_t pad4[8];
} Z8IspTables;
typedef struct {
    uint32_t offset, bytes, grid, identity, crc32;   /* span: [ISP tables] grid samples */
    uint16_t rendering, basis;
    char name[64];                      /* UTF-8, NUL-padded, 1..63 bytes */
    uint8_t id[32];
    uint32_t isp_bytes, reserved;       /* sizeof(Z8IspTables) for isp+residual, else 0 */
} Z8LookEntry;

/* reserved: 0 in every bundle (validated). */
typedef struct {
    uint64_t magic;
    uint32_t version, bytes, count, initial, crc32, reserved;
    uint8_t library_id[32];
    Z8LookEntry looks[Z8_LUT_MAX_LOOKS];
} Z8LookBundle;

/* The grid the CPU applies: the whole look (cpu-3d) or the residual (isp+residual). */
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
    Z8_LUT_NO_WORKER, Z8_LUT_NOT_DEVELOPED
};

uint32_t z8_lut_crc32(const void *, uint32_t);
void z8_lut_sha256(const void *data,uint32_t bytes,uint8_t digest[32]);
void z8_lut_look_id(uint32_t grid,const uint16_t *samples,uint8_t digest[32]);
void z8_lut_isp_look_id(uint32_t basis,uint32_t grid,const Z8IspTables *isp,const uint16_t *samples,uint8_t digest[32]);
void z8_lut_fit_id(const Z8IspTables *isp,uint8_t digest[32]);
unsigned z8_lut_isp_valid(const Z8IspTables *isp);
const Z8IspTables *z8_lut_isp(const Z8LookBundle *,uint32_t choice);
unsigned z8_lut_name_valid(const char *name,unsigned capacity);
unsigned z8_lut_validate(const Z8LookBundle *, uint32_t available);
unsigned z8_lut_select(const Z8LookBundle *, uint32_t choice, uint32_t strength, Z8Look *);
void z8_lut_rgb(const Z8Look *, const uint8_t rgb[3], uint8_t output[3]);
unsigned z8_lut_surface(const Z8Surface *, const Z8Look *);
unsigned z8_lut_preview_build(const Z8Look *,uint64_t *table,uint32_t bytes);
unsigned z8_lut_preview_surface(const Z8Surface *,const uint64_t *table,uint32_t bytes);
unsigned z8_lut_surface_cancel(const Z8Surface *,const Z8Look *,const volatile uint32_t *cancel);
unsigned z8_lut_preview_build_cancel(const Z8Look *,uint64_t *,uint32_t,const volatile uint32_t *cancel);
unsigned z8_lut_preview_surface_cancel(const Z8Surface *,const uint64_t *,uint32_t,const volatile uint32_t *cancel);

_Static_assert(sizeof(Z8LookEntry)==128, "look entry ABI");
_Static_assert(sizeof(Z8IspTables)==0x900 && offsetof(Z8IspTables,nlg)==0x560 && offsetof(Z8IspTables,m2)==0x730 &&
               offsetof(Z8IspTables,m1)==0x750 && offsetof(Z8IspTables,cpxs)==0x770, "ISP block ABI");
_Static_assert(sizeof(Z8LookBundle)==1088, "bundle ABI");
#endif
