/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Sanitizer and AArch64 ISA checks use the same kernel as the camera. */
#include "../z8photolut/src/lut.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

int main(int argc,char **argv)
{
    (void)argv;
    unsigned width=argc>1?8256:126,height=argc>1?5504:66,stride=width+64;
    uint16_t *table=malloc(33*33*33*3*sizeof(*table));assert(table);
    unsigned at=0;
    for(unsigned r=0;r<33;r++) for(unsigned g=0;g<33;g++) for(unsigned b=0;b<33;b++) {
        table[at++]=(uint16_t)((g*65535u+16)/32);
        table[at++]=(uint16_t)((b*65535u+16)/32);
        table[at++]=(uint16_t)((r*65535u+16)/32);
    }
    Z8Look look={table,33,0,256};
    for(unsigned divisor=1;divisor<=2;divisor++) {
        size_t yn=(size_t)stride*height,un=yn/divisor;
        uint8_t *y=malloc(yn+128),*uv=malloc(un+128);assert(y&&uv);
        memset(y,0x5a,yn+128);memset(uv,0xa5,un+128);
        for(unsigned row=0;row<height;row++) memset(y+64+(size_t)row*stride,100,width);
        for(unsigned row=0;row<height/divisor;row++) for(unsigned x=0;x<width;x+=2) {
            uv[64+(size_t)row*stride+x]=100;uv[65+(size_t)row*stride+x]=180;
        }
        Z8Surface f={y+64,uv+64,yn,un,width,height,stride,stride,divisor};
        clock_t start=clock();
        assert(z8_lut_surface(&f,&look)==0);
        double elapsed=(double)(clock()-start)/CLOCKS_PER_SEC;
        uint8_t first_y=y[64],first_u=uv[64],first_v=uv[65];
        assert(first_y!=100 && first_u!=100 && first_v!=180);
        for(unsigned row=0;row<height;row++) {
            for(unsigned x=0;x<width;x++) assert(y[64+(size_t)row*stride+x]==first_y);
            for(unsigned x=width;x<stride;x++) assert(y[64+(size_t)row*stride+x]==0x5a);
        }
        for(unsigned row=0;row<height/divisor;row++) {
            for(unsigned x=0;x<width;x+=2) {
                assert(uv[64+(size_t)row*stride+x]==first_u);
                assert(uv[65+(size_t)row*stride+x]==first_v);
            }
            for(unsigned x=width;x<stride;x++) assert(uv[64+(size_t)row*stride+x]==0xa5);
        }
        for(unsigned x=0;x<64;x++) {
            assert(y[x]==0x5a&&y[64+yn+x]==0x5a);
            assert(uv[x]==0xa5&&uv[64+un+x]==0xa5);
        }
        printf("{\"width\":%u,\"height\":%u,\"uv_divisor\":%u,\"seconds\":%.6f,\"yuv\":[%u,%u,%u],\"physical_camera_timing\":false}\n",width,height,divisor,elapsed,first_y,first_u,first_v);
        free(y);free(uv);
    }
    free(table);
    return 0;
}
