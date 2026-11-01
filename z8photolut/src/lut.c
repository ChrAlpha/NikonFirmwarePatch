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

/* FIPS 180-4 SHA-256, streaming, general registers only. */
typedef struct { uint32_t h[8]; uint8_t block[64]; uint64_t bytes; } Sha256;
static const uint32_t sha_k[64]={
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
static uint32_t ror(uint32_t x,unsigned n) { return (x>>n)|(x<<(32-n)); }
static void sha_block(Sha256 *s)
{
    uint32_t w[64],v[8];
    for(unsigned i=0;i<16;i++)
        w[i]=(uint32_t)s->block[4*i]<<24|(uint32_t)s->block[4*i+1]<<16|(uint32_t)s->block[4*i+2]<<8|s->block[4*i+3];
    for(unsigned i=16;i<64;i++)
        w[i]=w[i-16]+(ror(w[i-15],7)^ror(w[i-15],18)^(w[i-15]>>3))+w[i-7]+(ror(w[i-2],17)^ror(w[i-2],19)^(w[i-2]>>10));
    for(unsigned i=0;i<8;i++) v[i]=s->h[i];
    for(unsigned i=0;i<64;i++) {
        uint32_t t1=v[7]+(ror(v[4],6)^ror(v[4],11)^ror(v[4],25))+((v[4]&v[5])^(~v[4]&v[6]))+sha_k[i]+w[i];
        uint32_t t2=(ror(v[0],2)^ror(v[0],13)^ror(v[0],22))+((v[0]&v[1])^(v[0]&v[2])^(v[1]&v[2]));
        for(unsigned j=7;j;j--) v[j]=v[j-1];
        v[4]+=t1;v[0]=t1+t2;
    }
    for(unsigned i=0;i<8;i++) s->h[i]+=v[i];
}
static void sha_start(Sha256 *s)
{
    static const uint32_t initial[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    for(unsigned i=0;i<8;i++) s->h[i]=initial[i];
    s->bytes=0;
}
static void sha_add(Sha256 *s,const void *data,uint32_t bytes)
{
    const uint8_t *p=data;
    while(bytes) {
        unsigned at=(unsigned)(s->bytes%64),take=64-at<bytes?64-at:bytes;
        for(unsigned i=0;i<take;i++) s->block[at+i]=p[i];
        s->bytes+=take;p+=take;bytes-=take;
        if(at+take==64) sha_block(s);
    }
}
static void sha_finish(Sha256 *s,uint8_t digest[32])
{
    uint64_t bits=s->bytes*8;
    uint8_t one=0x80,nil=0;
    sha_add(s,&one,1);
    while(s->bytes%64!=56) sha_add(s,&nil,1);
    for(unsigned i=0;i<8;i++) { uint8_t b=(uint8_t)(bits>>(56-8*i));sha_add(s,&b,1); }
    for(unsigned i=0;i<32;i++) digest[i]=(uint8_t)(s->h[i/4]>>(24-8*(i%4)));
}

void z8_lut_sha256(const void *data,uint32_t bytes,uint8_t digest[32])
{
    Sha256 s;sha_start(&s);
    sha_add(&s,data,bytes);
    sha_finish(&s,digest);
}

void z8_lut_look_id(uint32_t grid,const uint16_t *samples,uint8_t digest[32])
{
    static const char domain[]="z8photolut/look/1\0" Z8_LUT_SEMANTICS;
    uint8_t g[4]={(uint8_t)grid,(uint8_t)(grid>>8),(uint8_t)(grid>>16),(uint8_t)(grid>>24)};
    Sha256 s;sha_start(&s);
    sha_add(&s,domain,sizeof(domain));            /* includes the final NUL separator */
    sha_add(&s,g,4);
    sha_add(&s,samples,grid*grid*grid*6);
    sha_finish(&s,digest);
}

/* 1..63 bytes of well-formed UTF-8, NUL-padded to capacity; no C0/C1 control,
 * DEL, surrogate or XML-forbidden U+FFFE/U+FFFF; no outer spaces. Mirrors
 * z8photolut/library.py check_name so menu text and metadata stay valid. */
static const uint32_t utf8_minimum[4]={0,0x80,0x800,0x10000};
unsigned z8_lut_name_valid(const char *name,unsigned capacity)
{
    const uint8_t *p=(const uint8_t *)name;
    unsigned n=0;
    while(n<capacity && p[n]) n++;
    if(!n || n>Z8_LUT_NAME_BYTES || n>=capacity || p[0]==' ' || p[n-1]==' ') return 0;
    for(unsigned i=n;i<capacity;i++) if(p[i]) return 0;
    for(unsigned i=0;i<n;) {
        uint32_t c=p[i],extra=c<0x80?0:(c&0xe0)==0xc0?1:(c&0xf0)==0xe0?2:(c&0xf8)==0xf0?3:4;
        if(extra>3 || (extra && i+extra>=n)) return 0;
        if(extra) c&=0x3f>>extra;
        for(unsigned k=1;k<=extra;k++) {
            if((p[i+k]&0xc0)!=0x80) return 0;
            c=c<<6|(p[i+k]&0x3f);
        }
        if(c<utf8_minimum[extra] || c>0x10ffff || (c>=0xd800 && c<=0xdfff) || c<0x20 ||
           (c>=0x7f && c<=0x9f) || c==0xfffe || c==0xffff) return 0;
        i+=extra+1;
    }
    return 1;
}

static int zero(const uint8_t *p,uint32_t n)
{
    for(uint32_t i=0;i<n;i++) if(p[i]) return 0;
    return 1;
}

/* `digests`: recompute every transform identity and the library identity.
 * Card files always need it. The built-in image library is covered by the
 * firmware package hashes checked at build and installation, so boot checks
 * only structure, CRCs, names and no-op flags (baseline boot cost). */
static unsigned validate(const Z8LookBundle *b,uint32_t available,unsigned digests)
{
    if(!b || ((uintptr_t)b&7) || available<sizeof(*b) ||
       b->magic!=Z8_LUT_MAGIC || b->version!=3 || b->bytes<sizeof(*b) ||
       b->bytes>available || b->bytes>Z8_LUT_BUNDLE_LIMIT ||
       b->count>Z8_LUT_MAX_LOOKS || b->initial>b->count ||
       b->reserved) return Z8_LUT_BAD_BUNDLE;
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
           e->bytes>b->bytes-e->offset || e->identity>1 || e->flags ||
           !zero(e->padding,sizeof(e->padding)) || !zero(raw+end,next-end)) return Z8_LUT_BAD_BUNDLE;
        if(!z8_lut_name_valid(e->name,sizeof(e->name))) return Z8_LUT_BAD_BUNDLE;
        if(z8_lut_crc32(raw+e->offset,e->bytes)!=e->crc32) return Z8_LUT_BAD_BUNDLE;
        if(digests) {
            uint8_t digest[32];
            z8_lut_look_id(e->grid,(const uint16_t *)(raw+e->offset),digest);
            for(unsigned k=0;k<32;k++) if(digest[k]!=e->id[k]) return Z8_LUT_BAD_BUNDLE;
        }
        for(unsigned k=0;k<i;k++) {
            unsigned same=1;
            for(unsigned j=0;j<32 && same;j++) same=b->looks[k].id[j]==e->id[j];
            if(same) return Z8_LUT_BAD_BUNDLE;
        }
        /* The no-op flag must equal exact sample identity (host
         * library.identity_samples): a corrupted flag must never silently
         * disable a real look, nor mark a no-op as a transform. */
        unsigned same=1;
        const uint16_t *table=(const uint16_t *)(raw+e->offset);
        for(unsigned r=0;r<e->grid && same;r++) for(unsigned g=0;g<e->grid && same;g++) for(unsigned v=0;v<e->grid && same;v++) {
            unsigned indices[3]={r,g,v};
            for(unsigned c=0;c<3;c++) {
                uint32_t expected=(2u*indices[c]*65535u+e->grid-1)/(2u*(e->grid-1));
                same&=*table++==expected;
            }
        }
        if(same!=e->identity) return Z8_LUT_BAD_BUNDLE;
        for(unsigned k=0;k<i;k++) {
            unsigned j=0;
            while(j<sizeof(e->name) && b->looks[k].name[j]==e->name[j]) j++;
            if(j==sizeof(e->name)) return Z8_LUT_BAD_BUNDLE;   /* exact duplicate name */
        }
        end=e->offset+e->bytes;
    }
    if(end!=b->bytes) return Z8_LUT_BAD_BUNDLE;
    if(!digests) return Z8_LUT_OK;
    /* Library identity (host library.py Library.runtime): domain, default,
     * then every byte after the header. */
    static const char domain[]="z8photolut/library/1";
    uint8_t initial[4]={(uint8_t)b->initial,(uint8_t)(b->initial>>8),(uint8_t)(b->initial>>16),(uint8_t)(b->initial>>24)};
    uint8_t digest[32];
    Sha256 s;sha_start(&s);
    sha_add(&s,domain,sizeof(domain));
    sha_add(&s,initial,4);
    sha_add(&s,raw+64,b->bytes-64);
    sha_finish(&s,digest);
    for(unsigned k=0;k<32;k++) if(digest[k]!=b->library_id[k]) return Z8_LUT_BAD_BUNDLE;
    return Z8_LUT_OK;
}
unsigned z8_lut_validate(const Z8LookBundle *b,uint32_t available) { return validate(b,available,1); }
unsigned z8_lut_validate_image(const Z8LookBundle *b,uint32_t available) { return validate(b,available,0); }

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
