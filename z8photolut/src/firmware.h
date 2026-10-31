/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef Z8_PHOTO_LUT_FIRMWARE_H
#define Z8_PHOTO_LUT_FIRMWARE_H
#include "lut.h"
#define Z8_LOOKS_VA UINT64_C(0x409a0000)
#define Z8_STATE_VA UINT64_C(0xa101f0000)
#define Z8_STATE_MAGIC UINT64_C(0x32545354554c385a)
#define Z8_OPTION_FIRST 0x35b5u
#define Z8_MENU_ITEM 0x2caau
#define Z8_STRING_FIRST 3137u
/* Native photo folder name is a six-byte blob at group6+0. Its two
 * alignment bytes are descriptor-unowned and persisted with all five banks. */
#define Z8_SAVED_CHOICE ((uint8_t *)(uintptr_t)UINT64_C(0xa03d482ba))
#define Z8_PREVIEW_SLOTS 8u

typedef struct {
    uint32_t state,choice;
    uint64_t allocation,y,uv;
    uint32_t capacity,width,height,y_stride,uv_stride,uv_divisor;
    uint32_t serial,original_cookie;
    uint64_t original_y,source_y;
    uint32_t channel,epoch;
    uint64_t apply_sequence;
    uint32_t flags,reserved;
    uint8_t request[0x98];
} Z8PreviewSlot;

typedef struct {
    uint64_t completed,produced,copy_races,rejected,pool_busy,allocation_failed;
    uint64_t enqueued,skipped,submitted,retired,stale_callbacks,pixels;
    uint64_t started,finished;
    uint32_t producer_lock,serial,allocated_bytes,error;
    uint64_t worker;
    uint32_t worker_id,worker_started,worker_error,power_paused;
    uint64_t cache;
    uint32_t cache_choice,cache_builds;
    uint64_t cache_resource,cache_allocations,cache_releases,cancelled;
    uint32_t paused,cache_busy,mode_epoch,mode_entries;
    uint64_t applies;
    uint32_t display_epoch,last_reset_channel;
    uint64_t queue_resets,orphaned,orphan_reclaimed;
    Z8PreviewSlot slots[Z8_PREVIEW_SLOTS];
} Z8Preview;

typedef struct {
    uint64_t magic;
    uint32_t version, bytes, choice, strength, ready, error;
    uint64_t boots, jpeg_calls, jpeg_applied, jpeg_pixels, jpeg_rejected;
    uint64_t jpeg_started, jpeg_finished;
    uint32_t jpeg_choice, jpeg_error, width, height;
    uint64_t y, uv;
    Z8Preview preview;
    /* Diagnostic count; native image-job ownership controls JPEG lifetime. */
    uint32_t jpeg_busy,cache_cleanup_lock;
} Z8LutState;

#define Z8_STATE ((volatile Z8LutState *)(uintptr_t)Z8_STATE_VA)
#define Z8_BUNDLE ((const Z8LookBundle *)(uintptr_t)Z8_LOOKS_VA)
void z8_photo_lut_boot(void);
void z8_photo_lut_jpeg(const uint64_t *saved);
void z8_photo_lut_frame(const uint64_t *saved);
unsigned z8_photo_lut_queue(uint64_t *saved);
void z8_photo_lut_mode_enter(void);
void z8_photo_lut_mode_exit(void);
void z8_photo_lut_queue_reset(const uint64_t *);
void z8_photo_lut_queue_construct(const uint64_t *);
void z8_photo_lut_will_apply(const uint64_t *);
void z8_photo_lut_applied(const uint64_t *);
void z8_photo_lut_shutdown(const uint64_t *);
void z8_photo_lut_power_cancel(void);
void z8_photo_lut_bank_reset_result(const uint64_t *);
int z8_photo_lut_menu_entry(void *page);
void z8_photo_lut_menu_ok(void *context);
int z8_photo_lut_photo_refresh(void *page);
_Static_assert(offsetof(Z8LutState,preview)==120,"telemetry prefix ABI");
_Static_assert(sizeof(Z8PreviewSlot)==256,"preview slot ABI");
_Static_assert(sizeof(Z8LutState)<=4096,"private runtime region");
#endif
