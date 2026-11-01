/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "firmware.h"
#include "fonts.h"

__attribute__((used)) const uint32_t z8_photo_lut_runtime_layout[]={
    sizeof(Z8LutState),offsetof(Z8LutState,preview),offsetof(Z8Preview,slots),
    sizeof(Z8PreviewSlot),offsetof(Z8PreviewSlot,request),offsetof(Z8PreviewSlot,channel),
    offsetof(Z8Preview,cache_resource),offsetof(Z8Preview,paused),
    offsetof(Z8PreviewSlot,apply_sequence),offsetof(Z8PreviewSlot,flags),
    offsetof(Z8Preview,applies),offsetof(Z8Preview,display_epoch),Z8_PREVIEW_SLOTS,
    offsetof(Z8Preview,power_paused),offsetof(Z8LutState,jpeg_busy),offsetof(Z8LutState,jpeg_previews_applied),
    offsetof(Z8LutState,jpeg),offsetof(Z8LutState,card),offsetof(Z8LutState,xmp_appended),
    offsetof(Z8LutState,xmp_skipped),offsetof(Z8LutState,persist),offsetof(Z8Persist,sequence),
    offsetof(Z8Persist,ready),offsetof(Z8Persist,restore),offsetof(Z8Persist,source),offsetof(Z8Persist,blob),
    offsetof(Z8Persist,pending_mask),offsetof(Z8Persist,authoritative),
    offsetof(Z8LutState,boots),offsetof(Z8Persist,reason),offsetof(Z8Persist,detail),offsetof(Z8Persist,retries),
    offsetof(Z8Persist,library_generation),offsetof(Z8Persist,effective),offsetof(Z8Persist,refused_forecasts),
    offsetof(Z8Persist,refused_allocations),offsetof(Z8Persist,commits),offsetof(Z8Persist,blob_writes),offsetof(Z8Persist,refused_writes),
    offsetof(Z8Persist,blob_reads),offsetof(Z8Persist,late_reads),offsetof(Z8LutState,xmp_last_choice),
    offsetof(Z8LutState,xmp_last_applied)
};

/* Clang may lower fixed aggregate copies to memcpy in freestanding code. */
void *memcpy(void *destination,const void *source,size_t bytes)
{
    volatile uint8_t *d=destination;const uint8_t *s=source;
    for(size_t i=0;i<bytes;i++) d[i]=s[i];
    return destination;
}
void *memset(void *destination,int value,size_t bytes)
{
    volatile uint8_t *d=destination;
    for(size_t i=0;i<bytes;i++) d[i]=(uint8_t)value;
    return destination;
}

static uint16_t read16(uint64_t p) { return *(volatile const uint16_t *)(uintptr_t)p; }
static uint32_t read32(uint64_t p) { return *(volatile const uint32_t *)(uintptr_t)p; }
static uint64_t read64(uint64_t p) { return *(volatile const uint64_t *)(uintptr_t)p; }
static int range(uint64_t p,uint64_t n,uint64_t first,uint64_t end)
{ return n && p>=first && p<end && n<=end-p; }
static uint64_t ticks(void) { uint64_t t;__asm__ volatile("mrs %0, cntpct_el0":"=r"(t));return t; }
static uint8_t *cpu_address(uint64_t raw)
{
    /* Native region flags distinguish uncached image RAM from the cached
     * General/Heap arenas. Keep raw addresses in all hardware descriptors. */
    uint64_t cached=((uint64_t (*)(uint64_t))(uintptr_t)0x41186718)(raw);
    if(cached!=raw && cached!=raw+UINT64_C(0x400000000)) return 0;
    return (uint8_t *)(uintptr_t)cached;
}
/* The active library is the built-in image bundle or the published card
 * bundle. Publication (card worker only) first quiesces preview work, then
 * raises `publishing` and waits until every counted reader has left. A reader
 * that observes publication in progress treats the look as unavailable. */
static const Z8LookBundle *library_active(void)
{
    uint64_t active=__atomic_load_n(&Z8_STATE->card.active,__ATOMIC_ACQUIRE);
    return active?(const Z8LookBundle *)(uintptr_t)active:Z8_BUILTIN;
}
static const Z8LookBundle *library_enter(void)
{
    volatile Z8CardLibrary *c=&Z8_STATE->card;
    __atomic_fetch_add(&c->readers,1,__ATOMIC_SEQ_CST);
    if(!__atomic_load_n(&c->publishing,__ATOMIC_SEQ_CST)) return library_active();
    __atomic_fetch_sub(&c->readers,1,__ATOMIC_RELEASE);
    return 0;
}
static void library_exit(void) { __atomic_fetch_sub(&Z8_STATE->card.readers,1,__ATOMIC_RELEASE); }

/* Native C3.11 forecast task: 40f964e8 posts mask 1 to the object at
 * a03bc6970 through 40c002a0. Its registered 40f95258 callback dispatches
 * bit 0 to 40f98758, whose 40f98958 call recomputes our prospective budget.
 * Notify after stable desired/library transitions, never from a frame path.
 * The object and event must have completed native initialization. */
void z8_photo_lut_capture_refresh(void)
{
    volatile Z8LutState *s=Z8_STATE;
    if(s->magic!=Z8_STATE_MAGIC || !s->persist.settings_restored || s->preview.power_paused) return;
    const uint64_t object=UINT64_C(0xa03bc6970);
    if(!*(volatile const uint8_t *)(uintptr_t)UINT64_C(0xa03bc6bd0) ||
       read64(object+0x48)!=UINT64_C(0x40f95258) || (int32_t)read32(object+0x38)<=0) return;
    ((void (*)(void))(uintptr_t)0x40f964e8)();
}

/* Native C3.11 cache helpers operate on 64-byte lines. All admitted image
 * spans own complete cache lines. Incoming DMA data is invalidated, then
 * the CPU-produced pixels are cleaned before entering the hardware encoder.
 * Physical DMA/cache behaviour remains a camera acceptance requirement. */
static void invalidate(const void *address,uint64_t bytes)
{
    uintptr_t p=(uintptr_t)address;
    __asm__ volatile("dsb sy":::"memory");
    for(uint64_t i=0;i<bytes;i+=64) __asm__ volatile("dc ivac, %0"::"r"(p+i):"memory");
    __asm__ volatile("dsb sy":::"memory");
}
static void clean(const void *address,uint64_t bytes)
{
    uintptr_t p=(uintptr_t)address;
    for(uint64_t i=0;i<bytes;i+=64) __asm__ volatile("dc cvac, %0"::"r"(p+i):"memory");
    __asm__ volatile("dsb sy":::"memory");
}

#define ALLOC ((void *(*)(uint32_t,uint32_t))(uintptr_t)0x40d2c408)
#define FREE ((void (*)(void *))(uintptr_t)0x40d2c448)
#define QUEUE_UNLOCK ((void (*)(void *))(uintptr_t)0x40d2c660)
#define TASK_CREATE ((void (*)(void *,void (*)(void *),uint32_t *))(uintptr_t)0x40d2d498)
#define TASK_START ((void (*)(void *,void *))(uintptr_t)0x40d2d5cc)
#define TASK_SLEEP ((void (*)(uint32_t))(uintptr_t)0x40d2d654)
#define EVENT_CREATE ((void (*)(void *,uint32_t,uint32_t,const char *))(uintptr_t)0x40d2bc28)
#define EVENT_SET ((void (*)(void *,uint32_t))(uintptr_t)0x40d2bc8c)
#define EVENT_WAIT ((int (*)(void *,uint32_t,uint32_t *,uint32_t))(uintptr_t)0x40d2bdac)
#define POOL_AVAILABLE ((uint32_t (*)(uint32_t))(uintptr_t)0x41047a00)
#define POOL_ALLOC ((uint64_t (*)(uint32_t,uint32_t,uint32_t))(uintptr_t)0x41047478)
#define POOL_FREE ((uint32_t (*)(uint64_t))(uintptr_t)0x410477c8)
#define POOL_MODE UINT64_C(0xa03f31a68)
enum { SLOT_FREE,SLOT_WRITING,SLOT_PENDING,SLOT_TRANSFORM,SLOT_READY,SLOT_QUEUED,SLOT_ORPHAN };
enum { SLOT_SUBMITTED=1,SLOT_APPLIED=2,SLOT_MAY_APPLY=4 };
/* 40d2d498 parameters: CPU class, priority, stack bytes, one attribute byte
 * (zero for every native task), then the NUL-terminated task name. */
#define TASK_NAME 13
typedef struct { uint8_t task[64];int32_t event[4]; } PreviewWorker;
static int cache_pins(volatile const Z8PreviewSlot *slot);

static void copy_bytes(void *destination,const void *source,uint32_t bytes)
{
    uint8_t *d=destination;const uint8_t *s=source;
    if(!(((uintptr_t)d|(uintptr_t)s)&7)) {
        while(bytes>=8) { *(uint64_t *)d=*(const uint64_t *)s;d+=8;s+=8;bytes-=8; }
    }
    while(bytes--) *d++=*s++;
}

static int main_span(uint64_t p,uint64_t n)
{ return range(p,n,UINT64_C(0xa00000000),UINT64_C(0xa40000000)); }

static int same_bytes(const uint8_t *a,const uint8_t *b,uint32_t n)
{
    for(uint32_t i=0;i<n;i++) if(a[i]!=b[i]) return 0;
    return 1;
}

/* Task context only: wait out a publication in progress. */
static const Z8LookBundle *library_wait(void)
{
    const Z8LookBundle *b;
    while(!(b=library_enter())) TASK_SLEEP(1);
    return b;
}

/* ---- desired and effective looks --------------------------------------- */
/* The bank record holds the desired look of photo banks A..D. Native bank
 * switching saves the current settings into the old bank and loads the new
 * one, so the current settings always belong to the selected bank and its
 * desired look is banks[selected]. Readers take a consistent copy through
 * the sequence; the GUI task and native settings producers write it. */
/* 1: *out is the bank's desired look; 0: not known yet (or a writer kept
 * the record busy). Before the stored record is read, only a change made
 * meanwhile is known. */
static int desired_slot(uint32_t bank,Z8BankSlot *out)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    for(unsigned attempt=0;attempt<64;attempt++) {
        uint32_t before=__atomic_load_n(&p->sequence,__ATOMIC_ACQUIRE);
        if(before&1) continue;
        unsigned ready=__atomic_load_n(&p->ready,__ATOMIC_ACQUIRE);
        unsigned known=ready || (p->pending_mask&(1u<<bank));
        if(known) copy_bytes(out,ready?(const void *)(uintptr_t)&p->record.banks[bank]:
                                       (const void *)(uintptr_t)&p->pending[bank],sizeof(*out));
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        if(__atomic_load_n(&p->sequence,__ATOMIC_RELAXED)==before) return known;
    }
    return 0;
}

/* The selected bank's look against library b. *index is its 1-based
 * position in b when applied, else 0. Restoring and unavailable looks are
 * never replaced by Off or by another look. */
static uint32_t evaluate(const Z8LookBundle *b,uint32_t *index)
{
    volatile Z8LutState *s=Z8_STATE;
    volatile Z8Persist *p=&s->persist;
    *index=0;
    uint32_t bank=read32(Z8_BANK_INDEX);
    Z8BankSlot slot;
    if(!desired_slot(bank<Z8_BANKS?bank:0,&slot)) return Z8_EFFECTIVE_RESTORING;
    if(bank>=Z8_BANKS) return Z8_EFFECTIVE_UNRESOLVED;
    if(slot.kind==Z8_DESIRED_OFF) return Z8_EFFECTIVE_OFF;
    if(slot.kind!=Z8_DESIRED_LOOK) return Z8_EFFECTIVE_UNRESOLVED;
    if(s->ready && b)
        for(uint32_t i=0;i<b->count;i++)
            if(same_bytes(b->looks[i].id,slot.id,32)) { *index=i+1;return Z8_EFFECTIVE_APPLIED; }
    /* Restoring only while the card library this configuration uses has not
     * been read yet; a built-in configuration never reads the card. */
    return p->record.library==Z8_LIBRARY_CARD && __atomic_load_n(&p->restore,__ATOMIC_ACQUIRE)!=Z8_RESTORE_DONE?
           Z8_EFFECTIVE_RESTORING:Z8_EFFECTIVE_UNAVAILABLE;
}

static uint32_t effective(const Z8LookBundle *b,uint32_t *index)
{
    uint32_t state=evaluate(b,index);
    Z8_STATE->persist.effective=state;
    return state;
}

/* The applied look's position in b, or 0 (Off or not applied). */
static uint32_t selection(const Z8LookBundle *b)
{
    uint32_t index;
    effective(b,&index);
    Z8_STATE->choice=index;
    return index;
}
static uint32_t choice(void)
{
    const Z8LookBundle *b=library_enter();
    uint32_t value=b?selection(b):0;
    if(b) library_exit();
    return value;
}
/* A capture is admitted with the selected bank's look applied, or Off. */
static int admitted(const Z8LookBundle *b,uint32_t *index)
{
    uint32_t state=effective(b,index);
    return state==Z8_EFFECTIVE_OFF || state==Z8_EFFECTIVE_APPLIED;
}
static int cpu_span(uint64_t p,uint64_t n)
{
    /* Native pool0x23 job bodies use raw image RAM; pool0x22 capture settings
     * use its cached alias. Both also have General-heap fallback paths. */
    return main_span(p,n) || range(p,n,UINT64_C(0x840000000),UINT64_C(0x900000000)) ||
           range(p,n,UINT64_C(0xc40000000),UINT64_C(0xd00000000));
}

static unsigned preview_source(uint64_t descriptor,Z8Surface *f)
{
    if(!main_span(descriptor,0x28) || (descriptor&7)) return Z8_LUT_BAD_SPAN;
    if(read32(descriptor)!=2) return Z8_LUT_BAD_GEOMETRY;
    *f=(Z8Surface){.y=(uint8_t *)(uintptr_t)read64(descriptor+24),.uv=(uint8_t *)(uintptr_t)read64(descriptor+32),
        .width=read16(descriptor+16),.height=read16(descriptor+18),.y_stride=read16(descriptor+8),
        .uv_stride=read16(descriptor+10),.uv_divisor=2};
    f->y_bytes=(uint64_t)f->y_stride*f->height;f->uv_bytes=(uint64_t)f->uv_stride*(f->height/2);
    if(f->width>2048 || f->height>1536 || f->y_bytes+f->uv_bytes>8*1024*1024 ||
       !main_span((uintptr_t)f->y,f->y_bytes) || !main_span((uintptr_t)f->uv,f->uv_bytes) ||
       (((uintptr_t)f->y|(uintptr_t)f->uv|f->y_bytes|f->uv_bytes)&63)) return Z8_LUT_BAD_SPAN;
    Z8Look off={0,17,1,0};return z8_lut_surface(f,&off);
}

static void release_cache(volatile Z8Preview *p)
{
    /* The native pool mutex is acquired after our mode-entry hook. Serialize
     * concurrent cleanup here so a second transition cannot reconfigure the
     * pool while the first still cleans or frees its detached resource. */
    for(;;) {
        uint32_t expected=0;
        if(__atomic_compare_exchange_n(&Z8_STATE->cache_cleanup_lock,&expected,1,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) break;
        TASK_SLEEP(1);
    }
    uint64_t resource=__atomic_exchange_n(&p->cache_resource,0,__ATOMIC_ACQ_REL);
    uint64_t cached=__atomic_exchange_n(&p->cache,0,__ATOMIC_ACQ_REL);
    __atomic_store_n(&p->cache_choice,0,__ATOMIC_RELEASE);
    if(resource && cached) {
        /* Prevent dirty CPU lines from reaching a later owner's DMA buffer. */
        clean((const void *)(uintptr_t)cached,Z8_PREVIEW_TABLE_BYTES);
        invalidate((const void *)(uintptr_t)cached,Z8_PREVIEW_TABLE_BYTES);
    }
    if(resource) {
        if(!POOL_FREE(resource)) p->worker_error=4;
        __atomic_fetch_add(&p->cache_releases,1,__ATOMIC_RELAXED);
    }
    __atomic_store_n(&Z8_STATE->cache_cleanup_lock,0,__ATOMIC_RELEASE);
}

static int acquire_cache(volatile Z8Preview *p)
{
    if(p->cache) return 1;
    if(read32(POOL_MODE)!=0 || !POOL_AVAILABLE(0x24)) return 0;
    uint64_t resource=POOL_ALLOC(0,0x24,Z8_PREVIEW_TABLE_BYTES);
    if(!resource) { p->allocation_failed++;return 0; }
    if(!main_span(resource,16)) { p->worker_error=5;return 0; }
    uint64_t descriptor=read64(resource+8);
    if(!main_span(descriptor,12)) { POOL_FREE(resource);p->worker_error=5;return 0; }
    uint64_t raw=read64(descriptor);
    if(read32(descriptor+8)<Z8_PREVIEW_TABLE_BYTES ||
       !range(raw,Z8_PREVIEW_TABLE_BYTES,UINT64_C(0x840000000),UINT64_C(0xa00000000)) || (raw&63)) {
        POOL_FREE(resource);p->allocation_failed++;return 0;
    }
    uint8_t *cpu=cpu_address(raw);
    if((uintptr_t)cpu!=raw+UINT64_C(0x400000000)) { POOL_FREE(resource);p->worker_error=5;return 0; }
    p->cache_resource=resource;p->cache=(uintptr_t)cpu;p->cache_choice=0;p->cache_allocations++;
    return 1;
}

/* The general heap is shared with native UI tasks. Retain five working
 * frames during preview; return retired spare allocations after a reset and
 * all unowned allocations when Off. Display-owned or cached frames stay pinned. */
static void trim_frames(unsigned retain)
{
    volatile Z8Preview *p=&Z8_STATE->preview;
    uint32_t unlocked=0;
    if(!__atomic_compare_exchange_n(&p->producer_lock,&unlocked,1,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) return;
    unsigned count=0;
    for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) count+=p->slots[i].allocation!=0;
    for(unsigned i=Z8_PREVIEW_SLOTS;i-- && count>retain;) {
        volatile Z8PreviewSlot *slot=&p->slots[i];
        uint32_t expected=SLOT_FREE;
        if(!slot->allocation || cache_pins(slot) ||
           !__atomic_compare_exchange_n(&slot->state,&expected,SLOT_WRITING,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) continue;
        clean((const void *)(uintptr_t)slot->y,slot->capacity);
        invalidate((const void *)(uintptr_t)slot->y,slot->capacity);
        FREE((void *)(uintptr_t)slot->allocation);
        p->allocated_bytes-=slot->capacity;
        slot->allocation=slot->y=slot->uv=0;slot->capacity=0;count--;
        __atomic_store_n(&slot->state,SLOT_FREE,__ATOMIC_RELEASE);
    }
    __atomic_store_n(&p->producer_lock,0,__ATOMIC_RELEASE);
}

static void discard_pending(volatile Z8Preview *p)
{
    for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) {
        uint32_t previous=SLOT_PENDING;
        if(!__atomic_compare_exchange_n(&p->slots[i].state,&previous,SLOT_FREE,0,__ATOMIC_RELEASE,__ATOMIC_RELAXED)) {
            previous=SLOT_READY;
            __atomic_compare_exchange_n(&p->slots[i].state,&previous,SLOT_FREE,0,__ATOMIC_RELEASE,__ATOMIC_RELAXED);
        }
    }
}

static void preview_worker(void *argument)
{
    PreviewWorker *worker=argument;
    volatile Z8Preview *p=&Z8_STATE->preview;
    for(;;) {
        uint32_t epoch=__atomic_load_n(&p->mode_epoch,__ATOMIC_ACQUIRE);
        if(__atomic_load_n(&p->paused,__ATOMIC_ACQUIRE) || __atomic_load_n(&p->power_paused,__ATOMIC_ACQUIRE) || read32(POOL_MODE)!=0) {
            EVENT_WAIT(worker->event,1,0,1000);continue;
        }
        if(!choice()) {
            __atomic_store_n(&p->cache_busy,1,__ATOMIC_RELEASE);
            __atomic_thread_fence(__ATOMIC_SEQ_CST);
            if(!__atomic_load_n(&p->paused,__ATOMIC_ACQUIRE) && epoch==__atomic_load_n(&p->mode_epoch,__ATOMIC_ACQUIRE)) {
                release_cache(p);discard_pending(p);trim_frames(0);
            }
            __atomic_store_n(&p->cache_busy,0,__ATOMIC_RELEASE);
            EVENT_WAIT(worker->event,1,0,1000);continue;
        }
        volatile Z8PreviewSlot *slot=0;
        for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) {
            volatile Z8PreviewSlot *candidate=&p->slots[i];
            if(__atomic_load_n(&candidate->state,__ATOMIC_ACQUIRE)==SLOT_PENDING &&
               (!slot || (int32_t)(candidate->serial-slot->serial)>0)) slot=candidate;
        }
        if(!slot) { EVENT_WAIT(worker->event,1,0,1000);continue; }
        uint32_t expected=SLOT_PENDING;
        if(!__atomic_compare_exchange_n(&slot->state,&expected,SLOT_TRANSFORM,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) continue;
        __atomic_store_n(&p->cache_busy,1,__ATOMIC_RELEASE);
        __atomic_thread_fence(__ATOMIC_SEQ_CST);
        uint32_t selected=slot->choice;
        unsigned error=0;
        if(__atomic_load_n(&p->paused,__ATOMIC_ACQUIRE) || __atomic_load_n(&p->power_paused,__ATOMIC_ACQUIRE) ||
           epoch!=__atomic_load_n(&p->mode_epoch,__ATOMIC_ACQUIRE)) error=Z8_LUT_CANCELLED;
        if(!error && selected && selected==choice()) {
            Z8Surface output={.y=(uint8_t *)(uintptr_t)slot->y,.uv=(uint8_t *)(uintptr_t)slot->uv,
                .width=slot->width,.height=slot->height,.y_stride=slot->y_stride,
                .uv_stride=slot->uv_stride,.uv_divisor=slot->uv_divisor};
            output.y_bytes=(uint64_t)output.y_stride*output.height;
            output.uv_bytes=(uint64_t)output.uv_stride*(output.height/output.uv_divisor);
            Z8Look look;
            p->started=ticks();
            error=z8_lut_select(library_active(),selected,256,&look);
            if(!error && !look.identity) {
                acquire_cache(p);
                if(p->cache && p->cache_choice!=selected) {
                    error=z8_lut_preview_build_cancel(&look,(uint64_t *)(uintptr_t)p->cache,Z8_PREVIEW_TABLE_BYTES,&p->paused);
                    if(!error) { p->cache_choice=selected;p->cache_builds++; }
                    else p->cache_choice=0;
                }
                if(!error && p->cache) error=z8_lut_preview_surface_cancel(&output,(const uint64_t *)(uintptr_t)p->cache,Z8_PREVIEW_TABLE_BYTES,&p->paused);
                else if(!error) error=z8_lut_surface_cancel(&output,&look,&p->paused);
            }
            if(!error) {
                clean(output.y,output.y_bytes);clean(output.uv,output.uv_bytes);
                p->finished=ticks();p->produced++;p->pixels+=(uint64_t)output.width*output.height;
                p->error=0;
            }
        }
        if(error==Z8_LUT_CANCELLED) p->cancelled++;
        else if(error) { p->error=error;__atomic_fetch_add(&p->rejected,1,__ATOMIC_RELAXED); }
        unsigned ready=!error && selected && selected==choice() &&
            !__atomic_load_n(&p->paused,__ATOMIC_ACQUIRE) && epoch==__atomic_load_n(&p->mode_epoch,__ATOMIC_ACQUIRE);
        __atomic_store_n(&slot->state,ready?SLOT_READY:SLOT_FREE,__ATOMIC_RELEASE);
        if(ready) for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) {
            volatile Z8PreviewSlot *old=&p->slots[i];
            if(old==slot || (int32_t)(old->serial-slot->serial)>=0) continue;
            uint32_t previous=SLOT_READY;
            __atomic_compare_exchange_n(&old->state,&previous,SLOT_FREE,0,__ATOMIC_RELEASE,__ATOMIC_RELAXED);
        }
        trim_frames(5);
        __atomic_store_n(&p->cache_busy,0,__ATOMIC_RELEASE);
        TASK_SLEEP(1);
    }
}

/* Reuse the proven native worker ABI used by pixel-shift processing. The
 * task block has independent heap lifetime and is never freed while live. */
static int preview_start_worker(volatile Z8Preview *p)
{
    if(p->worker_error) return 0;
    uint32_t id=p->worker_id;
    if(p->worker && id && id<=0x300 && p->worker_started) {
        uint64_t entry=UINT64_C(0xa03203e08)+16ull*id;
        if(read32(entry)==id && read64(entry+8)==p->worker &&
           read64(p->worker+8)==(uintptr_t)preview_worker) return 1;
        p->worker_error=1;return 0;
    }
    PreviewWorker *worker=ALLOC(sizeof(*worker),0);
    if(!worker) { p->allocation_failed++;return 0; }
    for(unsigned i=0;i<sizeof(*worker);i++) ((uint8_t *)worker)[i]=0;
    uint32_t parameters[7]={2,40,16384,0,0,0,0};
    copy_bytes((char *)parameters+TASK_NAME,"PhotoLUT",8);
    TASK_CREATE(worker->task,preview_worker,parameters);
    if(*(int32_t *)worker<=0) { FREE(worker);p->worker_error=2;return 0; }
    p->worker=(uintptr_t)worker;p->worker_id=*(uint32_t *)worker;
    EVENT_CREATE(worker->event,0,1,"PhotoLUT");
    if(worker->event[0]<=0) { p->worker_error=3;return 0; }
    TASK_START(worker->task,worker);
    p->worker_started=1;
    return 1;
}

/* Native FRECT wait success (40e991b0) owns the software current frame.
 * The following display enqueue already describes the NEXT source frame.
 * Copy privately here, outside the display mutex; a separate native task
 * transforms the copy so LUT work never blocks the original frame waits.
 * Timestamp comparison rejects an interrupted copy; it is not a physical
 * DMA lease or a substitute for camera cache/timing qualification. */
void z8_photo_lut_frame(const uint64_t *saved)
{
    volatile Z8LutState *s=Z8_STATE;
    if(s->magic!=Z8_STATE_MAGIC || !s->ready || !(saved[0]&1)) return;
    uint64_t stack=(uintptr_t)saved+256,context=saved[19];
    uint32_t flags=read32(stack+4);
    if(!(flags&0x40000) || (flags&0x80000000) || !main_span(context,0xc8) ||
       read32(context+0x10)!=6) return;
    volatile Z8Preview *p=&s->preview;
    __atomic_fetch_add(&p->completed,1,__ATOMIC_RELAXED);
    uint32_t epoch=__atomic_load_n(&p->mode_epoch,__ATOMIC_ACQUIRE);
    if(__atomic_load_n(&p->paused,__ATOMIC_ACQUIRE) || __atomic_load_n(&p->power_paused,__ATOMIC_ACQUIRE)) return;
    uint32_t selected=choice();
    if(!selected) return;
    uint32_t expected=0;
    if(!__atomic_compare_exchange_n(&p->producer_lock,&expected,1,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) return;
    volatile Z8PreviewSlot *slot=0;
    unsigned error=0;
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    if(__atomic_load_n(&p->paused,__ATOMIC_ACQUIRE) || epoch!=__atomic_load_n(&p->mode_epoch,__ATOMIC_ACQUIRE)) goto done;
    if(!preview_start_worker(p)) goto done;
    uint64_t object=read64(context+0xc0);
    Z8Surface source;
    if(!main_span(object,0x58) || (error=preview_source(object+0x30,&source))) {
        if(!error) error=Z8_LUT_BAD_SPAN;
        goto done;
    }
    /* Old pending copies may be replaced; READY and QUEUED images remain
     * available for the display while the worker processes a newer copy. */
    for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) {
        expected=SLOT_PENDING;
        if(__atomic_compare_exchange_n(&p->slots[i].state,&expected,SLOT_WRITING,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) { slot=&p->slots[i];break; }
    }
    if(!slot) for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) {
        expected=SLOT_FREE;
        if(__atomic_compare_exchange_n(&p->slots[i].state,&expected,SLOT_WRITING,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) { slot=&p->slots[i];break; }
    }
    if(!slot) { p->pool_busy++;goto done; }
    uint32_t bytes=(uint32_t)(source.y_bytes+source.uv_bytes);
    if(slot->capacity<bytes) {
        if(p->allocated_bytes-slot->capacity+bytes>8*1024*1024) { p->allocation_failed++;goto done; }
        if(slot->allocation) { FREE((void *)(uintptr_t)slot->allocation);p->allocated_bytes-=slot->capacity;slot->allocation=0;slot->capacity=0; }
        void *memory=ALLOC(bytes+63,0);
        if(!memory) { p->allocation_failed++;goto done; }
        slot->allocation=(uintptr_t)memory;slot->y=((uintptr_t)memory+63)&~UINT64_C(63);slot->capacity=bytes;
        p->allocated_bytes+=bytes;
    }
    slot->uv=slot->y+source.y_bytes;
    if(!main_span(slot->y,bytes)) { error=Z8_LUT_BAD_SPAN;goto done; }
    if((slot->y<(uintptr_t)source.y+source.y_bytes && (uintptr_t)source.y<slot->y+bytes) ||
       (slot->y<(uintptr_t)source.uv+source.uv_bytes && (uintptr_t)source.uv<slot->y+bytes)) {
        error=Z8_LUT_BAD_SPAN;goto done;
    }
    uint64_t stamp=read64(UINT64_C(0xa03b83470));
    uint8_t *source_y=cpu_address((uintptr_t)source.y),*source_uv=cpu_address((uintptr_t)source.uv);
    if(!source_y || !source_uv) { error=Z8_LUT_BAD_SPAN;goto done; }
    invalidate(source_y,source.y_bytes);invalidate(source_uv,source.uv_bytes);
    copy_bytes((void *)(uintptr_t)slot->y,source_y,(uint32_t)source.y_bytes);
    copy_bytes((void *)(uintptr_t)slot->uv,source_uv,(uint32_t)source.uv_bytes);
    __atomic_thread_fence(__ATOMIC_ACQUIRE);
    if(stamp!=read64(UINT64_C(0xa03b83470))) { p->copy_races++;goto done; }
    if(choice()!=selected || __atomic_load_n(&p->paused,__ATOMIC_ACQUIRE) ||
       epoch!=__atomic_load_n(&p->mode_epoch,__ATOMIC_ACQUIRE)) goto done;
    slot->choice=selected;slot->source_y=(uintptr_t)source.y;
    slot->width=source.width;slot->height=source.height;slot->y_stride=source.y_stride;
    slot->uv_stride=source.uv_stride;slot->uv_divisor=source.uv_divisor;
    slot->serial=++p->serial;
    if(!slot->serial) slot->serial=++p->serial;
    __atomic_store_n(&slot->state,SLOT_PENDING,__ATOMIC_RELEASE);
    EVENT_SET(((PreviewWorker *)(uintptr_t)p->worker)->event,1);
    slot=0;
done:
    if(error) { p->error=error;__atomic_fetch_add(&p->rejected,1,__ATOMIC_RELAXED); }
    if(slot) __atomic_store_n(&slot->state,SLOT_FREE,__ATOMIC_RELEASE);
    __atomic_store_n(&p->producer_lock,0,__ATOMIC_RELEASE);
}

/* The lower display manager retains this request even after replay and
 * queue retirement. Only states0/2 invalidate it; state1 remains replayable. */
static int cache_pins(volatile const Z8PreviewSlot *slot)
{
    if(read32(UINT64_C(0xa02e71908))!=1) return 0;
    return read64(UINT64_C(0xa02e71a20))==slot->y || read64(UINT64_C(0xa02e71a28))==slot->uv;
}

static void make_orphan(volatile Z8Preview *p,volatile Z8PreviewSlot *slot)
{
    uint32_t old=SLOT_QUEUED;
    if(__atomic_compare_exchange_n(&slot->state,&old,SLOT_ORPHAN,0,__ATOMIC_ACQ_REL,__ATOMIC_RELAXED))
        __atomic_fetch_add(&p->orphaned,1,__ATOMIC_RELAXED);
}

static void retire_private(volatile Z8Preview *p,volatile Z8PreviewSlot *slot)
{
    uint32_t flags=__atomic_load_n(&slot->flags,__ATOMIC_ACQUIRE);
    uint64_t sequence=slot->apply_sequence;
    uint64_t latest=__atomic_load_n(&p->applies,__ATOMIC_ACQUIRE);
    int fence=(flags&SLOT_APPLIED) && latest>=sequence && latest-sequence>=2;
    if(fence) {
        /* Pointer clearing is not a fence. A genuinely programmed image
         * must itself retire, and any replayable cached request stays pinned. */
        for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) {
            volatile Z8PreviewSlot *old=&p->slots[i];
            if(old==slot || __atomic_load_n(&old->state,__ATOMIC_ACQUIRE)!=SLOT_ORPHAN ||
               old->apply_sequence>=sequence || cache_pins(old)) continue;
            uint32_t expected=SLOT_ORPHAN;
            if(__atomic_compare_exchange_n(&old->state,&expected,SLOT_FREE,0,__ATOMIC_RELEASE,__ATOMIC_RELAXED))
                __atomic_fetch_add(&p->orphan_reclaimed,1,__ATOMIC_RELAXED);
        }
    }
    if(!cache_pins(slot) && (fence || !(flags&SLOT_MAY_APPLY)))
        __atomic_store_n(&slot->state,SLOT_FREE,__ATOMIC_RELEASE);
    else make_orphan(p,slot);
}

static void preview_callback(uint32_t event,const void *payload)
{
    if(!payload || ((uintptr_t)payload&7) || !main_span((uintptr_t)payload,12)) return;
    uint64_t y=read64((uintptr_t)payload);
    uint32_t cookie=read32((uintptr_t)payload+8);
    volatile Z8Preview *p=&Z8_STATE->preview;
    for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) {
        volatile Z8PreviewSlot *slot=&p->slots[i];
        uint32_t state=__atomic_load_n(&slot->state,__ATOMIC_ACQUIRE);
        if((state!=SLOT_QUEUED && state!=SLOT_ORPHAN) || slot->y!=y || slot->serial!=cookie) continue;
        uint32_t channel=slot->channel;
        struct { uint64_t y;uint32_t cookie,padding; } original={slot->original_y,slot->original_cookie,0};
        if(event==0x43130005+channel) {
            __atomic_fetch_or(&slot->flags,SLOT_SUBMITTED,__ATOMIC_RELEASE);
            __atomic_fetch_add(&p->submitted,1,__ATOMIC_RELAXED);
        }
        ((void (*)(uint32_t,const void *))(uintptr_t)0x40e9a570)(event,&original);
        if(event==0x43130009+channel) {
            __atomic_fetch_add(&p->retired,1,__ATOMIC_RELAXED);
            retire_private(p,slot);
        }
        return;
    }
    __atomic_fetch_add(&p->stale_callbacks,1,__ATOMIC_RELAXED);
}

/* At40cd7d54 the native queue has accepted this request and holds its mutex.
 * Its cached fallback still refers to the original source, never our pool.
 * No allocation, pixel access or blocking transform occurs under this lock. */
unsigned z8_photo_lut_queue(uint64_t *saved)
{
    saved[1]=saved[23]; /* displaced mov x1,x23 */
    volatile Z8LutState *s=Z8_STATE;
    uint32_t channel=(uint32_t)saved[27];
    if(s->magic!=Z8_STATE_MAGIC || !s->ready || (channel!=0 && channel!=3) ||
       saved[28]!=UINT64_C(0x40e9a570)) return 0;
    if(read32(UINT64_C(0xa02810470))!=(channel==3?1u:0u)) return 0;
    uint64_t stack=(uintptr_t)saved+256;
    uint64_t parent=read64(stack+0x168);
    if(read64(stack+0x88)!=UINT64_C(0x40e9a544) ||
       (channel==0 ? (parent!=UINT64_C(0x40e98d74) && parent!=UINT64_C(0x40e99270)) :
                     (parent!=UINT64_C(0x40e98d90) && parent!=UINT64_C(0x40e992b0)))) return 0;
    uint64_t context=read64(stack+0x160),request=saved[23];
    if(!main_span(context,0x14) || read32(context+0x10)!=6 || !main_span(request,0x98) ||
       *(const uint8_t *)(uintptr_t)request!=1) return 0;
    uint32_t selected=choice();
    if(!selected) return 0;
    volatile Z8Preview *p=&s->preview;
    volatile Z8PreviewSlot *best=0;
    if(__atomic_load_n(&p->paused,__ATOMIC_ACQUIRE) || __atomic_load_n(&p->power_paused,__ATOMIC_ACQUIRE)) goto skip;
    for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) {
        volatile Z8PreviewSlot *slot=&p->slots[i];
        if(__atomic_load_n(&slot->state,__ATOMIC_ACQUIRE)!=SLOT_READY) continue;
        if(slot->choice!=selected) {
            uint32_t expected=SLOT_READY;
            __atomic_compare_exchange_n(&slot->state,&expected,SLOT_FREE,0,__ATOMIC_RELEASE,__ATOMIC_RELAXED);
        } else if(!best || (int32_t)(slot->serial-best->serial)>0) best=slot;
    }
    if(best) {
        uint32_t expected=SLOT_READY;
        if(__atomic_compare_exchange_n(&best->state,&expected,SLOT_QUEUED,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) {
            Z8Surface next;
            unsigned error=preview_source(request+8,&next);
            if(!error && next.width==best->width && next.height==best->height &&
               next.y_stride==best->y_stride && next.uv_stride==best->uv_stride) {
                copy_bytes((void *)best->request,(const void *)(uintptr_t)request,0x98);
                best->original_y=(uintptr_t)next.y;best->original_cookie=read32(request+0x90);
                best->channel=channel;
                best->epoch=__atomic_load_n(&p->display_epoch,__ATOMIC_ACQUIRE);
                best->apply_sequence=0;best->flags=0;
                *(uint64_t *)(void *)(best->request+0x20)=best->y;
                *(uint64_t *)(void *)(best->request+0x28)=best->uv;
                *(uint32_t *)(void *)(best->request+0x90)=best->serial;
                __atomic_thread_fence(__ATOMIC_RELEASE);
                saved[1]=(uintptr_t)best->request;saved[28]=(uintptr_t)preview_callback;
                p->enqueued++;
                return 0;
            }
            p->error=error?error:Z8_LUT_BAD_GEOMETRY;p->rejected++;
            __atomic_store_n(&best->state,SLOT_FREE,__ATOMIC_RELEASE);
        }
    }
    /* FIFO index is still unchanged. Use the native unlock then its return0
     * epilogue. Keeping the current scanout avoids an unfiltered flash. */
skip:
    p->skipped++;
    QUEUE_UNLOCK((void *)(uintptr_t)read64(stack+8));
    return 1;
}

static void reset_display_queue(uint32_t channel)
{
    if((channel!=0 && channel!=3) || Z8_STATE->magic!=Z8_STATE_MAGIC || !Z8_STATE->ready) return;
    volatile Z8Preview *p=&Z8_STATE->preview;
    __atomic_fetch_add(&p->display_epoch,1,__ATOMIC_ACQ_REL);
    __atomic_fetch_add(&p->queue_resets,1,__ATOMIC_RELAXED);
    p->last_reset_channel=channel;
    /* Called while that channel's FIFO mutex is held, immediately after its
     * pointers clear. New admissions cannot be misclassified as old owners. */
    for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) if(p->slots[i].channel==channel) make_orphan(p,&p->slots[i]);
}

void z8_photo_lut_queue_reset(const uint64_t *saved)
{ reset_display_queue((uint32_t)saved[23]); }
void z8_photo_lut_queue_construct(const uint64_t *saved)
{ reset_display_queue(4u-(uint32_t)saved[23]); }

static void observe_application(const uint64_t *saved,int completed)
{
    if(Z8_STATE->magic!=Z8_STATE_MAGIC || !Z8_STATE->ready ||
       (completed && (uint32_t)saved[0]!=0) || saved[21]!=UINT64_C(0xa02e70c80) ||
       !main_span(saved[19],0x118)) return;
    uint64_t handle=read64(saved[21]);
    if(!main_span(handle,16) || read64(handle+8)!=UINT64_C(0x15484000)) return;
    uint64_t y=read64(saved[19]+0x108),uv=read64(saved[19]+0x110);
    if(!y || !uv) return;
    volatile Z8Preview *p=&Z8_STATE->preview;
    uint64_t sequence=completed?__atomic_add_fetch(&p->applies,1,__ATOMIC_ACQ_REL):0;
    for(unsigned i=0;i<Z8_PREVIEW_SLOTS;i++) {
        volatile Z8PreviewSlot *slot=&p->slots[i];
        uint32_t state=__atomic_load_n(&slot->state,__ATOMIC_ACQUIRE);
        if((state!=SLOT_QUEUED && state!=SLOT_ORPHAN) || slot->y!=y || slot->uv!=uv) continue;
        if(completed) slot->apply_sequence=sequence;
        __atomic_fetch_or(&slot->flags,completed?(SLOT_APPLIED|SLOT_MAY_APPLY):SLOT_MAY_APPLY,__ATOMIC_RELEASE);
        break;
    }
}
void z8_photo_lut_will_apply(const uint64_t *saved) { observe_application(saved,0); }
void z8_photo_lut_applied(const uint64_t *saved) { observe_application(saved,1); }

void z8_photo_lut_mode_enter(void)
{
    if(Z8_STATE->magic!=Z8_STATE_MAGIC || !Z8_STATE->ready) return;
    volatile Z8Preview *p=&Z8_STATE->preview;
    __atomic_fetch_add(&p->paused,1,__ATOMIC_ACQ_REL);
    __atomic_fetch_add(&p->mode_epoch,1,__ATOMIC_ACQ_REL);
    __atomic_fetch_add(&p->mode_entries,1,__ATOMIC_RELAXED);
    /* This entry precedes the native manager mutex. The worker's busy
     * interval includes POOL_AVAILABLE/ALLOC, CPU use and publication. */
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    while(__atomic_load_n(&p->producer_lock,__ATOMIC_ACQUIRE) ||
          __atomic_load_n(&p->cache_busy,__ATOMIC_ACQUIRE)) TASK_SLEEP(1);
    release_cache(p);
    discard_pending(p);
}

void z8_photo_lut_mode_exit(void)
{
    if(Z8_STATE->magic!=Z8_STATE_MAGIC || !Z8_STATE->ready) return;
    volatile Z8Preview *p=&Z8_STATE->preview;
    if(__atomic_load_n(&p->paused,__ATOMIC_ACQUIRE) &&
       __atomic_fetch_sub(&p->paused,1,__ATOMIC_ACQ_REL)==1 && !__atomic_load_n(&p->power_paused,__ATOMIC_ACQUIRE) &&
       p->worker && p->worker_started && read32(POOL_MODE)==0)
        EVENT_SET(((PreviewWorker *)(uintptr_t)p->worker)->event,1);
}

void z8_photo_lut_shutdown(const uint64_t *saved)
{
    if(Z8_STATE->magic!=Z8_STATE_MAGIC || !Z8_STATE->ready || (uint32_t)saved[0]!=0x05020102) return;
    uint32_t expected=0;
    if(!__atomic_compare_exchange_n(&Z8_STATE->preview.power_paused,&expected,1,0,__ATOMIC_ACQ_REL,__ATOMIC_RELAXED)) return;
    z8_photo_lut_mode_enter();
    /* Do not reject or drain JPEG here: this notification precedes capture
     * completion. Native BSShutdown waits bit0x400, owned by the entire image
     * job (40f6fa64..40f6fbc4), including our synchronous JPEG operation.
     * Returning an invented encoder error would not cancel its caller. */
}

void z8_photo_lut_power_cancel(void)
{
    if(Z8_STATE->magic!=Z8_STATE_MAGIC || !Z8_STATE->ready) return;
    if(__atomic_exchange_n(&Z8_STATE->preview.power_paused,0,__ATOMIC_ACQ_REL)) {
        z8_photo_lut_mode_exit();
    }
}

/* Status shown on the card row: load results, and why the selected bank's
 * look is not applied. */
enum { CARD_IDLE,CARD_LOADED,CARD_NO_FILE,CARD_INVALID,CARD_CONFLICT,CARD_READ,CARD_MEMORY,CARD_BUSY,
       CARD_RESTORING,CARD_NOT_IN_LIBRARY,CARD_UNRESOLVED,CARD_STATES };
/* English, Simplified and Traditional Chinese; \1 = look count, \2 = slot(s). */
static const char *const card_text[CARD_STATES][3]={
    {"Load LUTs from card","从存储卡载入 LUT","從記憶卡載入 LUT"},
    {"Card LUTs: \1 loaded (slot \2)","存储卡 LUT：已载入 \1 个（卡槽 \2）","記憶卡 LUT：已載入 \1 個（卡槽 \2）"},
    {"No Z8LUTLIB.BIN on either card","两张卡上都没有 Z8LUTLIB.BIN","兩張卡上都沒有 Z8LUTLIB.BIN"},
    {"Slot \2 Z8LUTLIB.BIN invalid; unchanged","卡槽 \2 的 Z8LUTLIB.BIN 无效，未更改","卡槽 \2 的 Z8LUTLIB.BIN 無效，未變更"},
    {"Slots 1 and 2 differ; unchanged","两张卡的 Z8LUTLIB.BIN 不同，未更改","兩張卡的 Z8LUTLIB.BIN 不同，未變更"},
    {"Slot \2 read error; unchanged","卡槽 \2 读取失败，未更改","卡槽 \2 讀取失敗，未變更"},
    {"Not enough memory; unchanged","内存不足，未更改","記憶體不足，未變更"},
    {"Card busy; try again","存储卡正在使用，请重试","記憶卡正在使用，請重試"},
    {"Restoring LUT settings...","正在恢复 LUT 设置...","正在恢復 LUT 設定..."},
    {"Selected LUT not on card; choose again","存储卡上没有所选 LUT，请重新选择","記憶卡上沒有所選 LUT，請重新選擇"},
    {"LUT setting not recognized; choose again","LUT 设置无法识别，请重新选择","LUT 設定無法識別，請重新選擇"},
};

static void menu_text(const Z8LookBundle *,uint32_t status,uint32_t count,uint32_t slots);
static int card_worker_start(void);
static void card_wake(void);
static void persist_bootstrap(void);
static void record_lock(void);
static void record_unlock(void);
static void preview_wake(void);

/* ---- bank record persistence ------------------------------------------- */
/* Native settings interfaces. Section-15 restore and the
 * blob-16 read are separate native steps; the record is decided once both
 * have reported, in either order. Only an actual read of the stored region,
 * or a native reset, decides what may be stored: a failed or skipped read
 * leaves every bank unresolved and stores nothing. Group 0x31's loaded byte
 * (0x41016aac/0x41016550) is native-owned: mode 4 sets it after loading
 * blob 16, and the save producer stores blob 16 only while it is set. */
#define REQUEST_SAVE ((void (*)(void))(uintptr_t)0x41012bf4)
#define SETTINGS_REPLY ((uint32_t (*)(uint64_t,uint64_t,uint64_t,uint64_t,uint64_t))(uintptr_t)0x410130e0)
#define SETTINGS_DEFAULTS ((void (*)(void))(uintptr_t)0x41016480)
#define SECTION_READ ((uint32_t (*)(uint32_t,uint64_t,uint32_t))(uintptr_t)0x40d6c7d8)
#define SECTION_WRITE ((uint32_t (*)(uint32_t,uint64_t,uint32_t))(uintptr_t)0x40d6c628)
#define SECTION ((uint64_t (*)(uint32_t))(uintptr_t)0x40d712c8)
#define GROUP_LOADED ((uint32_t (*)(uint32_t))(uintptr_t)0x41016aac)
#define BLOB_TOTAL (Z8_BLOB_BYTES+(uint32_t)sizeof(Z8BankRecord))
#define BLOB_ALLOC_TRIES 50u

/* Every record starts all-zero, so no byte it carries (or hashes) is left
 * over from a stack or an earlier record. */
static void record_init(Z8BankRecord *r) { for(unsigned i=0;i<sizeof(*r);i++) ((uint8_t *)r)[i]=0; }
static void record_seal(Z8BankRecord *r)
{
    r->magic=Z8_RECORD_MAGIC;r->version=1;r->bytes=sizeof(*r);r->reserved=0;
    z8_lut_sha256(r,offsetof(Z8BankRecord,sha256),r->sha256);
}

static int record_valid(const Z8BankRecord *r)
{
    if(r->magic!=Z8_RECORD_MAGIC || r->version!=1 || r->bytes!=sizeof(*r) || r->library>Z8_LIBRARY_CARD ||
       r->reserved) return 0;
    for(unsigned bank=0;bank<Z8_BANKS;bank++) {
        const Z8BankSlot *slot=&r->banks[bank];
        if(slot->kind>Z8_DESIRED_UNRESOLVED || slot->reserved[0] || slot->reserved[1] || slot->reserved[2]) return 0;
        if(slot->kind!=Z8_DESIRED_LOOK)
            for(unsigned i=0;i<sizeof(slot->id);i++) if(slot->id[i]) return 0;
    }
    uint8_t digest[32];
    z8_lut_sha256(r,offsetof(Z8BankRecord,sha256),digest);
    return same_bytes(digest,r->sha256,32);
}

/* Record writers hold record_lock; readers use the sequence. */
static void record_read(Z8BankRecord *out)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    for(;;) {
        uint32_t before=__atomic_load_n(&p->sequence,__ATOMIC_ACQUIRE);
        copy_bytes(out,(const void *)(uintptr_t)&p->record,sizeof(*out));
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        if(!(before&1) && __atomic_load_n(&p->sequence,__ATOMIC_RELAXED)==before) return;
        TASK_SLEEP(1);
    }
}
static void sequence_open(void) { __atomic_fetch_add(&Z8_STATE->persist.sequence,1,__ATOMIC_ACQ_REL); }
static void sequence_close(void) { __atomic_fetch_add(&Z8_STATE->persist.sequence,1,__ATOMIC_RELEASE); }
static void record_publish(Z8BankRecord *next)
{
    record_seal(next);
    sequence_open();
    copy_bytes((void *)(uintptr_t)&Z8_STATE->persist.record,next,sizeof(*next));
    sequence_close();
}

static void slot_clear(Z8BankSlot *slot) { for(unsigned i=0;i<sizeof(*slot);i++) ((uint8_t *)slot)[i]=0; }
/* The firmware default: the built-in library's default look, or Off. */
static void default_slot(Z8BankSlot *slot)
{
    slot_clear(slot);
    const Z8LookBundle *b=Z8_BUILTIN;
    if(Z8_STATE->ready && b->initial) {
        slot->kind=Z8_DESIRED_LOOK;
        copy_bytes(slot->id,b->looks[b->initial-1].id,sizeof(slot->id));
    }
}

static uint32_t owned(unsigned copy)
{ return __atomic_load_n((volatile uint16_t *)(uintptr_t)(Z8_OWNED_BYTES+copy*Z8_BANK_STRIDE),__ATOMIC_ACQUIRE); }
/* Returns whether any owned bytes changed (a section-15 save is needed). */
static int own_all(void)
{
    int changed=0;
    for(unsigned copy=0;copy<Z8_BANK_COPIES;copy++) {
        changed|=owned(copy)!=Z8_OWNED_RECORDED;
        __atomic_store_n((volatile uint16_t *)(uintptr_t)(Z8_OWNED_BYTES+copy*Z8_BANK_STRIDE),
                         (uint16_t)Z8_OWNED_RECORDED,__ATOMIC_RELEASE);
    }
    return changed;
}

/* Store the record with blob 16 and the owned bytes with section 15 through
 * the native asynchronous settings save (its region writer skips an
 * unchanged region). Only for a record decided from an actual read. */
static void commit(void)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    if(!__atomic_load_n(&p->authoritative,__ATOMIC_ACQUIRE) || p->blob==Z8_BLOB_PENDING) return;
    p->commits++;
    REQUEST_SAVE();
}

static uint8_t *blob_scratch(void)
{
    for(unsigned tries=0;tries<BLOB_ALLOC_TRIES;tries++) {
        uint8_t *scratch=ALLOC(BLOB_TOTAL,0);
        if(scratch && !((uintptr_t)scratch&7)) return scratch;
        if(scratch) FREE(scratch);
        Z8_STATE->persist.alloc_retries++;
        TASK_SLEEP(10);
    }
    return 0;
}

static int erased(const uint8_t *p,uint32_t n)
{
    for(uint32_t i=0;i<n;i++) if(p[i]!=0xff) return 0;
    return 1;
}
/* The stored region as read by the native reader into scratch: stock bytes
 * then the record. A failed read means the region is unreadable. */
static uint32_t region_state(uint32_t result,const uint8_t *scratch,Z8BankRecord *tail)
{
    if(!(result&1)) return Z8_BLOB_LOST;
    copy_bytes(tail,scratch+Z8_BLOB_BYTES,sizeof(*tail));
    if(erased((const uint8_t *)tail,sizeof(*tail))) return Z8_BLOB_ABSENT;
    return record_valid(tail)?Z8_BLOB_VALID:Z8_BLOB_CORRUPT;
}

/* Decide from the settings-task tail read, a validated retained warm record,
 * or an explicit native reset. A valid stored record restores full identities;
 * absent/lost tails migrate native owned bytes without inventing identities;
 * corrupt tails leave banks unresolved. Pending explicit edits and original
 * source references from deferred bank copies are merged before publication.
 * An undecided record is never written over. */
static void persist_decide(const Z8BankRecord *stored)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    record_lock();
    if(__atomic_load_n(&p->ready,__ATOMIC_ACQUIRE) && p->authoritative) { record_unlock();return; }
    if(stored) {
        Z8BankRecord staged;
        copy_bytes(&staged,stored,sizeof(staged));
        record_publish(&staged);
    }
    if(!p->settings_restored || p->blob==Z8_BLOB_PENDING) { record_unlock();return; }
    Z8BankRecord r;
    record_init(&r);
    uint32_t source,authoritative=1,store=1;
    if(p->defaults) {
        for(unsigned bank=0;bank<Z8_BANKS;bank++) default_slot(&r.banks[bank]);
        source=Z8_SOURCE_DEFAULTS;
    } else if(p->blob==Z8_BLOB_VALID) {
        record_read(&r);
        source=Z8_SOURCE_STORED;store=0;
    } else if(p->blob==Z8_BLOB_SKIPPED && p->retained) {
        record_read(&r);
        source=Z8_SOURCE_RETAINED;store=0;
    } else if(p->blob==Z8_BLOB_ABSENT || p->blob==Z8_BLOB_LOST || p->blob==Z8_BLOB_CORRUPT) {
        unsigned recorded=p->blob==Z8_BLOB_CORRUPT,legacy=0;
        for(unsigned copy=0;copy<Z8_BANK_COPIES;copy++) {
            uint32_t value=owned(copy);
            recorded|=value==Z8_OWNED_RECORDED;legacy|=value && value!=Z8_OWNED_RECORDED;
        }
        uint32_t selected=read32(Z8_BANK_INDEX);
        for(unsigned bank=0;bank<Z8_BANKS;bank++) {
            Z8BankSlot *slot=&r.banks[bank];
            slot_clear(slot);
            /* The current copy holds the selected bank's latest value. */
            uint32_t value=owned(bank==selected?0:bank+1);
            if(recorded) slot->kind=Z8_DESIRED_UNRESOLVED;
            else if(legacy) slot->kind=(value&0xff)?Z8_DESIRED_UNRESOLVED:Z8_DESIRED_OFF;
            else default_slot(slot);
        }
        source=recorded?Z8_SOURCE_LOST:legacy?Z8_SOURCE_MIGRATED:Z8_SOURCE_FRESH;
        store=!recorded;
    } else {
        for(unsigned bank=0;bank<Z8_BANKS;bank++) { slot_clear(&r.banks[bank]);r.banks[bank].kind=Z8_DESIRED_UNRESOLVED; }
        source=Z8_SOURCE_UNREAD;authoritative=0;store=0;
    }
    /* Deferred copies refer to the original unread slots, independently of
     * later edits to their source. Resolve all origins before explicit edits. */
    Z8BankSlot original[Z8_BANKS];
    copy_bytes(original,r.banks,sizeof(original));
    for(unsigned bank=0;bank<Z8_BANKS;bank++) {
        uint32_t origin=(p->pending_sources>>(4*bank))&15;
        if(origin && origin<=Z8_BANKS) { copy_bytes(&r.banks[bank],&original[origin-1],sizeof(Z8BankSlot));store=authoritative; }
    }
    for(unsigned bank=0;bank<Z8_BANKS;bank++)
        if(p->pending_mask&(1u<<bank)) { copy_bytes(&r.banks[bank],(const void *)(uintptr_t)&p->pending[bank],sizeof(Z8BankSlot));store=authoritative; }
    if(p->pending_mask&Z8_PENDING_LIBRARY) { r.library=p->pending_library;store=authoritative; }
    record_publish(&r);
    unsigned owned_changed=0;
    if(authoritative) { owned_changed=own_all();p->pending_mask=0;p->pending_sources=0; }
    p->source=source;
    __atomic_store_n(&p->authoritative,authoritative,__ATOMIC_RELEASE);
    __atomic_store_n(&p->ready,1,__ATOMIC_RELEASE);
    record_unlock();
    if(store) commit();
    else if(owned_changed) REQUEST_SAVE();
    card_wake();
    preview_wake();
    z8_photo_lut_capture_refresh();
}

/* A bank's desired look chosen in the menu or reset by the native producer.
 * Before an actual read decided the record, the change is kept and merged on
 * top of whatever that read finds. */
static void desire_bank(uint32_t bank,const Z8BankSlot *slot)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    record_lock();
    unsigned ready=__atomic_load_n(&p->ready,__ATOMIC_ACQUIRE),authoritative=ready && p->authoritative;
    if(ready) {
        Z8BankRecord r;
        record_read(&r);
        copy_bytes(&r.banks[bank],slot,sizeof(*slot));
        record_publish(&r);
    }
    if(!authoritative) {
        sequence_open();
        copy_bytes((void *)(uintptr_t)&p->pending[bank],slot,sizeof(*slot));
        p->pending_mask|=1u<<bank;
        p->pending_sources&=~(15u<<(4*bank));
        sequence_close();
    }
    record_unlock();
    if(authoritative) commit();
    preview_wake();
    z8_photo_lut_capture_refresh();
}

/* Explicit shooting-bank Copy at 410ddb8c. Native status zero means the
 * main item list was copied; this seam precedes name-save enqueue and any
 * selected-destination refresh. Copies 1..4 are A..D, independent of selection.
 * GUI actions serialize source edits. Observe the source on native success,
 * allowing a concurrent stored-record read to resolve its identity first;
 * no private lock is held across the original native transaction. */
int z8_photo_lut_bank_copy(uint32_t source,uint32_t destination,const uint32_t *items,uint32_t count)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    if(Z8_STATE->magic!=Z8_STATE_MAGIC)
        return ((int (*)(uint32_t,uint32_t,const uint32_t *,uint32_t))(uintptr_t)0x41013a14)(source,destination,items,count);
    int result=((int (*)(uint32_t,uint32_t,const uint32_t *,uint32_t))(uintptr_t)0x41013a14)(source,destination,items,count);
    unsigned changed=result==0 && count && source && source<=Z8_BANKS && destination && destination<=Z8_BANKS && source!=destination;
    record_lock();
    unsigned authoritative=__atomic_load_n(&p->ready,__ATOMIC_ACQUIRE) && p->authoritative;
    if(changed) {
        unsigned from=source-1,to=destination-1;
        unsigned known=authoritative || (p->pending_mask&(1u<<from));
        Z8BankSlot slot;
        slot_clear(&slot);slot.kind=Z8_DESIRED_UNRESOLVED;
        if(known) copy_bytes(&slot,authoritative?(const void *)(uintptr_t)&p->record.banks[from]:
                              (const void *)(uintptr_t)&p->pending[from],sizeof(slot));
        if(p->ready) {
            Z8BankRecord r;record_read(&r);
            copy_bytes(&r.banks[to],&slot,sizeof(slot));record_publish(&r);
        }
        if(!authoritative) {
            unsigned origin=(p->pending_sources>>(4*from))&15;
            sequence_open();
            p->pending_sources&=~(15u<<(4*to));
            if(known) { copy_bytes((void *)(uintptr_t)&p->pending[to],&slot,sizeof(slot));p->pending_mask|=1u<<to; }
            else { p->pending_mask&=~(1u<<to);p->pending_sources|=(origin?origin:source)<<(4*to); }
            sequence_close();
        }
    }
    record_unlock();
    if(changed) {
        if(authoritative) commit();
        preview_wake();z8_photo_lut_capture_refresh();
    }
    return result;
}

/* The configuration's library after an explicit Load (card worker task). */
static int request_cancelled(uint32_t serial)
{
    uint32_t cutoff=__atomic_load_n(&Z8_STATE->persist.cancelled_request,__ATOMIC_ACQUIRE);
    return cutoff && (int32_t)(serial-cutoff)<=0;
}

static void desire_library(uint32_t library,uint32_t serial)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    record_lock();
    if(request_cancelled(serial)) { record_unlock();return; }
    /* The single worker has completed canceled older requests before it
     * can accept this newer one. Do not let an ancient cutoff age across
     * the modular serial comparison window; zero is never a request. */
    __atomic_store_n(&p->cancelled_request,0,__ATOMIC_RELEASE);
    unsigned ready=__atomic_load_n(&p->ready,__ATOMIC_ACQUIRE),authoritative=ready && p->authoritative,changed=1;
    if(ready) {
        Z8BankRecord r;
        record_read(&r);
        changed=r.library!=library;
        r.library=library;
        if(changed) record_publish(&r);
    }
    if(!authoritative) { p->pending_library=library;p->pending_mask|=Z8_PENDING_LIBRARY; }
    record_unlock();
    if(authoritative && changed) commit();
    if(changed) z8_photo_lut_capture_refresh();
}

/* A failed initial task creation has no worker to retry itself. The native
 * settings task is the owner of its restore, so retry from that task's wait
 * loop only while an automatic import or unread-record recovery needs it. */
static int card_start_needed(void)
{
    volatile Z8LutState *s=Z8_STATE;
    volatile Z8Persist *p=&s->persist;
    if(s->magic!=Z8_STATE_MAGIC || !p->settings_restored || s->preview.power_paused ||
       __atomic_load_n(&s->card.worker_state,__ATOMIC_ACQUIRE)==1) return 0;
    if(!p->ready || !p->authoritative) return 1;
    Z8BankRecord r;record_read(&r);
    return r.library==Z8_LIBRARY_CARD;
}

/* 41012924 originally waits indefinitely at 40d2bd04 (kernel mode 1), then
 * dispatches the returned bits. 40d2bd64 is its timed mode-1 counterpart;
 * 40d2bdac uses mode 0x21 and would change native event consumption. A zero
 * timeout result must leave zero bits so the native loop performs no save.
 * Stock events still return immediately; once started, use the stock wait. */
uint32_t z8_photo_lut_settings_wait(void *event,uint32_t mask,uint32_t *bits)
{
    persist_bootstrap();
    volatile Z8Persist *p=&Z8_STATE->persist;
    uint32_t result;
    if(Z8_STATE->magic==Z8_STATE_MAGIC && (p->authoritative || Z8_STATE->preview.power_paused) && p->deferred_save && !(read32(Z8_SETTINGS_LOADED)&0x100)) {
        if(bits) *bits=0;
        result=1;
    } else if((card_start_needed() && !card_worker_start()) ||
              (Z8_STATE->magic==Z8_STATE_MAGIC && !Z8_STATE->preview.power_paused &&
               (!p->authoritative || (p->deferred_save && (read32(Z8_SETTINGS_LOADED)&0x100))))) {
        if(bits) *bits=0;
        result=((uint32_t (*)(void *,uint32_t,uint32_t *,uint32_t))(uintptr_t)0x40d2bd64)(event,mask,bits,500);
    } else result=((uint32_t (*)(void *,uint32_t,uint32_t *))(uintptr_t)0x40d2bd04)(event,mask,bits);
    /* This call may have blocked before private boot. Decide the real tail
     * before the native caller dispatches even its first save event. */
    persist_bootstrap();
    if(Z8_STATE->magic!=Z8_STATE_MAGIC && bits && (*bits&0x1000) && !(read32(Z8_SETTINGS_LOADED)&0x100)) {
        /* Late boot always re-posts SAVE. Preserve preboot RAM changes and
         * the old private tail until that owner can read it. Native shutdown
         * no-save acknowledgments still pass through without serialization. */
        *bits&=~0x1000;
        ((void (*)(void *,uint32_t))(uintptr_t)0x40d2bcc8)(event,0x1000);
    }
    if(Z8_STATE->magic==Z8_STATE_MAGIC && bits) {
        if(read32(Z8_SETTINGS_LOADED)&0x100) { /* incoming SAVE acknowledges completion only */ }
        else if(p->authoritative || Z8_STATE->preview.power_paused) { *bits|=p->deferred_save;p->deferred_save=0; }
        else if(*bits&0x1000) {
            p->deferred_save|=0x1000;*bits&=~0x1000;
            ((void (*)(void *,uint32_t))(uintptr_t)0x40d2bcc8)(event,0x1000);
        }
    }
    return result;
}

/* Native section-15 preparation finished before late mapping (4101248c). */
uint32_t z8_photo_lut_settings_restored(uint64_t a,uint64_t b,uint64_t c,uint64_t d,uint64_t e)
{
    /* A native preparation cycle precedes late mapping on cold and warm
     * paths. Quarantine retained task handles while preserving its record. */
    if(Z8_STATE->magic==Z8_STATE_MAGIC) Z8_STATE->magic=Z8_PREPARE_MAGIC;
    return SETTINGS_REPLY(a,b,c,d,e);
}

/* PreparePhase can reset native settings before private boot, including on
 * retained warm RAM. Invalidate its old private candidate without touching
 * task pointers from the previous boot. The later bootstrap reads native
 * preparation status; runtime whole reset has its separate wrapper below. */
void z8_photo_lut_restore_defaults(void)
{
    SETTINGS_DEFAULTS();
    volatile Z8Persist *p=&Z8_STATE->persist;
    if((Z8_STATE->magic&~UINT64_C(1))==Z8_STATE_MAGIC) {
        Z8_STATE->magic=Z8_PREPARE_MAGIC;
        __atomic_store_n(&p->ready,0,__ATOMIC_RELEASE);
        __atomic_store_n(&p->authoritative,0,__ATOMIC_RELEASE);
    }
}

/* Runtime whole-settings reset: every bank gets the firmware default and
 * the active library returns to the built-in image. */
void z8_photo_lut_defaults(void)
{
    SETTINGS_DEFAULTS();
    volatile Z8LutState *s=Z8_STATE;
    if(s->magic!=Z8_STATE_MAGIC) return;
    volatile Z8Persist *p=&s->persist;
    record_lock();
    p->defaults=1;
    __atomic_store_n(&p->cancelled_request,__atomic_load_n(&s->card.request,__ATOMIC_ACQUIRE),__ATOMIC_RELEASE);
    __atomic_store_n(&s->card.unload,1,__ATOMIC_RELEASE);
    sequence_open();p->pending_mask=0;p->pending_sources=0;sequence_close();
    unsigned ready=__atomic_load_n(&p->ready,__ATOMIC_ACQUIRE);
    if(ready) {
        Z8BankRecord r;
        record_init(&r);
        for(unsigned bank=0;bank<Z8_BANKS;bank++) default_slot(&r.banks[bank]);
        record_publish(&r);
        own_all();
        p->source=Z8_SOURCE_DEFAULTS;
        __atomic_store_n(&p->authoritative,1,__ATOMIC_RELEASE);
    }
    record_unlock();
    /* The configuration is back on the built-in library: so is the menu. */
    card_wake();
    if(ready) { commit();preview_wake();z8_photo_lut_capture_refresh(); }
    else persist_decide(0);
}

static void section_runtime(uint64_t buffer,uint32_t bytes)
{
    /* Leave the section's last-transfer fields as the stock call set them. */
    uint64_t section=SECTION(Z8_BLOB_SECTION);
    if(!section) return;
    *(volatile uint64_t *)(uintptr_t)(section+0x10)=buffer;
    *(volatile uint32_t *)(uintptr_t)(section+0x18)=bytes;
}

/* Read the stored region of blob 16 into scratch: stock bytes, then the
 * record. Returns the native result. */
static uint32_t blob_read_region(uint8_t *scratch,uint64_t buffer,uint32_t bytes)
{
    uint32_t result=SECTION_READ(Z8_BLOB_SECTION,(uintptr_t)scratch,BLOB_TOTAL);
    section_runtime(buffer,bytes);
    return result;
}

/* PreparePhase restores section 15 and loads blob 16 before the image's
 * late private-boot hook. Its loaded flag proves the stock prefix is live;
 * read only the private tail into scratch, leaving all live native bytes
 * intact. A retained record follows warm native RAM, even if the native
 * restore-result byte still records an earlier fresh/default startup. */
static void persist_bootstrap(void)
{
    volatile Z8LutState *s=Z8_STATE;
    volatile Z8Persist *p=&s->persist;
    if(s->magic!=Z8_STATE_MAGIC || (p->ready && p->authoritative) || !(GROUP_LOADED(Z8_BLOB_GROUP)&1)) return;
    p->settings_restored=1;
    Z8BankRecord tail;
    const Z8BankRecord *stored=0;
    if(p->retained) p->blob=Z8_BLOB_SKIPPED;
    else {
        p->defaults|=!(read32(Z8_SETTINGS_LOADED)&1);
        uint8_t *scratch=blob_scratch();
        if(!scratch) { p->blob=Z8_BLOB_UNREAD;return; }
        p->blob_reads++;
        p->blob=region_state(blob_read_region(scratch,Z8_BLOB,Z8_BLOB_BYTES),scratch,&tail);
        if(p->blob==Z8_BLOB_VALID) stored=&tail;
        FREE(scratch);
    }
    persist_decide(stored);
    if(card_start_needed() && card_worker_start()) card_wake();
}

/* Blob-16 store in the native save producer (0x41012558): one region write
 * of the unchanged stock bytes followed by the record. A record not decided
 * from an actual read is never written over: the stored region is read first
 * and decided on (an unreadable region is lost, as for the native caller).
 * Without memory for that, nothing is written and the store reports failure. */
uint32_t z8_photo_lut_blob_write(uint32_t section,uint64_t buffer,uint32_t bytes)
{
    volatile Z8LutState *s=Z8_STATE;
    if(section!=Z8_BLOB_SECTION || buffer!=Z8_BLOB || bytes!=Z8_BLOB_BYTES)
        return SECTION_WRITE(section,buffer,bytes);
    /* A direct preboot save must not erase a prior tail with a stock-length
     * write. Late boot re-posts the native save with the same live prefix. */
    if(s->magic!=Z8_STATE_MAGIC) return 0;
    volatile Z8Persist *p=&s->persist;
    uint8_t *scratch=blob_scratch();
    if(!scratch) { p->refused_writes++;return 0; }
    if(!__atomic_load_n(&p->authoritative,__ATOMIC_ACQUIRE)) {
        Z8BankRecord tail;
        p->blob=region_state(blob_read_region(scratch,buffer,bytes),scratch,&tail);
        persist_decide(p->blob==Z8_BLOB_VALID?&tail:0);
        if(!__atomic_load_n(&p->authoritative,__ATOMIC_ACQUIRE)) { FREE(scratch);p->refused_writes++;return 0; }
    }
    copy_bytes(scratch,(const void *)(uintptr_t)buffer,Z8_BLOB_BYTES);
    Z8BankRecord r;
    record_read(&r);
    copy_bytes(scratch+Z8_BLOB_BYTES,&r,sizeof(r));
    uint32_t result=SECTION_WRITE(section,(uintptr_t)scratch,BLOB_TOTAL);
    section_runtime(buffer,bytes);
    FREE(scratch);
    p->blob_writes++;
    return result;
}

void z8_photo_lut_boot(void)
{
    volatile Z8LutState *s=Z8_STATE;
    uint64_t boots=0;
    Z8BankRecord retained;
    unsigned keep=0;
    if((s->magic&~UINT64_C(1))==Z8_STATE_MAGIC && s->version==4 && s->bytes==sizeof(*s)) {
        boots=s->boots;
        /* A validated private record follows retained native RAM. Native
         * preparation quarantines its old task handles before this hook. */
        copy_bytes(&retained,(const void *)(uintptr_t)&s->persist.record,sizeof(retained));
        keep=s->persist.ready && s->persist.authoritative && record_valid(&retained);
    }
    volatile uint8_t *p=(volatile uint8_t *)s;
    for(unsigned i=0;i<sizeof(*s);i++) p[i]=0;
    s->version=4;s->bytes=sizeof(*s);s->boots=boots+1;s->strength=256;
    s->error=z8_lut_validate_image(Z8_BUILTIN,Z8_LUT_BUNDLE_LIMIT);
    s->choice=0;
    s->ready=s->error==Z8_LUT_OK;
    if(keep) { copy_bytes((void *)(uintptr_t)&s->persist.record,&retained,sizeof(retained));s->persist.retained=1; }
    /* The heap is rebuilt at every boot: the card library is restored by the
     * card worker once native settings are back. */
    if(s->ready) menu_text(Z8_BUILTIN,CARD_RESTORING,0,0);
    s->magic=Z8_STATE_MAGIC;
    __atomic_thread_fence(__ATOMIC_RELEASE);
    /* Native PreparePhase and its settings event already exist at this late
     * mapping seam. Wake that task; its wait continuation restores our tail
     * before this event can serialize any native settings. */
    REQUEST_SAVE();
}

/* Copy the native source into the lease scratch and apply the capture's
 * look. The NEF/native source is only read; the encoder sees cleaned lines. */
static unsigned jpeg_work(const Z8Surface *f,uint8_t *source_y,uint8_t *source_uv,const Z8Look *look)
{
    invalidate(source_y,f->y_bytes);invalidate(source_uv,f->uv_bytes);
    invalidate(f->y,f->y_bytes+f->uv_bytes);
    copy_bytes(f->y,source_y,(uint32_t)f->y_bytes);copy_bytes(f->uv,source_uv,(uint32_t)f->uv_bytes);
    unsigned error=z8_lut_surface(f,look);
    clean(f->y,f->y_bytes+f->uv_bytes);
    return error;
}

/* Native encoder tasks share CPU1 with UI, MCU and service tasks of lower
 * priority; a full-resolution transform there starves them for seconds.
 * One dedicated task below every native C3.11 priority (2..40) performs the
 * CPU work on CPU3 using otherwise idle time, and is never stopped.
 *
 * Lifetime of a queued request: its scratch lies in the capture's pool0x38
 * output owner and its source in that capture's native buffers. The calling
 * encoder task stays inside the native image job until the request is DONE,
 * so output release and BSShutdown (which waits for the image job) cannot
 * precede it. The native mode transition (41046538..41046598) destroys a pool
 * only when 40cdafa0 reports it entirely free; a pool with this owner live is
 * marked busy and kept. Preview pause and power quiescing never touch jobs. */
#define JPEG_WORKER_CPU 3
#define JPEG_WORKER_PRIORITY 48
enum { JPEG_WORKER_ABSENT,JPEG_WORKER_CREATING,JPEG_WORKER_RUNNING };
enum { JOB_FREE,JOB_FILLING,JOB_QUEUED,JOB_RUNNING,JOB_DONE };

static void jpeg_worker(void *argument)
{
    PreviewWorker *worker=argument;
    volatile Z8JpegWorker *w=&Z8_STATE->jpeg;
    /* Sequences are contiguous from 1 per boot and each is published before
     * the next is assigned. Serving only the exact next one is therefore
     * strict publication order: observing any later request (acquire) makes
     * the earlier publication visible, so the rescan never waits for it. */
    uint64_t next=1;
    __atomic_store_n(&w->alive,1,__ATOMIC_RELEASE);
    for(;;) {
        volatile Z8JpegJob *job=0;
        unsigned later=0;
        for(unsigned i=0;i<Z8_JPEG_JOBS && !job;i++) {
            volatile Z8JpegJob *candidate=&w->jobs[i];
            if(__atomic_load_n(&candidate->state,__ATOMIC_ACQUIRE)!=JOB_QUEUED) continue;
            if(candidate->sequence==next) job=candidate;
            else later=1;
        }
        if(!job) {
            if(!later) EVENT_WAIT(worker->event,1,0,1000);
            continue;
        }
        next++;
        __atomic_store_n(&job->state,JOB_RUNNING,__ATOMIC_RELAXED);
        Z8Surface f;Z8Look look;
        copy_bytes(&f,(const void *)&job->surface,sizeof(f));
        copy_bytes(&look,(const void *)&job->look,sizeof(look));
        job->error=jpeg_work(&f,(uint8_t *)(uintptr_t)job->source_y,(uint8_t *)(uintptr_t)job->source_uv,&look);
        __atomic_fetch_add(&w->completed,1,__ATOMIC_RELAXED);
        __atomic_store_n(&job->state,JOB_DONE,__ATOMIC_RELEASE);
    }
}

/* Called while reserving a capture that will need a LUT JPEG transform; a
 * capture is admitted only with the worker running. Never sleeps: if another
 * task is starting it, or a step fails, the reservation fails into native
 * allocation retry. Completed steps (task block, wake event, created task)
 * stay recorded here and are reused by the next attempt, so a failure never
 * orphans resources. 40d2d5cc discards the OS start result; 401855e0 fails
 * only for an ISR caller, an id below one or a non-dormant task, all excluded
 * for this freshly created task started from task context. */
static int jpeg_worker_start(void)
{
    volatile Z8JpegWorker *w=&Z8_STATE->jpeg;
    uint32_t expected=JPEG_WORKER_ABSENT;
    if(__atomic_load_n(&w->worker_state,__ATOMIC_ACQUIRE)==JPEG_WORKER_RUNNING) return 1;
    if(!__atomic_compare_exchange_n(&w->worker_state,&expected,JPEG_WORKER_CREATING,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) return 0;
    PreviewWorker *worker=(PreviewWorker *)(uintptr_t)w->worker;
    if(!worker && (worker=ALLOC(sizeof(*worker),0))) {
        for(unsigned i=0;i<sizeof(*worker);i++) ((uint8_t *)worker)[i]=0;
        w->worker=(uintptr_t)worker;
    }
    if(worker && !w->event_ready) {
        EVENT_CREATE(worker->event,0,1,"PhotoJPG");
        w->event_ready=worker->event[0]>0;
    }
    if(worker && w->event_ready && !w->worker_id) {
        uint32_t parameters[7]={JPEG_WORKER_CPU,JPEG_WORKER_PRIORITY,16384,0,0,0,0};
        copy_bytes((char *)parameters+TASK_NAME,"PhotoJPG",8);
        TASK_CREATE(worker->task,jpeg_worker,parameters);
        if(*(int32_t *)worker>0) w->worker_id=*(uint32_t *)worker;
    }
    if(w->worker_id) {
        TASK_START(worker->task,worker);
        __atomic_store_n(&w->worker_state,JPEG_WORKER_RUNNING,__ATOMIC_RELEASE);
        return 1;
    }
    __atomic_fetch_add(&w->start_failures,1,__ATOMIC_RELAXED);
    __atomic_store_n(&w->worker_state,JPEG_WORKER_ABSENT,__ATOMIC_RELEASE);
    return 0;
}

/* The calling native task keeps its encoder lock and job ownership and
 * sleeps until its own request completes. Sequence assignment and
 * publication form one critical section, so the worker serves requests in
 * exactly their publication order. The lock waits by sleeping: equal-
 * priority encoders without time slicing would never let a spinning
 * contender's preempted holder resume. */
static unsigned jpeg_transform(const Z8Surface *f,uint8_t *source_y,uint8_t *source_uv,const Z8Look *look)
{
    volatile Z8JpegWorker *w=&Z8_STATE->jpeg;
    /* Every lease with transform scratch was admitted with a running worker,
     * which never stops; this guard is unreachable and never runs inline. */
    if(__atomic_load_n(&w->worker_state,__ATOMIC_ACQUIRE)!=JPEG_WORKER_RUNNING) return Z8_LUT_NO_WORKER;
    PreviewWorker *worker=(PreviewWorker *)(uintptr_t)w->worker;
    volatile Z8JpegJob *job=0;
    for(;;) {
        for(unsigned i=0;i<Z8_JPEG_JOBS && !job;i++) {
            uint32_t expected=JOB_FREE;
            if(__atomic_compare_exchange_n(&w->jobs[i].state,&expected,JOB_FILLING,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) job=&w->jobs[i];
        }
        if(job) break;
        __atomic_fetch_add(&w->waits,1,__ATOMIC_RELAXED);
        TASK_SLEEP(1);
    }
    copy_bytes((void *)&job->surface,f,sizeof(*f));
    copy_bytes((void *)&job->look,look,sizeof(*look));
    job->source_y=(uintptr_t)source_y;job->source_uv=(uintptr_t)source_uv;job->error=0;
    for(;;) {
        uint32_t unlocked=0;
        if(__atomic_compare_exchange_n(&w->submit_lock,&unlocked,1,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) break;
        __atomic_fetch_add(&w->waits,1,__ATOMIC_RELAXED);
        TASK_SLEEP(1);
    }
    job->sequence=++w->sequence;
    __atomic_store_n(&job->state,JOB_QUEUED,__ATOMIC_RELEASE);
    __atomic_store_n(&w->submit_lock,0,__ATOMIC_RELEASE);
    __atomic_fetch_add(&w->submitted,1,__ATOMIC_RELAXED);
    uint32_t depth=0;
    for(unsigned i=0;i<Z8_JPEG_JOBS;i++) {
        uint32_t state=__atomic_load_n(&w->jobs[i].state,__ATOMIC_ACQUIRE);
        depth+=state==JOB_QUEUED || state==JOB_RUNNING;
    }
    uint32_t peak=__atomic_load_n(&w->queue_peak,__ATOMIC_RELAXED);
    while(depth>peak && !__atomic_compare_exchange_n(&w->queue_peak,&peak,depth,0,__ATOMIC_RELAXED,__ATOMIC_RELAXED)) {}
    EVENT_SET(worker->event,1);
    while(__atomic_load_n(&job->state,__ATOMIC_ACQUIRE)!=JOB_DONE) {
        __atomic_fetch_add(&w->waits,1,__ATOMIC_RELAXED);
        TASK_SLEEP(1);
    }
    unsigned error=job->error;
    __atomic_store_n(&job->state,JOB_FREE,__ATOMIC_RELEASE);
    return error;
}

/* The native output constructor computes compressed segment capacities from
 * capture settings, independently of its pool allocation's actual capacity.
 * Reserve the LUT surface in that same owner BEFORE native image processing.
 * Allocation failure therefore follows 40ea9f44's native event wait/retry;
 * no extra memory is requested after the JPEG phase has been admitted.
 * Only the native output segments are serialized; this footer is never saved. */
#define JPEG_ENCODE ((unsigned (*)(uint32_t,uint64_t))(uintptr_t)0x40e82090)
#define JPEG_ENABLED ((unsigned (*)(uint64_t))(uintptr_t)0x4105903c)
#define JPEG_LEASE_MAGIC UINT64_C(0x3154504f4c385a4a)
#define JPEG_SURFACE_BYTES (8256u*5504u*2u)
#define JPEG_MEDIUM_BYTES (1920u*1080u*2u)
#define JPEG_SMALL_BYTES (640u*480u*2u)
#define JPEG_TINY_BYTES (192u*120u*2u)
#define JPEG_THUMB_BYTES 0x3600u
#define JPEG_STORAGE_BYTES (JPEG_SURFACE_BYTES+JPEG_MEDIUM_BYTES+JPEG_SMALL_BYTES+JPEG_TINY_BYTES+JPEG_THUMB_BYTES)
/* The lease latches the look at reservation: a private copy of its samples
 * (after the JPEG scratch, inside this capture's own output owner) plus its
 * identity and name. Every later JPEG step and its provenance use only this
 * snapshot, so a library change can never retarget an admitted capture. */
typedef struct {
    uint64_t magic,owner,base,capacity,scratch;
    uint32_t scratch_bytes,choice;
    uint64_t compressed_bytes,samples;      /* raw address of the sample copy, or 0 */
    uint32_t grid,identity,reserved,sample_bytes;
    uint8_t id[32];
    char name[64];
    uint8_t immutable_reserved[16];
    /* Keep mutable CPU ownership on a separate cache line. */
    uint64_t thumbnail[4];
    uint32_t locks[4],thumbnail_ready,applied,mutable_reserved[2];
} JpegLease;
#define JPEG_LEASE_IMMUTABLE 192u
_Static_assert(sizeof(JpegLease)==256 && offsetof(JpegLease,thumbnail)==JPEG_LEASE_IMMUTABLE,
               "JPEG lease: three immutable lines, then one CPU line");

static uint32_t look_bytes(const Z8LookBundle *b,uint32_t selected)
{
    return selected?(b->looks[selected-1].bytes+63u)&~63u:0;
}

static uint32_t jpeg_required_bytes(uint32_t bytes,uint64_t format,const Z8LookBundle *b,uint32_t selected,uint32_t *scratch)
{
    /* The capture-settings block may live in pool0x22's cached image RAM
     * (observed C3.11 address c40000008), not only the main metadata arena. */
    if((format&3) || !cpu_span(format,0x94)) return 0;
    Z8Look look;
    if(z8_lut_select(b,selected,256,&look)) return 0;
    *scratch=(!look.identity && look.strength && (JPEG_ENABLED(format)&1))?JPEG_STORAGE_BYTES+look_bytes(b,selected):0;
    uint64_t offset=((uint64_t)bytes+511)&~UINT64_C(511);
    uint64_t required=offset+*scratch+sizeof(JpegLease);
    return bytes && required<=UINT32_MAX?(uint32_t)required:0;
}

/* Larger than any photo output pool: the native forecast counts no photo,
 * which is how release is withheld while the selected look is not applied. */
#define OUTPUT_NO_ROOM 0x80000000u

uint32_t z8_photo_lut_output_budget(uint64_t settings,uint64_t format,uint32_t special)
{
    uint32_t bytes=((uint32_t (*)(uint64_t,uint64_t,uint32_t))(uintptr_t)0x4104886c)(settings,format,special);
    const Z8LookBundle *b=library_enter();
    if(!b) return bytes;
    uint32_t selected=0,scratch=0,required=0;
    unsigned admit=admitted(b,&selected);
    if(admit) required=jpeg_required_bytes(bytes,format,b,selected,&scratch);
    library_exit();
    /* Only the prospective pool0x38 photo count uses this wrapper. Keep
     * the native compressed layout and its zero-size unit queries intact. */
    if(!admit && bytes) { Z8_STATE->persist.refused_forecasts++;return OUTPUT_NO_ROOM; }
    return required?required:bytes;
}

uint64_t z8_photo_lut_output_alloc(uint32_t frame,uint32_t pool,uint32_t bytes,uint64_t configuration)
{
    if(configuration>UINT64_MAX-0x26c || pool!=0x38) return 0;
    /* During a library publication, and while the selected bank's look is
     * not applied, the native allocation wait/retry (40ea9f3c) runs again;
     * its cancel bit ends the capture without an image. */
    const Z8LookBundle *b=library_enter();
    if(!b) return 0;
    uint32_t selected=0,scratch=0,required=0;
    uint64_t owner=0;
    if(!admitted(b,&selected)) { Z8_STATE->persist.refused_allocations++;goto done; }
    required=jpeg_required_bytes(bytes,configuration+0x26c,b,selected,&scratch);
    if(!required) goto done;
    /* A transform reservation is admitted only with its worker running. */
    if(scratch && !jpeg_worker_start()) goto done;
    uint64_t offset=((uint64_t)bytes+511)&~UINT64_C(511);
    owner=POOL_ALLOC(frame,pool,required);
    if(!owner) goto done;
    if(!main_span(owner,0x18) || (owner&7)) { owner=0;goto done; }
    uint64_t descriptor=read64(owner+8);
    if(!main_span(descriptor,12) || (descriptor&7)) goto failed;
    uint64_t raw=read64(descriptor),capacity=read32(descriptor+8);
    /* Variable pools can clamp oversized requests or take a fallback unit.
     * A non-NULL handle is not evidence that the requested span exists. */
    if(capacity<required || ((raw|capacity)&511) ||
       !range(raw,capacity,UINT64_C(0x800000000),UINT64_C(0xa00000000))) goto failed;
    uint8_t *cpu=cpu_address(raw);
    if(!cpu || (uintptr_t)cpu!=raw+UINT64_C(0x400000000)) goto failed;
    JpegLease *lease=(JpegLease *)(cpu+capacity-sizeof(JpegLease));
    invalidate(lease,sizeof(*lease));
    *lease=(JpegLease){.magic=JPEG_LEASE_MAGIC,.owner=owner,.base=raw,.capacity=capacity,
        .scratch=raw+offset,.scratch_bytes=scratch,.choice=selected,.compressed_bytes=bytes};
    if(selected) {
        const Z8LookEntry *e=&b->looks[selected-1];
        lease->grid=e->grid;lease->identity=e->identity;
        copy_bytes(lease->id,e->id,sizeof(lease->id));copy_bytes(lease->name,e->name,sizeof(lease->name));
        if(scratch) {
            uint8_t *copy=cpu+offset+JPEG_STORAGE_BYTES;
            lease->samples=raw+offset+JPEG_STORAGE_BYTES;lease->sample_bytes=e->bytes;
            invalidate(copy,look_bytes(b,selected));
            copy_bytes(copy,(const uint8_t *)b+e->offset,e->bytes);
            clean(copy,look_bytes(b,selected));
        }
    }
    clean(lease,sizeof(*lease));
    goto done;
failed:
    POOL_FREE(owner);
    owner=0;
done:
    library_exit();
    return owner;
}

static unsigned jpeg_lease(uint64_t owner,JpegLease *lease,volatile JpegLease **storage)
{
    if(!main_span(owner,0xd68) || (owner&7)) return Z8_LUT_BAD_SPAN;
    uint64_t descriptor=read64(owner+8);
    if(!main_span(descriptor,12) || (descriptor&7)) return Z8_LUT_BAD_SPAN;
    uint64_t raw=read64(descriptor),capacity=read32(descriptor+8);
    if(capacity<sizeof(*lease) || ((raw|capacity)&511) ||
       !range(raw,capacity,UINT64_C(0x800000000),UINT64_C(0xa00000000))) return Z8_LUT_BAD_SPAN;
    uint8_t *cpu=cpu_address(raw);
    if(!cpu || (uintptr_t)cpu!=raw+UINT64_C(0x400000000)) return Z8_LUT_BAD_SPAN;
    const JpegLease *source=(const JpegLease *)(cpu+capacity-sizeof(*lease));
    /* The last line contains live CPU locks, never DMA data. */
    invalidate(source,JPEG_LEASE_IMMUTABLE);
    *lease=*source;
    uint32_t copy=(lease->sample_bytes+63u)&~63u;
    if(lease->magic!=JPEG_LEASE_MAGIC || lease->owner!=owner || lease->base!=raw ||
       lease->capacity!=capacity || !lease->compressed_bytes ||
       lease->compressed_bytes>capacity-sizeof(*lease) || lease->choice>Z8_LUT_MAX_LOOKS ||
       lease->scratch!=raw+((lease->compressed_bytes+511)&~UINT64_C(511)) ||
       !range(lease->scratch,(uint64_t)lease->scratch_bytes+sizeof(*lease),raw,raw+capacity))
        return Z8_LUT_BAD_SPAN;
    if(lease->scratch_bytes) {
        /* A transform lease carries a whole private look grid. */
        if(!lease->choice || lease->identity || lease->scratch_bytes!=JPEG_STORAGE_BYTES+copy ||
           (lease->grid!=17 && lease->grid!=33 && lease->grid!=65) ||
           lease->sample_bytes!=lease->grid*lease->grid*lease->grid*6 ||
           lease->samples!=lease->scratch+JPEG_STORAGE_BYTES) return Z8_LUT_BAD_SPAN;
    } else if(lease->samples || lease->sample_bytes) return Z8_LUT_BAD_SPAN;
    *storage=(volatile JpegLease *)source;
    return Z8_LUT_OK;
}

static unsigned lease_look(const JpegLease *lease,Z8Look *look)
{
    *look=(Z8Look){0,17,1,0};
    if(!lease->scratch_bytes) return Z8_LUT_OK;
    /* CPU-written through the cached alias and cleaned at reservation; no
     * hardware writes this private copy, so no line can be stale here. */
    uint8_t *samples=cpu_address(lease->samples);
    if(!samples) return Z8_LUT_BAD_SPAN;
    *look=(Z8Look){(const uint16_t *)samples,lease->grid,0,256};
    return Z8_LUT_OK;
}

static void jpeg_lock(volatile uint32_t *lock)
{
    for(;;) {
        uint32_t expected=0;
        if(__atomic_compare_exchange_n(lock,&expected,1,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) return;
        TASK_SLEEP(1);
    }
}

static void jpeg_error(unsigned kind,unsigned error)
{
    if(kind==0) {
        Z8_STATE->jpeg_error=error;
        __atomic_fetch_add(&Z8_STATE->jpeg_rejected,1,__ATOMIC_RELAXED);
    } else {
        Z8_STATE->jpeg_preview_error=error;
        __atomic_fetch_add(&Z8_STATE->jpeg_preview_rejected,1,__ATOMIC_RELAXED);
    }
}

static unsigned jpeg_encode_locked(uint32_t channel,uint64_t parameters,unsigned kind,
                                   const JpegLease *lease,const Z8Look *look,unsigned *applied)
{
    static const uint32_t offsets[]={0,JPEG_SURFACE_BYTES,JPEG_SURFACE_BYTES+JPEG_MEDIUM_BYTES,
        JPEG_SURFACE_BYTES+JPEG_MEDIUM_BYTES+JPEG_SMALL_BYTES};
    static const uint32_t capacities[]={JPEG_SURFACE_BYTES,JPEG_MEDIUM_BYTES,JPEG_SMALL_BYTES,JPEG_TINY_BYTES};
    volatile Z8LutState *s=Z8_STATE;
    unsigned error=Z8_LUT_BAD_SPAN;
    *applied=0;
    if(kind>3 || lease->scratch_bytes<JPEG_STORAGE_BYTES || !main_span(parameters,0x50) || (parameters&7)) goto rejected;
    uint64_t descriptor=read64(parameters+8);
    if(!main_span(descriptor,0x28) || (descriptor&7)) goto rejected;
    uint32_t format=read32(descriptor);
    if(format!=1 && format!=2) { error=Z8_LUT_BAD_GEOMETRY;goto rejected; }
    Z8Surface f={.y=(uint8_t *)(uintptr_t)read64(descriptor+24),.uv=(uint8_t *)(uintptr_t)read64(descriptor+32),
        .width=read16(descriptor+16),.height=read16(descriptor+18),
        .y_stride=read16(descriptor+8),.uv_stride=read16(descriptor+10),.uv_divisor=format==1?1u:2u};
    f.y_bytes=(uint64_t)f.y_stride*f.height;f.uv_bytes=(uint64_t)f.uv_stride*(f.height/f.uv_divisor);
    if(kind==0) { s->width=f.width;s->height=f.height;s->y=(uintptr_t)f.y;s->uv=(uintptr_t)f.uv; }
    uint64_t scratch=lease->scratch+offsets[kind],bytes=f.y_bytes+f.uv_bytes;
    uint64_t original_y=(uintptr_t)f.y,original_uv=(uintptr_t)f.uv;
    if(!range(original_y,f.y_bytes,UINT64_C(0x800000000),UINT64_C(0x900000000)) ||
       !range(original_uv,f.uv_bytes,UINT64_C(0x800000000),UINT64_C(0x900000000)) ||
       ((original_y|original_uv|f.y_bytes|f.uv_bytes)&63) || bytes>capacities[kind] ||
       (scratch<original_y+f.y_bytes && original_y<scratch+bytes) ||
       (scratch<original_uv+f.uv_bytes && original_uv<scratch+bytes)) goto rejected;
    Z8Look off={0,17,1,0};
    error=z8_lut_surface(&f,&off);
    if(error) goto rejected;
    uint8_t *source_y=cpu_address(original_y),*source_uv=cpu_address(original_uv);
    f.y=cpu_address(scratch);f.uv=f.y?f.y+f.y_bytes:0;
    if(!source_y || !source_uv || !f.y) { error=Z8_LUT_BAD_SPAN;goto rejected; }
    __atomic_fetch_add(&s->jpeg_busy,1,__ATOMIC_ACQ_REL);
    if(kind==0) s->jpeg_started=ticks();
    error=jpeg_transform(&f,source_y,source_uv,look);
    if(error) {
        if(kind==0) s->jpeg_finished=ticks();
        __atomic_fetch_sub(&s->jpeg_busy,1,__ATOMIC_RELEASE);
        goto rejected;
    }
    uint64_t private_descriptor[5];
    copy_bytes(private_descriptor,(const void *)(uintptr_t)descriptor,sizeof(private_descriptor));
    private_descriptor[3]=scratch;private_descriptor[4]=scratch+f.y_bytes;
    /* Native encoding retains params+0x20 for control updates until return.
     * Substitute only its source pointer, preserving native caller outputs. */
    *(uint64_t *)(uintptr_t)(parameters+8)=(uintptr_t)private_descriptor;
    unsigned result=JPEG_ENCODE(channel,parameters);
    *(uint64_t *)(uintptr_t)(parameters+8)=descriptor;
    /* 40e82090 returns 1 unless its encode start (40c9b648) failed; with a
     * result block at params+0x48 it also stores completion (state+0xbc==0)
     * in that block's first byte (40e8222c..40e82250). */
    uint64_t status=read64(parameters+0x48);
    unsigned encoded=result==1 && (!status || *(volatile const uint8_t *)(uintptr_t)status==1);
    if(kind==0) {
        s->jpeg_finished=ticks();
        __atomic_fetch_add(&s->jpeg_applied,1,__ATOMIC_RELAXED);
        __atomic_fetch_add(&s->jpeg_pixels,(uint64_t)f.width*f.height,__ATOMIC_RELAXED);
    } else {
        __atomic_fetch_add(&s->jpeg_previews_applied,1,__ATOMIC_RELAXED);
        __atomic_fetch_add(&s->jpeg_preview_pixels,(uint64_t)f.width*f.height,__ATOMIC_RELAXED);
    }
    __atomic_fetch_sub(&s->jpeg_busy,1,__ATOMIC_RELEASE);
    *applied=encoded;
    return result;
rejected:
    jpeg_error(kind,error);
    return JPEG_ENCODE(channel,parameters);
}

static unsigned jpeg_output(uint32_t channel,uint64_t parameters,uint64_t owner,unsigned kind)
{
    if(Z8_STATE->magic!=Z8_STATE_MAGIC || !Z8_STATE->ready) return JPEG_ENCODE(channel,parameters);
    JpegLease lease;volatile JpegLease *storage=0;
    unsigned error=jpeg_lease(owner,&lease,&storage);
    if(error) { jpeg_error(kind,error);return JPEG_ENCODE(channel,parameters); }
    if(kind==0) {
        __atomic_fetch_add(&Z8_STATE->jpeg_calls,1,__ATOMIC_RELAXED);
        Z8_STATE->jpeg_choice=lease.choice;Z8_STATE->jpeg_error=0;
    }
    Z8Look look;
    error=lease_look(&lease,&look);
    if(error) { jpeg_error(kind,error);return JPEG_ENCODE(channel,parameters); }
    if(look.identity || !look.strength) return JPEG_ENCODE(channel,parameters);
    jpeg_lock(&storage->locks[kind]);
    /* Which outputs were successfully encoded from the transformed surface:
     * provenance may describe an applied look only for these. A repeated
     * encode of the same output first withdraws its earlier success. */
    __atomic_fetch_and(&storage->applied,~(1u<<kind),__ATOMIC_ACQ_REL);
    unsigned applied,result=jpeg_encode_locked(channel,parameters,kind,&lease,&look,&applied);
    if(applied) __atomic_fetch_or(&storage->applied,1u<<kind,__ATOMIC_RELEASE);
    __atomic_store_n(&storage->locks[kind],0,__ATOMIC_RELEASE);
    return result;
}

unsigned z8_photo_lut_jpeg(uint32_t channel,uint64_t parameters)
{
    if(!main_span(parameters,0x50) || (parameters&7)) {
        jpeg_error(0,Z8_LUT_BAD_SPAN);return JPEG_ENCODE(channel,parameters);
    }
    /* 40eaeb24..34 builds this exact owner-relative output descriptor. */
    return jpeg_output(channel,parameters,read64(parameters+0x10)-0x38,0);
}

static unsigned jpeg_preview(uint32_t channel,uint64_t parameters,uint64_t capture,uint64_t selector,unsigned kind)
{
    if(selector!=0) return JPEG_ENCODE(channel,parameters);
    if(!main_span(capture,0x6b0) || !main_span(parameters,0x50) || (parameters&7)) {
        jpeg_error(kind,Z8_LUT_BAD_SPAN);return JPEG_ENCODE(channel,parameters);
    }
    uint64_t owner=read64(capture+0x6a8),offset=kind==1?0xc0:0xa0;
    if(read64(parameters+0x10)!=owner+offset) {
        jpeg_error(kind,Z8_LUT_BAD_SPAN);return JPEG_ENCODE(channel,parameters);
    }
    return jpeg_output(channel,parameters,owner,kind);
}

unsigned z8_photo_lut_medium(uint32_t channel,uint64_t parameters,uint64_t capture,uint64_t selector)
{ return jpeg_preview(channel,parameters,capture,selector,1); }
unsigned z8_photo_lut_small(uint32_t channel,uint64_t parameters,uint64_t capture,uint64_t selector)
{ return jpeg_preview(channel,parameters,capture,selector,2); }

unsigned z8_photo_lut_thumbnail(uint32_t channel,uint64_t parameters)
{
    if(!main_span(parameters,0x50) || (parameters&7)) {
        jpeg_error(3,Z8_LUT_BAD_SPAN);return JPEG_ENCODE(channel,parameters);
    }
    JpegLease lease;volatile JpegLease *storage=0;
    unsigned error=jpeg_lease(read64(parameters+0x10)-0xe0,&lease,&storage);
    if(error) { jpeg_error(3,error);return JPEG_ENCODE(channel,parameters); }
    Z8Look look;
    error=lease_look(&lease,&look);
    if(error) { jpeg_error(3,error);return JPEG_ENCODE(channel,parameters); }
    if(look.identity || !look.strength) return JPEG_ENCODE(channel,parameters);
    jpeg_lock(&storage->locks[3]);
    __atomic_store_n(&storage->thumbnail_ready,0,__ATOMIC_RELEASE);
    __atomic_fetch_and(&storage->applied,~(1u<<3),__ATOMIC_ACQ_REL);
    uint64_t private_parameters[10],private_result[12]={0};
    copy_bytes(private_parameters,(const void *)(uintptr_t)parameters,sizeof(private_parameters));
    copy_bytes((void *)storage->thumbnail,(const void *)(uintptr_t)read64(parameters+0x10),sizeof(storage->thumbnail));
    storage->thumbnail[1]=lease.scratch+JPEG_STORAGE_BYTES-JPEG_THUMB_BYTES;
    storage->thumbnail[2]=0;
    private_parameters[2]=(uintptr_t)storage->thumbnail;
    private_parameters[3]=(private_parameters[3]&UINT64_C(0xffffffff00000000))|JPEG_THUMB_BYTES;
    private_parameters[9]=(uintptr_t)private_result;
    /* Preserve the common native thumbnail for NEF. The JPEG duplicate gets
     * its own result block and never overwrites the original control output. */
    unsigned result=JPEG_ENCODE(channel,parameters),applied=0;
    unsigned private_ok=jpeg_encode_locked(channel,(uintptr_t)private_parameters,3,&lease,&look,&applied);
    uint32_t bytes=(uint32_t)storage->thumbnail[2];
    if(private_ok && applied && bytes && bytes<=JPEG_THUMB_BYTES) {
        __atomic_fetch_or(&storage->applied,1u<<3,__ATOMIC_RELEASE);
        __atomic_store_n(&storage->thumbnail_ready,1,__ATOMIC_RELEASE);
    }
    else jpeg_error(3,Z8_LUT_BAD_SPAN);
    clean((const void *)storage->thumbnail,64);
    __atomic_store_n(&storage->locks[3],0,__ATOMIC_RELEASE);
    return result;
}

unsigned z8_photo_lut_file_graph(uint32_t file,uint64_t capture,uint64_t owner,uint64_t header,uint64_t graph)
{
    unsigned kind=((unsigned (*)(uint32_t,uint64_t,uint64_t,uint64_t,uint64_t))(uintptr_t)0x40eb17e0)
        (file,capture,owner,header,graph);
    if(kind!=0 || ((file&15)!=0 && (file&15)!=4)) return kind;
    JpegLease lease;volatile JpegLease *storage=0;
    unsigned error=jpeg_lease(owner,&lease,&storage);
    if(error) { jpeg_error(3,error);return kind; }
    if(!lease.scratch_bytes) return kind;
    if(!cpu_span(graph,0x2d8) || (graph&7) || !__atomic_load_n(&storage->thumbnail_ready,__ATOMIC_ACQUIRE)) {
        jpeg_error(3,Z8_LUT_BAD_SPAN);return kind;
    }
    /* JPEG header generation, EXIF length, catalog and async preview writer
     * all follow this per-file pointer. NEF's graph and shared owner stay
     * original; the descriptor and bytes live until native output release. */
    *(uint64_t *)(uintptr_t)(graph+0x18)=(uintptr_t)storage->thumbnail;
    return kind;
}

/* JPEG APP1 XMP provenance (metadata-native NATIVE-XMP.md sections 1, 2, 10, 11).
 * The native composer writes the whole fixed-size packet; afterwards one
 * description is added in place, consuming padding, so the packet, APP1 and
 * every Exif offset keep their sizes. Only the per-file header context, its
 * graph and the capture's own lease are consulted; no global state. */
#define XMP_COMPOSE ((uint32_t (*)(uint32_t,uint32_t,uint64_t,uint32_t,uint64_t))(uintptr_t)0x406e3698)
/* Native file-id class: low nibble of the packed id, F -> E (leaf). */
#define FILE_CLASS ((uint32_t (*)(uint32_t))(uintptr_t)0x4086cd48)
static uint8_t read8(uint64_t p) { return *(volatile const uint8_t *)(uintptr_t)p; }

/* The header builder writes the packet through 0x41186718's translation of
 * its header resource (0x406c32bc..c8; 0x406c40a0..b0 for the second call):
 * raw image RAM 0x800000000..0xa00000000 becomes its cached alias, raw +
 * 0x400000000, the same window jpeg_lease admits. A General-heap buffer is
 * returned unchanged. */
static int packet_span(uint64_t p,uint64_t n)
{ return main_span(p,n) || range(p,n,UINT64_C(0xc00000000),UINT64_C(0xe00000000)); }

static uint32_t xmp_skip(unsigned gate,uint32_t result)
{
    __atomic_fetch_add(&Z8_STATE->xmp_skipped[gate],1,__ATOMIC_RELAXED);
    return result;
}

uint32_t z8_photo_lut_xmp(uint32_t kind,uint32_t file,uint64_t settings,uint32_t size,uint64_t packet,uint64_t header)
{
    uint32_t result=XMP_COMPOSE(kind,file,settings,size,packet);
    volatile Z8LutState *s=Z8_STATE;
    /* Without initialized state nothing, not even telemetry, is written. */
    if(s->magic!=Z8_STATE_MAGIC) return result;
    __atomic_fetch_add(&s->xmp_calls,1,__ATOMIC_RELAXED);
    s->xmp_last_header=header;s->xmp_last_packet=packet;s->xmp_last_kind=kind;s->xmp_last_file=file;
    s->xmp_last_graph=0;s->xmp_last_lease=~0u;s->xmp_last_choice=0;s->xmp_last_applied=0;
    if(!s->ready) return xmp_skip(Z8_XMP_NOT_READY,result);
    /* File ids are packed (0x4086cd04: bits 29..30, directory at 19, sequence
     * at 5, type in the low nibble). The graph builder keeps the full id at
     * graph+0x48 (0x40eb180c) and dispatches on its class 0x4086cd48 (low
     * nibble, F -> E); classes 0 and 4 are JPEG files, like file_graph's test.
     * Both callers then hand the header builder the graph return as its
     * dispatch (0x40eb1788..17b0, 0x40eb1dc4..1dec): a JPEG graph returns
     * settings+0x130 ? 4 : 0 (0x40eb1b0c..b24) independently of the class;
     * table 0x440c6de8 sends both to this APP1 arm, which passes composer kind
     * (dispatch==0 ? 0 : 4) (0x406c328c..a4). NEF and other files stay native. */
    uint32_t class=FILE_CLASS(file);
    s->xmp_last_class=class;
    if((class!=0 && class!=4) || (kind!=0 && kind!=4)) return xmp_skip(Z8_XMP_NOT_JPEG,result);
    if(!cpu_span(header,0x18) || (header&7)) return xmp_skip(Z8_XMP_HEADER,result);
    uint64_t graph=read64(header+0x10);
    s->xmp_last_graph=graph;
    if(graph!=header-0x48 || !cpu_span(graph,0x50)) return xmp_skip(Z8_XMP_HEADER,result);
    /* The alternate kind0 arm (graph+0x4c = 1, graph+0x00 = owner+0x80) is
     * not an output the LUT hooks touch. */
    if(read8(graph+0x4c)!=0 || read32(graph+0x48)!=file) return xmp_skip(Z8_XMP_BRANCH,result);
    JpegLease lease;volatile JpegLease *storage=0;
    unsigned leased=jpeg_lease(read64(graph)-0x38,&lease,&storage);
    s->xmp_last_lease=leased;
    if(leased) return xmp_skip(Z8_XMP_LEASE,result);
    s->xmp_last_choice=lease.choice;
    if(!lease.choice) return xmp_skip(Z8_XMP_OFF,result);
    if(!packet_span(packet,size)) return xmp_skip(Z8_XMP_PACKET,result);
    uint32_t applied=__atomic_load_n(&storage->applied,__ATOMIC_ACQUIRE);
    s->xmp_last_applied=applied;
    /* A real look is claimed only for a successful transformed main encode;
     * a no-op look is recorded as such. Anything else writes nothing. */
    if(!lease.identity && !(applied&1)) return xmp_skip(Z8_XMP_NOT_APPLIED,result);
    Z8LutProvenance record;
    for(unsigned i=0;i<sizeof(record);i++) ((uint8_t *)&record)[i]=0;
    copy_bytes(record.name,lease.name,sizeof(record.name));
    copy_bytes(record.id,lease.id,sizeof(record.id));
    record.grid=lease.grid;record.strength=256;record.identity=lease.identity;
    uint32_t appended=0;
    unsigned error=z8_meta_append((char *)(uintptr_t)packet,size,&record,&appended);
    if(error) { s->xmp_error=error;__atomic_fetch_add(&s->xmp_refused,1,__ATOMIC_RELAXED); }
    else __atomic_fetch_add(&s->xmp_appended,1,__ATOMIC_RELAXED);
    return result;
}

#define ENTRY_GUARD ((int (*)(void *))(uintptr_t)0x403ae0f8)
#define SET_CURSOR ((void (*)(void *,uint32_t))(uintptr_t)0x403b0048)
#define SET_MARK ((int (*)(void *,uint32_t,uint32_t))(uintptr_t)0x403b00b8)
#define PHOTO_REFRESH ((int (*)(void *))(uintptr_t)0x405d9808)
#define CHILD ((void *(*)(void *,uint32_t,uint32_t))(uintptr_t)0x4050a6f0)
#define CONTENT ((void (*)(void *,uint32_t))(uintptr_t)0x403f645c)
#define BIND ((int (*)(void *,uint32_t,uint32_t))(uintptr_t)0x4031e178)
/* ---- menu text ------------------------------------------------------- */
/* Append UTF-8 (validated names or the fixed texts above) as UTF-16 at a
 * unit position of a block, through the writable alias of the image text. */
static unsigned text_append(uint64_t block,unsigned n,unsigned limit,const char *utf8,uint32_t count,uint32_t slots)
{
    volatile uint16_t *out=(volatile uint16_t *)(uintptr_t)(block+Z8_TEXT_ALIAS);
    for(const uint8_t *p=(const uint8_t *)utf8;*p && n+2<=limit;) {
        uint32_t c=*p++;
        if(c==1) { out[n++]=(uint16_t)('0'+count%10);continue; }
        if(c==2) {
            if((slots&1) && n<limit) out[n++]='1';
            if(slots==3 && n<limit) out[n++]='+';
            if((slots&2) && n<limit) out[n++]='2';
            continue;
        }
        unsigned extra=c>=0xf0?3:c>=0xe0?2:c>=0xc0?1:0;
        if(extra) c&=0x3fu>>extra;
        for(;extra && (*p&0xc0)==0x80;extra--) c=c<<6|(*p++&0x3fu);
        if(c>=0x10000) { c-=0x10000;out[n++]=(uint16_t)(0xd800|c>>10);out[n++]=(uint16_t)(0xdc00|(c&0x3ff)); }
        else out[n++]=(uint16_t)c;
    }
    return n;
}

/* Rewrite the card-load row and the eight look rows for every language;
 * rows past the library keep a "-" placeholder and are hidden on entry.
 * Called at boot and from the GUI task, never while a menu is drawn. */
static void menu_text(const Z8LookBundle *b,uint32_t status,uint32_t count,uint32_t slots)
{
    static const uint8_t representative[3]={0,8,9};
    for(unsigned group=0;group<3;group++) {
        /* Title, help and Off stay; rewrite from the card-load row start. */
        uint64_t blob=read64(Z8_LANGUAGE_BLOBS+8*representative[group]);
        const volatile int32_t *table=(const volatile int32_t *)(uintptr_t)read64(Z8_LANGUAGE_DIRECTORY+8*representative[group]);
        uint64_t start=blob+(int64_t)table[Z8_STRING_FIRST+3];
        uint64_t end=Z8_TEXT_BLOCKS_VA+(group+1)*Z8_TEXT_BLOCK_BYTES;
        if(start<end-Z8_TEXT_BLOCK_BYTES || start>=end || (start&1)) continue;
        unsigned limit=(unsigned)(end-start)/2;
        uint32_t at[2+Z8_LUT_MAX_LOOKS];
        unsigned n=text_append(start,0,limit,card_text[status<CARD_STATES?status:CARD_BUSY][group],count,slots);
        for(unsigned i=0;i<Z8_LUT_MAX_LOOKS;i++) {
            at[1+i]=n;
            /* "1. Name": the position tells similar names apart when a long
             * name is clipped; library, provenance and exports keep the name. */
            if(i<b->count) {
                static const char number[]="1. ";
                char prefix[sizeof(number)];
                copy_bytes(prefix,number,sizeof(number));prefix[0]=(char)('1'+i);
                n=text_append(start,n,limit,prefix,0,0);
            }
            n=text_append(start,n,limit,i<b->count?b->looks[i].name:"-",0,0);
        }
        at[0]=0;at[1+Z8_LUT_MAX_LOOKS]=n;
        for(unsigned language=0;language<Z8_LANGUAGES;language++) {
            if((language==8?1u:language==9?2u:0u)!=group) continue;
            uint64_t table_va=read64(Z8_LANGUAGE_DIRECTORY+8*language),own_blob=read64(Z8_LANGUAGE_BLOBS+8*language);
            volatile int32_t *entry=(volatile int32_t *)(uintptr_t)(table_va+Z8_TEXT_ALIAS+4ull*(Z8_STRING_FIRST+3));
            for(unsigned k=0;k<2+Z8_LUT_MAX_LOOKS;k++) entry[k]=(int32_t)(start+2ull*at[k]-own_blob);
        }
    }
}

/* ---- card library -------------------------------------------------- */
/* Pinned C3.11 file interface, as used by the pixel-shift and pre-capture
 * card workers: paths are "A:\..." (slot 1, CFexpress) and "B:\..." (slot 2,
 * SD); open(path,0,0x20); seek(fd,offset,whence,&position). */
#define FS_OPEN ((int (*)(const char *,uint32_t,uint32_t))(uintptr_t)0x402daa4c)
#define FS_CLOSE ((int (*)(int))(uintptr_t)0x402daaac)
#define FS_READ ((int (*)(int,void *,uint32_t,uint32_t *))(uintptr_t)0x402dab4c)
#define FS_SEEK ((int (*)(int,uint32_t,uint32_t,uint32_t *))(uintptr_t)0x402dac24)
#define FS_EXISTS ((int (*)(const char *))(uintptr_t)0x402db1ac)
#define CACHE_FLUSH ((void (*)(const void *,uint32_t))(uintptr_t)0x40d2b9c8)
#define CARD_WORKER_CPU 3
#define CARD_WORKER_PRIORITY 47
#define CARD_WAIT_POLLS 300u
#define CARD_WAIT_MS 10u
#define CARD_CHUNK 0x40000u
#define CARD_IDLE_MS 500u
enum { FILE_MISSING,FILE_VALID,FILE_INVALID,FILE_READ,FILE_MEMORY };

static int powering_down(void) { return __atomic_load_n(&Z8_STATE->preview.power_paused,__ATOMIC_ACQUIRE)!=0; }

static unsigned card_read(char drive,uint8_t **found)
{
    char path[]="A:\\Z8LUTLIB.BIN";
    path[0]=drive;*found=0;
    if(FS_EXISTS(path)<0) return FILE_MISSING;
    int fd=FS_OPEN(path,0,0x20);
    if(fd<0) return FILE_READ;
    unsigned result=FILE_READ;
    uint32_t end=UINT32_MAX,start=UINT32_MAX,count=0;
    uint8_t *memory=0;
    FS_SEEK(fd,0,2,&end);FS_SEEK(fd,0,0,&start);
    if(start!=0 || end>=0x80000000u) goto done;
    result=FILE_INVALID;
    if(end<sizeof(Z8LookBundle)) goto done;
    uint32_t limit=end<Z8_LUT_BUNDLE_LIMIT?end:Z8_LUT_BUNDLE_LIMIT;
    memory=ALLOC(limit,0);
    if(!memory || ((uintptr_t)memory&7)) { result=FILE_MEMORY;goto done; }
    /* No dirty line may be written back over the transfer, and no stale
     * line may be read after it. */
    CACHE_FLUSH(memory,limit);
    int error=FS_READ(fd,memory,64,&count);
    CACHE_FLUSH(memory,64);
    if(error || count!=64) { result=FILE_READ;goto done; }
    const Z8LookBundle *b=(const Z8LookBundle *)memory;
    uint32_t bytes=b->bytes;
    /* A declared bundle beyond the file is truncated; beyond 2 MiB, oversized. */
    if(b->magic!=Z8_LUT_MAGIC || bytes<sizeof(*b) || bytes>limit) goto done;
    /* Bounded transfers: power-down stops the read between chunks. */
    for(uint32_t at=64;at<bytes;at+=count) {
        uint32_t chunk=bytes-at<CARD_CHUNK?bytes-at:CARD_CHUNK;
        if(powering_down()) { result=FILE_READ;goto done; }
        error=FS_READ(fd,memory+at,chunk,&count);
        CACHE_FLUSH(memory+at,chunk);
        if(error || count!=chunk) { result=FILE_READ;goto done; }
    }
    if(z8_lut_validate(b,bytes)!=Z8_LUT_OK) goto done;
    if(bytes<limit) {
        uint8_t *exact=ALLOC(bytes,0);
        if(exact && !((uintptr_t)exact&7)) { copy_bytes(exact,memory,bytes);FREE(memory);memory=exact; }
        else if(exact) FREE(exact);
    }
    result=FILE_VALID;*found=memory;memory=0;
done:
    if(memory) FREE(memory);
    FS_CLOSE(fd);
    return result;
}

/* Both cards are read. A present file that is invalid or unreadable blocks
 * the load even when the other card's file is valid; two valid files must
 * be identical bundles. Nothing here changes the active library. */
static uint8_t *card_load(uint32_t *status,uint32_t *slots)
{
    uint8_t *found[2];unsigned result[2];
    for(unsigned slot=0;slot<2;slot++) result[slot]=card_read((char)('A'+slot),&found[slot]);
    *status=CARD_NO_FILE;*slots=0;
    for(unsigned slot=2;slot--;) {
        if(result[slot]==FILE_MEMORY) *status=CARD_MEMORY;
        else if(result[slot]==FILE_READ) { *status=CARD_READ;*slots=slot+1; }
        else if(result[slot]==FILE_INVALID) { *status=CARD_INVALID;*slots=slot+1; }
    }
    uint8_t *chosen=0;
    if(*status==CARD_NO_FILE) {
        const Z8LookBundle *a=(const Z8LookBundle *)found[0],*b=(const Z8LookBundle *)found[1];
        if(a && b) {
            if(a->bytes==b->bytes && same_bytes(found[0],found[1],a->bytes)) { chosen=found[0];*slots=3; }
            else *status=CARD_CONFLICT;
        } else if(a) { chosen=found[0];*slots=1; }
        else if(b) { chosen=found[1];*slots=2; }
        if(chosen) *status=CARD_LOADED;
    }
    for(unsigned slot=0;slot<2;slot++) if(found[slot] && found[slot]!=chosen) FREE(found[slot]);
    return chosen;
}

/* Card worker only, so publications never overlap. Order: preview quiesce
 * (pause, cancel, wait, release cache, discard pending) -> publishing -> no
 * counted reader -> swap -> release -> resume. Banks name their looks by full
 * transform identity, so nothing is remapped and nothing is saved. An
 * identical bundle is not published again; 0 publishes the built-in library. */
static int card_publish(uint8_t *next)
{
    volatile Z8LutState *s=Z8_STATE;
    volatile Z8CardLibrary *c=&s->card;
    const Z8LookBundle *previous=library_active(),*bundle=next?(const Z8LookBundle *)next:Z8_BUILTIN;
    if(powering_down() || previous==bundle || (previous!=Z8_BUILTIN && next && previous->bytes==bundle->bytes &&
                                               same_bytes((const uint8_t *)previous,next,bundle->bytes))) {
        if(next) FREE(next);
        return !powering_down();
    }
    z8_photo_lut_mode_enter();
    __atomic_store_n(&c->publishing,1,__ATOMIC_SEQ_CST);
    while(__atomic_load_n(&c->readers,__ATOMIC_SEQ_CST)) TASK_SLEEP(1);
    __atomic_store_n(&c->active,(uintptr_t)next,__ATOMIC_RELEASE);
    __atomic_store_n(&c->publishing,0,__ATOMIC_SEQ_CST);
    if(previous!=Z8_BUILTIN) FREE((void *)(uintptr_t)previous);
    __atomic_fetch_add(&s->persist.library_generation,1,__ATOMIC_RELEASE);
    c->publications++;
    z8_photo_lut_mode_exit();
    z8_photo_lut_capture_refresh();
    return 1;
}

/* The configuration names the card library, and either no card library is
 * published yet or the selected bank's look is missing from it. Another
 * bank's missing look is looked for when that bank is selected (the worker
 * re-checks at every wake), so an unused bank causes no card I/O. A
 * built-in configuration never reads the card by itself. */
static int restore_needed(void)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    if(!__atomic_load_n(&p->ready,__ATOMIC_ACQUIRE) || p->record.library!=Z8_LIBRARY_CARD) return 0;
    if(!__atomic_load_n(&Z8_STATE->card.active,__ATOMIC_ACQUIRE)) return 1;
    uint32_t index;
    const Z8LookBundle *b=library_wait();
    uint32_t state=evaluate(b,&index);
    library_exit();
    return state==Z8_EFFECTIVE_RESTORING || state==Z8_EFFECTIVE_UNAVAILABLE;
}

/* One automatic restore attempt: read both cards and publish a valid
 * library. Desired looks are never touched; failures only explain why a
 * look is unavailable. */
static void restore_attempt(void)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    __atomic_store_n(&p->restore,Z8_RESTORE_READING,__ATOMIC_RELEASE);
    uint32_t status=CARD_BUSY,slots=0;
    uint8_t *chosen=powering_down()?0:card_load(&status,&slots);
    uint32_t count=chosen?((const Z8LookBundle *)chosen)->count:0;
    if(chosen && __atomic_load_n(&Z8_STATE->card.unload,__ATOMIC_ACQUIRE)) { FREE(chosen);chosen=0;status=CARD_BUSY; }
    if(chosen && !card_publish(chosen)) status=CARD_BUSY;
    if(status==CARD_LOADED) {
        volatile Z8CardLibrary *c=&Z8_STATE->card;
        c->status=status;c->detail=slots;c->loaded=count;
    }
    p->reason=status;p->detail=slots;p->retries++;
    __atomic_store_n(&p->restore,Z8_RESTORE_DONE,__ATOMIC_RELEASE);
}

static void card_worker(void *argument)
{
    PreviewWorker *worker=argument;
    volatile Z8LutState *s=Z8_STATE;
    volatile Z8CardLibrary *c=&s->card;
    volatile Z8Persist *p=&s->persist;
    /* Automatic retries while a wanted look is missing: 0.5, 1, 2, 4, 8 s,
     * then every 10 s; a menu visit retries at once. */
    static const uint32_t backoff[]={500,1000,2000,4000,8000,10000};
    unsigned attempts=0,unread=0;
    /* Time is counted in idle waits at their bound: an early wake only
     * brings a retry forward. */
    uint32_t clock=0,last=0,saved=0;
    for(;;) {
        /* A reset preceding a newer explicit Load is applied first. A reset
         * during a read cancels its staged result below, before publication. */
        if(__atomic_exchange_n(&c->unload,0,__ATOMIC_ACQ_REL)) {
            card_publish(0);
            attempts=0;
            continue;
        }
        uint32_t serial=__atomic_load_n(&c->request,__ATOMIC_ACQUIRE);
        if(serial!=__atomic_load_n(&c->result,__ATOMIC_ACQUIRE)) {
            /* An explicit Load: published whatever the menu does meanwhile. */
            uint32_t status=CARD_BUSY,slots=0,count=0;
            uint8_t *chosen=powering_down() || request_cancelled(serial)?0:card_load(&status,&slots);
            if(chosen && request_cancelled(serial)) { FREE(chosen);chosen=0;status=CARD_BUSY; }
            if(chosen) {
                count=((const Z8LookBundle *)chosen)->count;
                if(!card_publish(chosen)) status=CARD_BUSY;
                else desire_library(Z8_LIBRARY_CARD,serial);
            }
            c->status=status;c->detail=slots;c->loaded=status==CARD_LOADED?count:0;
            if(status!=CARD_LOADED) c->refusals++;
            /* A card read happened: a look still missing is now unavailable. */
            p->reason=status;p->detail=slots;
            __atomic_store_n(&p->restore,Z8_RESTORE_DONE,__ATOMIC_RELEASE);
            __atomic_store_n(&c->result,serial,__ATOMIC_RELEASE);
            continue;
        }
        /* The record could not be read (no memory): ask the native settings
         * task to save, whose blob-16 store reads the region first. */
        if(!powering_down() && __atomic_load_n(&p->ready,__ATOMIC_ACQUIRE) &&
           !__atomic_load_n(&p->authoritative,__ATOMIC_ACQUIRE) && (GROUP_LOADED(Z8_BLOB_GROUP)&1) &&
           (p->blob==Z8_BLOB_UNREAD || p->blob==Z8_BLOB_SKIPPED) && (!unread || clock-saved>=backoff[unread-1])) {
            REQUEST_SAVE();
            saved=clock;
            if(unread<6) unread++;
        }
        unsigned due=0;
        if(__atomic_load_n(&p->ready,__ATOMIC_ACQUIRE) && !powering_down()) {
            if(__atomic_exchange_n(&c->retry_now,0,__ATOMIC_ACQ_REL)) due=restore_needed();
            else if(p->restore==Z8_RESTORE_WAITING) due=restore_needed();
            else if(restore_needed()) {
                uint32_t delay=backoff[attempts<5?attempts:5];
                due=clock-last>=delay;
            } else attempts=0;
        }
        if(due) {
            restore_attempt();
            last=clock;
            if(attempts<5) attempts++;
            continue;
        }
        EVENT_WAIT(worker->event,1,0,CARD_IDLE_MS);
        clock+=CARD_IDLE_MS;
    }
}

/* Started by the post-mapping settings task or explicit GUI import. */
static int card_worker_start(void)
{
    volatile Z8CardLibrary *c=&Z8_STATE->card;
    uint32_t expected=0;
    if(!__atomic_compare_exchange_n(&c->worker_state,&expected,2,0,__ATOMIC_ACQ_REL,__ATOMIC_ACQUIRE))
        return expected==1;
    PreviewWorker *worker=(PreviewWorker *)(uintptr_t)c->worker;
    if(!worker && (worker=ALLOC(sizeof(*worker),0))) {
        for(unsigned i=0;i<sizeof(*worker);i++) ((uint8_t *)worker)[i]=0;
        c->worker=(uintptr_t)worker;
    }
    if(worker && !c->event_ready) {
        EVENT_CREATE(worker->event,0,1,"PhotoCRD");
        c->event_ready=worker->event[0]>0;
    }
    if(worker && c->event_ready && !c->worker_id) {
        uint32_t parameters[7]={CARD_WORKER_CPU,CARD_WORKER_PRIORITY,16384,0,0,0,0};
        copy_bytes((char *)parameters+TASK_NAME,"PhotoCRD",8);
        TASK_CREATE(worker->task,card_worker,parameters);
        if(*(int32_t *)worker>0) c->worker_id=*(uint32_t *)worker;
    }
    if(!c->worker_id) { __atomic_store_n(&c->worker_state,0,__ATOMIC_RELEASE);return 0; }
    TASK_START(worker->task,worker);
    __atomic_store_n(&c->worker_state,1,__ATOMIC_RELEASE);
    return 1;
}

static void card_wake(void)
{
    volatile Z8CardLibrary *c=&Z8_STATE->card;
    if(__atomic_load_n(&c->worker_state,__ATOMIC_ACQUIRE)==1)
        EVENT_SET(((PreviewWorker *)(uintptr_t)c->worker)->event,1);
}

static void menu_show(void);

/* GUI task: an explicit Load. The worker reads and publishes; the menu
 * waits a bounded time for the result to show it. */
static void card_request(void)
{
    volatile Z8CardLibrary *c=&Z8_STATE->card;
    c->attempts++;
    if(!card_worker_start()) { c->status=CARD_MEMORY;c->detail=0;c->loaded=0;c->refusals++;return; }
    uint32_t serial=c->request+1;
    if(!serial) serial=1;
    __atomic_store_n(&c->request,serial,__ATOMIC_RELEASE);
    card_wake();
    for(unsigned i=0;i<CARD_WAIT_POLLS && __atomic_load_n(&c->result,__ATOMIC_ACQUIRE)!=serial;i++) TASK_SLEEP(CARD_WAIT_MS);
    if(__atomic_load_n(&c->result,__ATOMIC_ACQUIRE)!=serial) { c->status=CARD_BUSY;c->detail=0;c->loaded=0; }
    menu_show();
}

/* ---- label font --------------------------------------------------------- */
/* A static label's font family comes from the stock resolver 0x4032e19c
 * (language, string id); ours are all class 0, the menu font of the UI
 * language. Nikon's CJK fonts are subsets, so a name may need another
 * language's menu font. The kind-2 glyph builder (0x4032b648) calls the
 * resolver once at 0x4032b748 with the label's UTF-16 text in x8 and its
 * byte length in w9 (font-native FONT-SEAM.md); only our strings change. */
#define TEXT_FONT ((uint32_t (*)(uint32_t,uint32_t))(uintptr_t)0x4032e19c)
#define LABEL_BYTES_LIMIT 512u

static int font_has(uint32_t font,uint32_t code)
{
    /* Byte-packed {u16 first, u16 glyph, u16 count} records, sorted. */
    const volatile uint8_t *records=(const volatile uint8_t *)(uintptr_t)z8_rom_fonts[font].records;
    uint32_t low=0,high=z8_rom_fonts[font].count;
    while(low<high) {
        uint32_t middle=(low+high)/2;
        const volatile uint8_t *r=records+6*middle;
        uint32_t first=r[0]|(uint32_t)r[1]<<8,count=r[4]|(uint32_t)r[5]<<8;
        if(code<first) high=middle;
        else if(code-first>=count) low=middle+1;
        else return 1;
    }
    return 0;
}

static int font_covers(uint32_t font,const volatile uint16_t *text,uint32_t units)
{
    if(font>=Z8_ROM_FONTS) return 0;
    for(uint32_t i=0;i<units;i++) {
        uint32_t c=text[i];
        if(c<0x20) continue;
        if((c>=0xd800 && c<0xe000) || !font_has(font,c)) return 0;
    }
    return 1;
}

uint32_t z8_photo_lut_text_font(uint32_t language,uint32_t id,const volatile uint16_t *text,uint32_t bytes)
{
    uint32_t font=TEXT_FONT(language,id)&255;
    if(id-Z8_STRING_FIRST>=Z8_LABELS || !text || ((uintptr_t)text&1) || bytes>LABEL_BYTES_LIMIT) return font;
    uint32_t units=bytes/2;
    if(font_covers(font,text,units)) return font;
    /* The menu fonts of English, Simplified and Traditional Chinese, Japanese
     * and Korean, in that order; none covering the label keeps the UI font. */
    static const uint8_t languages[]={1,8,9,0,10};
    for(unsigned i=0;i<sizeof(languages);i++) {
        uint32_t other=TEXT_FONT(languages[i],id)&255;
        if(other!=font && font_covers(other,text,units)) return other;
    }
    return font;
}

/* ---- menu ------------------------------------------------------------- */
#define ROW_DISABLE ((void (*)(void *,uint32_t,uint32_t))(uintptr_t)0x403b1ed0)

/* GUI task: what the camera does for the selected bank, and the card-row
 * status that explains it. */
static uint32_t menu_state(const Z8LookBundle *b,uint32_t *index,uint32_t *status,uint32_t *count,uint32_t *slots)
{
    volatile Z8LutState *s=Z8_STATE;
    volatile Z8CardLibrary *c=&s->card;
    volatile Z8Persist *p=&s->persist;
    uint32_t state=effective(b,index);
    *count=0;*slots=0;
    if(state==Z8_EFFECTIVE_RESTORING) *status=CARD_RESTORING;
    else if(state==Z8_EFFECTIVE_UNRESOLVED) *status=CARD_UNRESOLVED;
    else if(state==Z8_EFFECTIVE_UNAVAILABLE) {
        *status=p->reason==CARD_LOADED?CARD_NOT_IN_LIBRARY:p->reason;
        *slots=p->detail;
    } else { *status=c->status;*count=c->loaded;*slots=c->detail; }
    return state;
}

/* Rewrite the menu strings when the status or the published library changed
 * since they were written (GUI task, before the page or row is drawn). */
static void menu_refresh(const Z8LookBundle *b,uint32_t status,uint32_t count,uint32_t slots)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    uint32_t key[2]={status|count<<8|slots<<16|1u<<24,__atomic_load_n(&p->library_generation,__ATOMIC_ACQUIRE)};
    if(p->text_key[0]==key[0] && p->text_key[1]==key[1]) return;
    menu_text(b,status,count,slots);
    p->text_key[0]=key[0];p->text_key[1]=key[1];
}

/* GUI task: bring the menu strings up to date. */
static void menu_show(void)
{
    const Z8LookBundle *b=library_wait();
    uint32_t index,status,count,slots;
    menu_state(b,&index,&status,&count,&slots);
    menu_refresh(b,status,count,slots);
    library_exit();
}

static void preview_wake(void)
{
    volatile Z8Preview *p=&Z8_STATE->preview;
    if(p->worker && p->worker_started) EVENT_SET(((PreviewWorker *)(uintptr_t)p->worker)->event,1);
}

int z8_photo_lut_menu_entry(void *page)
{
    volatile Z8LutState *s=Z8_STATE;
    const Z8LookBundle *b=library_wait();
    uint32_t index,status,count,slots,state=menu_state(b,&index,&status,&count,&slots);
    menu_refresh(b,status,count,slots);
    uint32_t looks=s->ready?b->count:0;
    library_exit();
    /* Look rows past the active library are disabled, which the native list
     * hides (QEMU run-01; the pinned row state of the pixel-shift browser). */
    for(uint32_t look=1;look<=Z8_LUT_MAX_LOOKS;look++) ROW_DISABLE(page,z8_row_of(look),look>looks);
    /* The mark names what the camera applies: Off, the applied look, or the
     * card row while the selected bank's look is restoring or unavailable;
     * opening the page then retries the card at once. */
    uint32_t row=state==Z8_EFFECTIVE_OFF?0:state==Z8_EFFECTIVE_APPLIED?z8_row_of(index):Z8_LOAD_ROW;
    if(row==Z8_LOAD_ROW) { __atomic_store_n(&s->card.retry_now,1,__ATOMIC_RELEASE);card_wake(); }
    /* Right after a card load ask for the cursor on the result row. Where the
     * native re-entry finally places it is not established; the rebuilt page
     * shows the result row either way (QEMU qemu-ui-c). */
    if(__atomic_exchange_n(&s->card.show_result,0,__ATOMIC_ACQ_REL)) SET_CURSOR(page,Z8_OPTION_FIRST+Z8_LOAD_ROW);
    else if(ENTRY_GUARD(page)!=1) SET_CURSOR(page,Z8_OPTION_FIRST+row);
    return SET_MARK(page,Z8_OPTION_FIRST+row,1);
}

/* Record writers (GUI task and the native settings task) take turns. */
static void record_lock(void)
{
    volatile Z8Persist *p=&Z8_STATE->persist;
    for(;;) {
        uint32_t free=0;
        if(__atomic_compare_exchange_n(&p->writer,&free,1,0,__ATOMIC_ACQUIRE,__ATOMIC_RELAXED)) return;
        TASK_SLEEP(1);
    }
}
static void record_unlock(void) { __atomic_store_n(&Z8_STATE->persist.writer,0,__ATOMIC_RELEASE); }

/* The stock dispatcher passes its transition request (page at +0, target
 * state at +0xC) and goes to the target read back afterwards (pixel-shift
 * PS11: 0x403DB910, 0x40508E04 -> 0x403AF53C). Choosing a look keeps the
 * stock target (back to the photo menu); a card load targets this page again. */
#define REQUEST_TARGET 0xcu
int z8_photo_lut_menu_ok(void *request)
{
    /* Exact stock option-row lookup, with bounds before storing the choice. */
    uint64_t page=read64((uintptr_t)request);
    uint64_t list=read64(page+0x50),rows=read64(list+0x10);
    uint32_t cursor=(uint32_t)read16(page+0x58)+read16(page+0x32);
    if(cursor>=Z8_ROWS || Z8_STATE->magic!=Z8_STATE_MAGIC) return 0;
    uint32_t item=read32(rows+(uint64_t)cursor*44+24);
    if(item<Z8_OPTION_FIRST || item>=Z8_OPTION_FIRST+Z8_ROWS) return 0;
    uint32_t row=item-Z8_OPTION_FIRST;
    if(row==Z8_LOAD_ROW) {
        card_request();
        __atomic_store_n(&Z8_STATE->card.show_result,1,__ATOMIC_RELEASE);
        *(volatile uint32_t *)((uintptr_t)request+REQUEST_TARGET)=Z8_MENU_STATE;
        return 0;
    }
    uint32_t bank=read32(Z8_BANK_INDEX);
    if(bank>=Z8_BANKS) return 0;
    Z8BankSlot slot={0};
    uint32_t option=row?row-1:0;
    if(option) {
        const Z8LookBundle *b=library_wait();
        unsigned valid=Z8_STATE->ready && option<=b->count;
        if(valid) { slot.kind=Z8_DESIRED_LOOK;copy_bytes(slot.id,b->looks[option-1].id,sizeof(slot.id)); }
        library_exit();
        if(!valid) return 0;                /* an unused row keeps the selection */
    }
    desire_bank(bank,&slot);
    return 0;
}

int z8_photo_lut_photo_refresh(void *page)
{
    int result=PHOTO_REFRESH(page);
    void *child=page?CHILD(page,Z8_MENU_ITEM,0x4ba7):0;
    const Z8LookBundle *b=library_wait();
    uint32_t index,status,count,slots,state=menu_state(b,&index,&status,&count,&slots);
    menu_refresh(b,status,count,slots);
    library_exit();
    if(child) {
        /* The reused row's stock value is a graphic. Reset its native kind
         * before binding a static language-table string (kind2). */
        if(read32((uintptr_t)child+12)!=2) BIND(child,0,0);
        /* Strings: +2 Off, +3 card row (status), +4.. the looks. */
        uint32_t text=state==Z8_EFFECTIVE_OFF?Z8_STRING_FIRST+2:
                      state==Z8_EFFECTIVE_APPLIED?Z8_STRING_FIRST+3+index:Z8_STRING_FIRST+3;
        if(read32((uintptr_t)child+8)!=text || read32((uintptr_t)child+12)!=2) BIND(child,text,2);
        CONTENT(child,text);
    }
    return result;
}

void z8_photo_lut_bank_reset_result(const uint64_t *saved)
{
    volatile Z8LutState *s=Z8_STATE;
    if(s->magic!=Z8_STATE_MAGIC || !(saved[0]&1)) return;
    uint32_t kind=(uint32_t)saved[19],bank=kind==9?read32(Z8_BANK_INDEX):kind-10;
    if(bank>=Z8_BANKS) return;
    /* Individual-bank reset deliberately excludes the folder-name item from
     * its 531-ID list. Reset the bank's look only after native success, to
     * the firmware default. */
    Z8BankSlot slot;
    default_slot(&slot);
    desire_bank(bank,&slot);
}
