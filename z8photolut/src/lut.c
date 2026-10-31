/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "lut.h"

uint32_t z8_lut_crc32(const void *data, uint32_t bytes)
{
    const uint8_t *p=data;
    uint32_t crc=~0u;
    for(uint32_t i=0;i<bytes;i++) {
        crc^=p[i];
        for(unsigned b=0;b<8;b++) crc=(crc>>1)^(0xedb88320u & (0u-(crc&1u)));
    }
    return ~crc;
}

static int zero(const uint8_t *p,uint32_t n)
{
    for(uint32_t i=0;i<n;i++) if(p[i]) return 0;
    return 1;
}

unsigned z8_lut_validate(const Z8LookBundle *b,uint32_t available)
{
    if(!b || ((uintptr_t)b&7) || available<sizeof(*b) ||
       b->magic!=Z8_LUT_MAGIC || b->version!=2 || b->bytes<sizeof(*b) ||
       b->bytes>available || b->bytes>Z8_LUT_BUNDLE_LIMIT ||
       !b->count || b->count>Z8_LUT_MAX_LOOKS || b->initial>b->count ||
       b->reserved || !zero(b->padding,sizeof(b->padding))) return Z8_LUT_BAD_BUNDLE;
    const uint8_t *raw=(const uint8_t *)b;
    if(z8_lut_crc32(raw+64,b->bytes-64)!=b->crc32) return Z8_LUT_BAD_BUNDLE;
    uint32_t end=sizeof(*b);
    for(unsigned i=0;i<Z8_LUT_MAX_LOOKS;i++) {
        const Z8LookEntry *e=&b->looks[i];
        if(i>=b->count) {
            if(!zero((const uint8_t *)e,sizeof(*e))) return Z8_LUT_BAD_BUNDLE;
            continue;
        }
        uint32_t next=(end+63)&~63u;
        if((e->grid!=17 && e->grid!=33 && e->grid!=65) || e->offset!=next ||
           e->bytes!=e->grid*e->grid*e->grid*6 || e->offset>b->bytes ||
           e->bytes>b->bytes-e->offset || e->identity>1 || e->reserved ||
           !zero(e->padding,sizeof(e->padding)) || !zero(raw+end,next-end)) return Z8_LUT_BAD_BUNDLE;
        unsigned terminator=0;
        while(terminator<64 && e->title[terminator]) {
            if((uint8_t)e->title[terminator]<32) return Z8_LUT_BAD_BUNDLE;
            terminator++;
        }
        if(!terminator || terminator==64 || !zero((const uint8_t *)e->title+terminator,64-terminator)) return Z8_LUT_BAD_BUNDLE;
        if(z8_lut_crc32(raw+e->offset,e->bytes)!=e->crc32) return Z8_LUT_BAD_BUNDLE;
        /* A corrupted identity flag must never silently disable a real look. */
        if(e->identity) {
            const uint16_t *table=(const uint16_t *)(raw+e->offset);
            for(unsigned r=0;r<e->grid;r++) for(unsigned g=0;g<e->grid;g++) for(unsigned v=0;v<e->grid;v++) {
                unsigned indices[3]={r,g,v};
                for(unsigned c=0;c<3;c++) {
                    uint32_t expected=(indices[c]*65535u+(e->grid-1)/2)/(e->grid-1);
                    uint32_t actual=*table++;
                    /* Host round-to-even and nearest-away differ only at ties. */
                    if(actual+1<expected || actual>expected+1) return Z8_LUT_BAD_BUNDLE;
                }
            }
        }
        end=e->offset+e->bytes;
    }
    return end==b->bytes ? Z8_LUT_OK : Z8_LUT_BAD_BUNDLE;
}

unsigned z8_lut_select(const Z8LookBundle *b,uint32_t choice,uint32_t strength,Z8Look *look)
{
    if(!look || !b || choice>b->count || strength>256) return Z8_LUT_BAD_SELECTION;
    if(!choice) { *look=(Z8Look){0,17,1,0}; return Z8_LUT_OK; }
    const Z8LookEntry *e=&b->looks[choice-1];
    *look=(Z8Look){(const uint16_t *)((const uint8_t *)b+e->offset),e->grid,e->identity,strength};
    return Z8_LUT_OK;
}

/* Tetrahedral interpolation reads four RGB vertices. Fractions have exact
 * denominator 255; one final division converts weighted uint16 to uint8.
 * The ordering is chosen per pixel, never specialized for a particular look. */
static inline __attribute__((always_inline)) void lookup_rgb(const Z8Look *look,const uint8_t rgb[3],uint8_t output[3])
{
    if(look->identity || !look->strength) {
        for(unsigned c=0;c<3;c++) output[c]=rgb[c];
        return;
    }
    unsigned low[3],f[3],n=look->grid;
    for(unsigned c=0;c<3;c++) {
        unsigned scaled=rgb[c]*(n-1);
        low[c]=scaled/255; f[c]=scaled%255;
        if(low[c]==n-1) { low[c]--; f[c]=255; }
    }
    unsigned first=((low[0]*n+low[1])*n+low[2])*3;
    const uint16_t *p0=look->samples+first;
    const uint16_t *p1,*p2,*p3=p0+(n*n+n+1)*3;
    unsigned high,middle,last;
    if(f[0]>=f[1]) {
        if(f[1]>=f[2]) { p1=p0+n*n*3;p2=p1+n*3;high=f[0];middle=f[1];last=f[2]; }
        else if(f[0]>=f[2]) { p1=p0+n*n*3;p2=p1+3;high=f[0];middle=f[2];last=f[1]; }
        else { p1=p0+3;p2=p1+n*n*3;high=f[2];middle=f[0];last=f[1]; }
    } else {
        if(f[0]>=f[2]) { p1=p0+n*3;p2=p1+n*n*3;high=f[1];middle=f[0];last=f[2]; }
        else if(f[1]>=f[2]) { p1=p0+n*3;p2=p1+3;high=f[1];middle=f[2];last=f[0]; }
        else { p1=p0+3;p2=p1+n*3;high=f[2];middle=f[1];last=f[0]; }
    }
    unsigned w0=255-high,w1=high-middle,w2=middle-last,w3=last;
    for(unsigned c=0;c<3;c++) {
        uint32_t sum=w0*p0[c]+w1*p1[c]+w2*p2[c]+w3*p3[c];
        output[c]=(uint8_t)((sum+32767)/65535);
    }
}

void z8_lut_rgb(const Z8Look *look,const uint8_t rgb[3],uint8_t output[3])
{
    lookup_rgb(look,rgb,output);
}

static uint8_t clip(int32_t value)
{
    return value<0?0:value>255?255:(uint8_t)value;
}

static inline __attribute__((always_inline)) void transform(const Z8Look *look,uint8_t y,uint8_t u,uint8_t v,uint8_t output[3])
{
    int32_t cb=(int32_t)u-128,cr=(int32_t)v-128;
    uint8_t rgb[3]={clip(y+((359*cr+128)>>8)),clip(y-((88*cb+183*cr+128)>>8)),clip(y+((454*cb+128)>>8))};
    uint8_t mapped[3];
    lookup_rgb(look,rgb,mapped);
    int32_t r=mapped[0],g=mapped[1],b=mapped[2];
    uint8_t result[3]={(uint8_t)((77*r+150*g+29*b+128)>>8),
        clip(((-43*r-85*g+128*b+128)>>8)+128),clip(((128*r-107*g-21*b+128)>>8)+128)};
    uint8_t original[3]={y,u,v};
    for(unsigned c=0;c<3;c++) output[c]=(uint8_t)((result[c]*look->strength+original[c]*(256-look->strength)+128)>>8);
}

unsigned z8_lut_surface_cancel(const Z8Surface *f,const Z8Look *look,const volatile uint32_t *cancel)
{
    if(!f || !f->width || !f->height || ((f->width|f->height)&1) ||
       f->width>16384 || f->height>8192 || f->y_stride<f->width || f->uv_stride<f->width ||
       f->y_stride>32768 || f->uv_stride>32768 || (f->uv_divisor!=1 && f->uv_divisor!=2)) return Z8_LUT_BAD_GEOMETRY;
    uint64_t yn=(uint64_t)f->y_stride*f->height,un=(uint64_t)f->uv_stride*(f->height/f->uv_divisor);
    uintptr_t yp=(uintptr_t)f->y,up=(uintptr_t)f->uv;
    if(!yp || !up || yn>f->y_bytes || un>f->uv_bytes || yn>UINTPTR_MAX-yp || un>UINTPTR_MAX-up ||
       (yp<up+un && up<yp+yn)) return Z8_LUT_BAD_SPAN;
    if(!look || look->strength>256 || look->identity>1 ||
       (look->grid!=17 && look->grid!=33 && look->grid!=65) ||
       (!look->identity && look->strength && (!look->samples || ((uintptr_t)look->samples&1)))) return Z8_LUT_BAD_LOOK;
    if(look->identity || !look->strength) return Z8_LUT_OK;
    const Z8Look selected=*look;
    for(unsigned row=0;row<f->height;row+=f->uv_divisor) {
        if(cancel && __atomic_load_n(cancel,__ATOMIC_ACQUIRE)) return Z8_LUT_CANCELLED;
        uint8_t *chroma=f->uv+(uint64_t)(row/f->uv_divisor)*f->uv_stride;
        for(unsigned x=0;x<f->width;x+=2) {
            uint8_t u=chroma[x],v=chroma[x+1];
            uint32_t usum=0,vsum=0;
            for(unsigned dy=0;dy<f->uv_divisor;dy++) {
                uint8_t *luma=f->y+(uint64_t)(row+dy)*f->y_stride+x;
                for(unsigned dx=0;dx<2;dx++) {
                    uint8_t output[3];
                    transform(&selected,luma[dx],u,v,output);
                    luma[dx]=output[0];usum+=output[1];vsum+=output[2];
                }
            }
            unsigned count=2*f->uv_divisor;
            chroma[x]=(uint8_t)((usum+count/2)/count);
            chroma[x+1]=(uint8_t)((vsum+count/2)/count);
        }
    }
    return Z8_LUT_OK;
}

unsigned z8_lut_surface(const Z8Surface *f,const Z8Look *look)
{ return z8_lut_surface_cancel(f,look,0); }

/* Preview cache keeps every 8-bit luma value and 65 chroma coordinates.
 * RGB LUT evaluation and color conversion occur once per selected look.
 * Three 8-bit outputs occupy independent16-bit lanes, allowing exact
 * rounded bilinear lane arithmetic with two64-bit multiplies per lerp.
 * JPEG continues to use the original per-pixel RGB tetrahedral transform. */
unsigned z8_lut_preview_build_cancel(const Z8Look *look,uint64_t *table,uint32_t bytes,const volatile uint32_t *cancel)
{
    if(!table || ((uintptr_t)table&7) || bytes!=Z8_PREVIEW_TABLE_BYTES || !look ||
       !look->samples || look->strength!=256 || look->identity ||
       (look->grid!=17 && look->grid!=33 && look->grid!=65)) return Z8_LUT_BAD_LOOK;
    unsigned at=0;
    for(unsigned u=0;u<Z8_PREVIEW_GRID;u++) for(unsigned v=0;v<Z8_PREVIEW_GRID;v++) {
        if(cancel && __atomic_load_n(cancel,__ATOMIC_ACQUIRE)) return Z8_LUT_CANCELLED;
        uint8_t cb=(uint8_t)(u==64?255:u*4),cr=(uint8_t)(v==64?255:v*4);
        for(unsigned y=0;y<256;y++) {
            uint8_t output[3];transform(look,(uint8_t)y,cb,cr,output);
            table[at++]=(uint64_t)output[0]|((uint64_t)output[1]<<16)|((uint64_t)output[2]<<32);
        }
    }
    return Z8_LUT_OK;
}

unsigned z8_lut_preview_build(const Z8Look *look,uint64_t *table,uint32_t bytes)
{ return z8_lut_preview_build_cancel(look,table,bytes,0); }

static inline uint64_t preview_lerp(uint64_t a,uint64_t b,unsigned fraction)
{
    return ((a*(256-fraction)+b*fraction+UINT64_C(0x008000800080))>>8)&UINT64_C(0x00ff00ff00ff);
}

unsigned z8_lut_preview_surface_cancel(const Z8Surface *f,const uint64_t *table,uint32_t bytes,const volatile uint32_t *cancel)
{
    Z8Look off={0,17,1,0};
    unsigned error=z8_lut_surface(f,&off);
    if(error) return error;
    if(!table || ((uintptr_t)table&7) || bytes!=Z8_PREVIEW_TABLE_BYTES) return Z8_LUT_BAD_LOOK;
    for(unsigned row=0;row<f->height;row+=f->uv_divisor) {
        if(cancel && __atomic_load_n(cancel,__ATOMIC_ACQUIRE)) return Z8_LUT_CANCELLED;
        uint8_t *chroma=f->uv+(uint64_t)(row/f->uv_divisor)*f->uv_stride;
        for(unsigned x=0;x<f->width;x+=2) {
            unsigned u=chroma[x],v=chroma[x+1];
            unsigned fu=u<252?(u&3)*64:((u-252)*256+1)/3;
            unsigned fv=v<252?(v&3)*64:((v-252)*256+1)/3;
            const uint64_t *base=table+(((u>>2)*65+(v>>2))*256);
            uint64_t sum=0;
            for(unsigned dy=0;dy<f->uv_divisor;dy++) {
                uint8_t *luma=f->y+(uint64_t)(row+dy)*f->y_stride+x;
                for(unsigned dx=0;dx<2;dx++) {
                    unsigned y=luma[dx];
                    uint64_t low=fv?preview_lerp(base[y],base[256+y],fv):base[y];
                    uint64_t result=low;
                    if(fu) {
                        uint64_t high=fv?preview_lerp(base[65*256+y],base[66*256+y],fv):base[65*256+y];
                        result=preview_lerp(low,high,fu);
                    }
                    luma[dx]=(uint8_t)result;
                    sum+=result;
                }
            }
            /* At most four 8-bit values occupy each16-bit lane. */
            chroma[x]=(uint8_t)(((sum>>16)+f->uv_divisor)>>f->uv_divisor);
            chroma[x+1]=(uint8_t)(((sum>>32)+f->uv_divisor)>>f->uv_divisor);
        }
    }
    return Z8_LUT_OK;
}

unsigned z8_lut_preview_surface(const Z8Surface *f,const uint64_t *table,uint32_t bytes)
{ return z8_lut_preview_surface_cancel(f,table,bytes,0); }
