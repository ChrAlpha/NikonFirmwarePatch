/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "firmware.h"

__attribute__((used)) const uint32_t z8_photo_lut_runtime_layout[]={
    sizeof(Z8LutState),offsetof(Z8LutState,preview),offsetof(Z8Preview,slots),
    sizeof(Z8PreviewSlot),offsetof(Z8PreviewSlot,request),offsetof(Z8PreviewSlot,channel),
    offsetof(Z8Preview,cache_resource),offsetof(Z8Preview,paused),
    offsetof(Z8PreviewSlot,apply_sequence),offsetof(Z8PreviewSlot,flags),
    offsetof(Z8Preview,applies),offsetof(Z8Preview,display_epoch),Z8_PREVIEW_SLOTS,
    offsetof(Z8Preview,power_paused),offsetof(Z8LutState,jpeg_busy),offsetof(Z8LutState,jpeg_previews_applied)
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
static uint32_t choice(void)
{
    uint32_t value=__atomic_load_n(Z8_SAVED_CHOICE,__ATOMIC_ACQUIRE);
    if(!Z8_STATE->ready || value>Z8_BUNDLE->count) value=0;
    Z8_STATE->choice=value;
    return value;
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
            error=z8_lut_select(Z8_BUNDLE,selected,256,&look);
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
    copy_bytes((char *)parameters+12,"PhotoLUT",8);
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

void z8_photo_lut_boot(void)
{
    volatile Z8LutState *s=Z8_STATE;
    uint64_t boots=0;
    if(s->magic==Z8_STATE_MAGIC && s->version==2 && s->bytes==sizeof(*s)) {
        boots=s->boots;
    }
    volatile uint8_t *p=(volatile uint8_t *)s;
    for(unsigned i=0;i<sizeof(*s);i++) p[i]=0;
    s->version=2;s->bytes=sizeof(*s);s->boots=boots+1;s->strength=256;
    s->error=z8_lut_validate(Z8_BUNDLE,Z8_LUT_BUNDLE_LIMIT);
    /* Native DS initializes later. The authoritative selection is read from
     * the current photo bank at each menu/capture boundary, never from a
     * previous task's private RAM. */
    s->choice=0;
    s->ready=s->error==Z8_LUT_OK;
    s->magic=Z8_STATE_MAGIC;
    __atomic_thread_fence(__ATOMIC_RELEASE);
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
typedef struct {
    uint64_t magic,owner,base,capacity,scratch;
    uint32_t scratch_bytes,choice;
    uint64_t compressed_bytes,reserved;
    /* Keep mutable CPU ownership on a separate cache line. */
    uint64_t thumbnail[4];
    uint32_t locks[4],thumbnail_ready,mutable_reserved[3];
} JpegLease;
_Static_assert(sizeof(JpegLease)==128,"JPEG lease occupies two cache lines");

static uint32_t jpeg_required_bytes(uint32_t bytes,uint64_t format,uint32_t selected,uint32_t *scratch)
{
    /* The capture-settings block may live in pool0x22's cached image RAM
     * (observed C3.11 address c40000008), not only the main metadata arena. */
    if((format&3) || !cpu_span(format,0x94)) return 0;
    Z8Look look;
    if(z8_lut_select(Z8_BUNDLE,selected,256,&look)) return 0;
    *scratch=(!look.identity && look.strength && (JPEG_ENABLED(format)&1))?JPEG_STORAGE_BYTES:0;
    uint64_t offset=((uint64_t)bytes+511)&~UINT64_C(511);
    uint64_t required=offset+*scratch+sizeof(JpegLease);
    return bytes && required<=UINT32_MAX?(uint32_t)required:0;
}

uint32_t z8_photo_lut_output_budget(uint64_t settings,uint64_t format,uint32_t special)
{
    uint32_t bytes=((uint32_t (*)(uint64_t,uint64_t,uint32_t))(uintptr_t)0x4104886c)(settings,format,special);
    uint32_t scratch=0,required=jpeg_required_bytes(bytes,format,choice(),&scratch);
    /* Only the prospective pool0x38 photo count uses this wrapper. Keep
     * the native compressed layout and its zero-size unit queries intact. */
    return required?required:bytes;
}

uint64_t z8_photo_lut_output_alloc(uint32_t frame,uint32_t pool,uint32_t bytes,uint64_t configuration)
{
    uint32_t selected=choice(),scratch=0;
    if(configuration>UINT64_MAX-0x26c || pool!=0x38) return 0;
    uint32_t required=jpeg_required_bytes(bytes,configuration+0x26c,selected,&scratch);
    if(!required) return 0;
    uint64_t offset=((uint64_t)bytes+511)&~UINT64_C(511);
    uint64_t owner=POOL_ALLOC(frame,pool,required);
    if(!owner) return 0;
    if(!main_span(owner,0x18) || (owner&7)) return 0;
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
    clean(lease,sizeof(*lease));
    return owner;
failed:
    POOL_FREE(owner);
    return 0;
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
    /* The second line contains live CPU locks, never DMA data. */
    invalidate(source,64);
    *lease=*source;
    if(lease->magic!=JPEG_LEASE_MAGIC || lease->owner!=owner || lease->base!=raw ||
       lease->capacity!=capacity || lease->reserved || !lease->compressed_bytes ||
       lease->compressed_bytes>capacity-sizeof(*lease) ||
       lease->scratch!=raw+((lease->compressed_bytes+511)&~UINT64_C(511)) ||
       (lease->scratch_bytes && lease->scratch_bytes!=JPEG_STORAGE_BYTES) ||
       !range(lease->scratch,(uint64_t)lease->scratch_bytes+sizeof(*lease),raw,raw+capacity))
        return Z8_LUT_BAD_SPAN;
    *storage=(volatile JpegLease *)source;
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
    if(kind>3 || lease->scratch_bytes!=JPEG_STORAGE_BYTES || !main_span(parameters,0x50) || (parameters&7)) goto rejected;
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
    invalidate(source_y,f.y_bytes);invalidate(source_uv,f.uv_bytes);
    invalidate(f.y,bytes);
    copy_bytes(f.y,source_y,(uint32_t)f.y_bytes);copy_bytes(f.uv,source_uv,(uint32_t)f.uv_bytes);
    error=z8_lut_surface(&f,look);
    clean(f.y,bytes);
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
    if(kind==0) {
        s->jpeg_finished=ticks();
        __atomic_fetch_add(&s->jpeg_applied,1,__ATOMIC_RELAXED);
        __atomic_fetch_add(&s->jpeg_pixels,(uint64_t)f.width*f.height,__ATOMIC_RELAXED);
    } else {
        __atomic_fetch_add(&s->jpeg_previews_applied,1,__ATOMIC_RELAXED);
        __atomic_fetch_add(&s->jpeg_preview_pixels,(uint64_t)f.width*f.height,__ATOMIC_RELAXED);
    }
    __atomic_fetch_sub(&s->jpeg_busy,1,__ATOMIC_RELEASE);
    *applied=1;
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
    error=z8_lut_select(Z8_BUNDLE,lease.choice,256,&look);
    if(error) { jpeg_error(kind,error);return JPEG_ENCODE(channel,parameters); }
    if(look.identity || !look.strength) return JPEG_ENCODE(channel,parameters);
    jpeg_lock(&storage->locks[kind]);
    unsigned applied,result=jpeg_encode_locked(channel,parameters,kind,&lease,&look,&applied);
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
    error=z8_lut_select(Z8_BUNDLE,lease.choice,256,&look);
    if(error) { jpeg_error(3,error);return JPEG_ENCODE(channel,parameters); }
    if(look.identity || !look.strength || !lease.scratch_bytes) return JPEG_ENCODE(channel,parameters);
    jpeg_lock(&storage->locks[3]);
    __atomic_store_n(&storage->thumbnail_ready,0,__ATOMIC_RELEASE);
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
    if(private_ok && applied && bytes && bytes<=JPEG_THUMB_BYTES)
        __atomic_store_n(&storage->thumbnail_ready,1,__ATOMIC_RELEASE);
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

#define ENTRY_GUARD ((int (*)(void *))(uintptr_t)0x403ae0f8)
#define SET_CURSOR ((void (*)(void *,uint32_t))(uintptr_t)0x403b0048)
#define SET_MARK ((int (*)(void *,uint32_t,uint32_t))(uintptr_t)0x403b00b8)
#define PHOTO_REFRESH ((int (*)(void *))(uintptr_t)0x405d9808)
#define CHILD ((void *(*)(void *,uint32_t,uint32_t))(uintptr_t)0x4050a6f0)
#define CONTENT ((void (*)(void *,uint32_t))(uintptr_t)0x403f645c)
#define BIND ((int (*)(void *,uint32_t,uint32_t))(uintptr_t)0x4031e178)
#define REQUEST_SAVE ((void (*)(void))(uintptr_t)0x41012bf4)

int z8_photo_lut_menu_entry(void *page)
{
    uint32_t selected=choice();
    if(ENTRY_GUARD(page)!=1) SET_CURSOR(page,Z8_OPTION_FIRST+selected);
    return SET_MARK(page,Z8_OPTION_FIRST+selected,1);
}

void z8_photo_lut_menu_ok(void *context)
{
    /* Exact stock option-row lookup, with bounds before storing the choice. */
    uint64_t page=read64((uintptr_t)context);
    uint64_t list=read64(page+0x50),rows=read64(list+0x10);
    uint32_t cursor=(uint32_t)read16(page+0x58)+read16(page+0x32);
    if(cursor>Z8_BUNDLE->count || !Z8_STATE->ready) return;
    uint32_t item=read32(rows+(uint64_t)cursor*44+24);
    if(item<Z8_OPTION_FIRST || item>Z8_OPTION_FIRST+Z8_BUNDLE->count) return;
    __atomic_store_n(Z8_SAVED_CHOICE,(uint8_t)(item-Z8_OPTION_FIRST),__ATOMIC_RELEASE);
    Z8_STATE->choice=item-Z8_OPTION_FIRST;
    /* Request native NVRAM serialization. It compares and writes the entire
     * 0x2e0-byte photo block, including these alignment bytes. */
    REQUEST_SAVE();
    if(Z8_STATE->preview.worker && Z8_STATE->preview.worker_started)
        EVENT_SET(((PreviewWorker *)(uintptr_t)Z8_STATE->preview.worker)->event,1);
}

int z8_photo_lut_photo_refresh(void *page)
{
    int result=PHOTO_REFRESH(page);
    void *child=page?CHILD(page,Z8_MENU_ITEM,0x4ba7):0;
    uint32_t selected=choice();
    if(child) {
        /* The reused row's stock value is a graphic. Reset its native kind
         * before binding a static language-table string (kind2). */
        if(read32((uintptr_t)child+12)!=2) BIND(child,0,0);
        if(read32((uintptr_t)child+8)!=Z8_STRING_FIRST+2+selected || read32((uintptr_t)child+12)!=2)
            BIND(child,Z8_STRING_FIRST+2+selected,2);
        CONTENT(child,Z8_STRING_FIRST+2+selected);
    }
    return result;
}

void z8_photo_lut_bank_reset_result(const uint64_t *saved)
{
    if(Z8_STATE->magic!=Z8_STATE_MAGIC || !Z8_STATE->ready || !(saved[0]&1)) return;
    uint32_t kind=(uint32_t)saved[19],bank=kind==9?0:kind-10;
    if(bank>=4) return;
    /* Individual-bank reset deliberately excludes the folder-name item from
     * its531-ID list. Reset our independent setting only after native success.
     * Whole-camera resets already copy the patched group6 defaults. */
    uint8_t initial=(uint8_t)Z8_BUNDLE->initial;
    __atomic_store_n(Z8_SAVED_CHOICE+(bank+1)*0x2e0,initial,__ATOMIC_RELEASE);
    if(read32(UINT64_C(0xa03d48164))==bank) {
        __atomic_store_n(Z8_SAVED_CHOICE,initial,__ATOMIC_RELEASE);
        Z8_STATE->choice=initial;
        if(Z8_STATE->preview.worker && Z8_STATE->preview.worker_started)
            EVENT_SET(((PreviewWorker *)(uintptr_t)Z8_STATE->preview.worker)->event,1);
    }
    REQUEST_SAVE();
}
