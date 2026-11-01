/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef Z8_PHOTO_LUT_FIRMWARE_H
#define Z8_PHOTO_LUT_FIRMWARE_H
#include "lut.h"
#include "metadata.h"
#define Z8_LOOKS_VA UINT64_C(0x409a0000)
#define Z8_STATE_VA UINT64_C(0xa101f0000)
#define Z8_STATE_MAGIC UINT64_C(0x32545354554c385a)
#define Z8_OPTION_FIRST 0x35b5u
#define Z8_MENU_ITEM 0x2caau
#define Z8_MENU_STATE 0x1du       /* borrowed hidden photo state 29 holding the option page */
#define Z8_LABELS 12u             /* strings Z8_STRING_FIRST+0..11: title, help, Off, load row, 8 looks */
#define Z8_STRING_FIRST 3137u
/* Native photo folder name is a six-byte blob at group6+0. Its two
 * alignment bytes are descriptor-unowned and persisted with all five banks. */
#define Z8_SAVED_CHOICE ((uint8_t *)(uintptr_t)UINT64_C(0xa03d482ba))
#define Z8_PREVIEW_SLOTS 8u
/* Menu text lives in the loaded image; its RAM copy is writable at this
 * fixed alias (the same bias as the private runtime 0x40bf0000->0xa101f0000). */
#define Z8_TEXT_ALIAS UINT64_C(0x9cf600000)
/* Stock language tables hold start offsets only; a string ends where the
 * next begins. Three packed UTF-16 blocks (English for every other language,
 * Simplified and Traditional Chinese) each hold strings Z8_STRING_FIRST+0..11
 * contiguously: title, help, Off, card-load row, eight looks (+12 = end). */
#define Z8_TEXT_BLOCKS_VA UINT64_C(0x408d0000)
#define Z8_TEXT_BLOCK_BYTES 0x800u
#define Z8_LANGUAGES 36u
#define Z8_LANGUAGE_DIRECTORY UINT64_C(0x4327c5a0)
#define Z8_LANGUAGE_BLOBS UINT64_C(0x439daf58)
/* Option rows: 0 Off, 1 Load LUTs from card, 2.. looks 1..8. Unused look
 * rows are hidden, and only ever at the end, so a visible position is always
 * the row index whether or not the native list compacts hidden rows. */
#define Z8_LOAD_ROW 1u
#define Z8_ROWS (2u+Z8_LUT_MAX_LOOKS)
static inline uint32_t z8_row_of(uint32_t selection) { return selection?selection+1:0; }

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

#define Z8_JPEG_JOBS 8u

/* One JPEG copy/LUT request handed from its native encoder task to the
 * photo LUT JPEG worker. The requester retains job ownership throughout. */
typedef struct {
    uint32_t state,error;
    uint64_t sequence;
    uint64_t source_y,source_uv;
    Z8Surface surface;
    Z8Look look;
} Z8JpegJob;

typedef struct {
    uint64_t worker;                    /* retained task block and wake event */
    uint32_t worker_id,worker_state;
    uint32_t event_ready,alive;
    uint32_t submit_lock,queue_peak;
    uint64_t sequence,submitted,completed,start_failures,waits;
    Z8JpegJob jobs[Z8_JPEG_JOBS];
} Z8JpegWorker;

/* A card library loaded for this power-on session. The built-in image
 * library is never modified; the heap is rebuilt at every boot. */
typedef struct {
    uint64_t active;                    /* session bundle; 0 = built-in */
    uint64_t staged;                    /* validated, owned by whoever exchanges it out */
    uint32_t readers,publishing;        /* counted guard of the global library */
    uint32_t request,result,wanted;     /* GUI request / worker result serials */
    uint32_t status,detail,loaded;      /* last result shown in the menu */
    uint64_t worker;                    /* task block and wake event */
    uint32_t worker_id,worker_state,event_ready,show_result;
    uint64_t attempts,publications,refusals;
} Z8CardLibrary;

enum { Z8_XMP_NOT_READY,Z8_XMP_NOT_JPEG,Z8_XMP_HEADER,Z8_XMP_BRANCH,Z8_XMP_LEASE,Z8_XMP_OFF,
       Z8_XMP_PACKET,Z8_XMP_NOT_APPLIED,Z8_XMP_GATES };

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
    uint64_t jpeg_previews_applied,jpeg_preview_pixels,jpeg_preview_rejected;
    uint32_t jpeg_preview_error,reserved;
    Z8JpegWorker jpeg;
    Z8CardLibrary card;
    uint64_t xmp_appended,xmp_refused;   /* provenance written / refused by the helper */
    uint32_t xmp_error,xmp_calls;
    /* Observability only (never consulted): why a JPEG packet was left
     * native, per Z8_XMP_* gate, and the last addresses the hook received. */
    uint32_t xmp_skipped[Z8_XMP_GATES];
    uint64_t xmp_last_header,xmp_last_packet;
    uint32_t xmp_last_kind,xmp_last_file,xmp_last_class,xmp_last_lease;  /* lease: jpeg_lease result, ~0 unread */
    uint64_t xmp_last_graph;
    uint32_t xmp_last_choice,xmp_last_applied;
} Z8LutState;

#define Z8_STATE ((volatile Z8LutState *)(uintptr_t)Z8_STATE_VA)
#define Z8_BUILTIN ((const Z8LookBundle *)(uintptr_t)Z8_LOOKS_VA)
void z8_photo_lut_boot(void);
uint64_t z8_photo_lut_output_alloc(uint32_t frame,uint32_t pool,uint32_t bytes,uint64_t configuration);
uint32_t z8_photo_lut_output_budget(uint64_t settings,uint64_t format,uint32_t special);
unsigned z8_photo_lut_jpeg(uint32_t channel,uint64_t parameters);
unsigned z8_photo_lut_medium(uint32_t,uint64_t,uint64_t,uint64_t);
unsigned z8_photo_lut_small(uint32_t,uint64_t,uint64_t,uint64_t);
unsigned z8_photo_lut_thumbnail(uint32_t,uint64_t);
unsigned z8_photo_lut_file_graph(uint32_t,uint64_t,uint64_t,uint64_t,uint64_t);
uint32_t z8_photo_lut_xmp(uint32_t kind,uint32_t file,uint64_t settings,uint32_t size,uint64_t packet,uint64_t header);
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
int z8_photo_lut_menu_ok(void *request);
uint32_t z8_photo_lut_text_font(uint32_t language,uint32_t id,const volatile uint16_t *text,uint32_t bytes);
int z8_photo_lut_photo_refresh(void *page);
_Static_assert(offsetof(Z8LutState,preview)==120,"telemetry prefix ABI");
_Static_assert(sizeof(Z8PreviewSlot)==256,"preview slot ABI");
_Static_assert(sizeof(Z8JpegJob)==112,"JPEG job ABI");
_Static_assert(offsetof(Z8JpegWorker,jobs)==72,"JPEG worker ABI");
_Static_assert(sizeof(Z8CardLibrary)==96,"card library ABI");
_Static_assert(sizeof(Z8LutState)<=4096,"private runtime region");
#endif
