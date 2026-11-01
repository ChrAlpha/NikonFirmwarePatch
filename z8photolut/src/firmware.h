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
/* Native photo folder name is a six-byte blob at group6+0 of each of the
 * five 0x2e0-byte photo-bank blobs (current, A..D). Its two alignment bytes
 * are descriptor-unowned and persisted with the blob. Firmware up to abe1a72
 * kept a session selection there; now they only say whether the bank record
 * below is authoritative (Z8_OWNED_RECORDED) or never written (0). */
#define Z8_OWNED_BYTES UINT64_C(0xa03d482ba)
#define Z8_BANK_STRIDE 0x2e0u
#define Z8_BANK_COPIES 5u
#define Z8_OWNED_RECORDED 0xffffu
#define Z8_BANKS 4u
#define Z8_BANK_INDEX UINT64_C(0xa03d48164)      /* native selected photo bank 0..3 */
#define Z8_SETTINGS_LOADED UINT64_C(0xa03bddcc8) /* byte: native section-15 restore succeeded */
/* Fixed settings blob 16 (group 0x31): its mirrored region also stores the
 * bank record right after the stock bytes in the same mirrored region commit. */
#define Z8_BLOB_SECTION 0x10u
#define Z8_BLOB_GROUP 0x31u
#define Z8_BLOB UINT64_C(0xa03dabdc0)
#define Z8_BLOB_BYTES 0xcdb0u
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

/* The card library published in RAM (restored automatically at boot or
 * loaded from the menu). The built-in image library is never modified. */
typedef struct {
    uint64_t active;                    /* card bundle; 0 = built-in; published by the card worker */
    uint32_t readers,publishing;        /* counted guard of the global library */
    uint32_t request,result;            /* explicit Load request / worker result serials */
    uint32_t status,detail,loaded;      /* last explicit Load result */
    uint32_t retry_now;                 /* menu visit: retry a missing look at once */
    uint32_t unload;                    /* whole reset: back to the built-in library */
    uint64_t worker;                    /* task block and wake event */
    uint32_t worker_id,worker_state,event_ready,show_result;
    uint32_t attempts,publications,refusals;
} Z8CardLibrary;

enum { Z8_XMP_NOT_READY,Z8_XMP_NOT_JPEG,Z8_XMP_HEADER,Z8_XMP_BRANCH,Z8_XMP_LEASE,Z8_XMP_OFF,
       Z8_XMP_PACKET,Z8_XMP_NOT_APPLIED,Z8_XMP_GATES };

/* Desired look of one photo bank: Off, a full transform identity, or
 * unresolved (identity not known: never fabricated, never shown as Off). */
enum { Z8_DESIRED_OFF,Z8_DESIRED_LOOK,Z8_DESIRED_UNRESOLVED };
#define Z8_PENDING_LIBRARY 0x10u
typedef struct {
    uint8_t kind,reserved[3];
    uint8_t id[32];
} Z8BankSlot;
#define Z8_RECORD_MAGIC 0x4b42385au     /* "Z8BK" */
/* Which library the configuration uses: the built-in one, or the card's
 * Z8LUTLIB.BIN (restored at every start, also while every bank is Off). */
enum { Z8_LIBRARY_BUILTIN,Z8_LIBRARY_CARD };
typedef struct {
    uint32_t magic;
    uint16_t version,bytes;
    uint32_t library,reserved;
    Z8BankSlot banks[Z8_BANKS];
    uint8_t sha256[32];                 /* over the bytes above */
} Z8BankRecord;

/* Where the record came from this boot (observability and tests). UNREAD:
 * the stored region could not be read, so nothing is known and nothing is
 * stored until an actual read succeeds. */
enum { Z8_SOURCE_NONE,Z8_SOURCE_STORED,Z8_SOURCE_FRESH,Z8_SOURCE_MIGRATED,Z8_SOURCE_LOST,
       Z8_SOURCE_DEFAULTS,Z8_SOURCE_RETAINED,Z8_SOURCE_UNREAD };
/* Outcome of reading the stored blob-16 region (mode-4 settings callback, or
 * a later read before a store). LOST: the region itself is unreadable (the
 * native caller then uses blob defaults); UNREAD: our copy of the tail could
 * not be made (no memory), the region was not judged. */
enum { Z8_BLOB_PENDING,Z8_BLOB_VALID,Z8_BLOB_ABSENT,Z8_BLOB_CORRUPT,Z8_BLOB_LOST,Z8_BLOB_UNREAD,
       Z8_BLOB_SKIPPED };
/* Card library restore, as shown for a bank whose look is not published. */
enum { Z8_RESTORE_WAITING,Z8_RESTORE_READING,Z8_RESTORE_DONE };
/* What the camera does for the selected bank (last evaluated). */
enum { Z8_EFFECTIVE_OFF,Z8_EFFECTIVE_APPLIED,Z8_EFFECTIVE_RESTORING,Z8_EFFECTIVE_UNAVAILABLE,
       Z8_EFFECTIVE_UNRESOLVED };

typedef struct {
    Z8BankRecord record;                /* desired looks once ready */
    Z8BankSlot pending[Z8_BANKS];       /* changes made before a stored record could be read */
    uint32_t sequence,writer;           /* sequence odd while written; one writer at a time */
    uint32_t ready,authoritative;       /* ready: usable; authoritative: from an actual read, may be stored */
    uint32_t source,blob,pending_mask;  /* pending_mask: bit per bank, Z8_PENDING_LIBRARY */
    uint32_t pending_library;
    uint32_t settings_restored,defaults,retained;
    uint32_t library_generation,restore,reason,detail,retries;
    uint32_t commits,late_reads,blob_writes,blob_reads,refused_writes,alloc_retries;
    uint32_t effective;                 /* Z8_EFFECTIVE_* of the selected bank, last evaluated */
    uint32_t refused_forecasts,refused_allocations;   /* captures withheld: look not applied */
    uint32_t pending_sources;           /* nibble per destination: original unread bank + 1, or zero */
    uint32_t cancelled_request;         /* whole reset cancels explicit imports through this serial */
    uint32_t text_key[2];               /* menu text last written: packed status, library generation */
} Z8Persist;

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
    Z8Persist persist;
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
int z8_photo_lut_bank_copy(uint32_t,uint32_t,const uint32_t *,uint32_t);
uint32_t z8_photo_lut_settings_wait(void *,uint32_t,uint32_t *);
uint32_t z8_photo_lut_settings_restored(uint64_t,uint64_t,uint64_t,uint64_t,uint64_t);
void z8_photo_lut_defaults(void);
uint32_t z8_photo_lut_blob_read(uint32_t section,uint64_t buffer,uint32_t bytes);
uint32_t z8_photo_lut_blob_write(uint32_t section,uint64_t buffer,uint32_t bytes);
uint32_t z8_photo_lut_blob_loaded(uint32_t group);
_Static_assert(offsetof(Z8LutState,preview)==120,"telemetry prefix ABI");
_Static_assert(sizeof(Z8PreviewSlot)==256,"preview slot ABI");
_Static_assert(sizeof(Z8JpegJob)==112,"JPEG job ABI");
_Static_assert(offsetof(Z8JpegWorker,jobs)==72,"JPEG worker ABI");
_Static_assert(sizeof(Z8CardLibrary)==88,"card library ABI");
_Static_assert(sizeof(Z8BankRecord)==0xc0,"bank record ABI");
_Static_assert(sizeof(Z8LutState)<=4096,"private runtime region");
#endif
