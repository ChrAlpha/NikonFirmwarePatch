#!/usr/bin/env python3
"""Count executed AArch64 instructions in the actual freestanding LUT kernel."""
import json
import ctypes as C
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_BLOCK
from unicorn import arm64_const as regs
from z8photolut.patcher import compile_code,CODE


def main():
    code,symbols,meta=compile_code()
    table=bytearray()
    for r in range(33):
        for g in range(33):
            for b in range(33):table.extend(struct.pack('<3H',*((v*65535+16)//32 for v in (g,b,r))))
    class Look(C.Structure):
        _fields_=[('table',C.c_void_p),('grid',C.c_uint32),('identity',C.c_uint32),('strength',C.c_uint32)]
    with tempfile.TemporaryDirectory(prefix='lut-preview-measure-') as tmp:
        lib=Path(tmp)/'lut.so'
        subprocess.run(['cc','-O2','-shared','-fPIC',str(Path(__file__).resolve().parents[1]/'z8photolut/src/lut.c'),'-o',str(lib)],check=True)
        native=C.CDLL(str(lib))
        rgb=C.create_string_buffer(bytes(table));look=Look(C.addressof(rgb),33,0,256)
        cache=(C.c_uint64*(256*65*65))()
        native.z8_lut_preview_build.argtypes=[C.POINTER(Look),C.c_void_p,C.c_uint32]
        assert native.z8_lut_preview_build(C.byref(look),cache,C.sizeof(cache))==0
        preview=bytes(cache)
    records=[]
    samples=[('grid_node',100,180),('one_fraction',101,180),('two_fractions',101,183),
             ('last_interval',253,254),('endpoint',255,255)]
    for mode,size,label,u,v in [('rgb',n,'grid_node',100,180) for n in (2,16,32)]+[
            (mode,32,label,u,v) for label,u,v in samples for mode in ('rgb','preview')]:
        machine=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        for at,length in [(CODE,65536),(0x61000000,0x100000),(0x62000000,0x20000),
                          (0x63000000,65536),(0x65000000,4096)]:machine.mem_map(at,length)
        if mode=='preview':
            machine.mem_map(0x66000000,(len(preview)+4095)&~4095)
            machine.mem_write(0x66000000,preview)
        machine.mem_write(CODE,code)
        machine.mem_write(0x61010000,bytes(table))
        pixels=size*size
        machine.mem_write(0x61000000,struct.pack('<4Q5I4x',0x62000000,0x62010000,pixels,pixels,size,size,size,size,1))
        machine.mem_write(0x61000100,struct.pack('<Q3I4x',0x61010000,33,0,256))
        machine.mem_write(0x62000000,bytes([100])*pixels)
        machine.mem_write(0x62010000,bytes([u,v])*(pixels//2))
        machine.reg_write(regs.UC_ARM64_REG_X0,0x61000000)
        machine.reg_write(regs.UC_ARM64_REG_X1,0x61000100)
        if mode=='preview':
            machine.reg_write(regs.UC_ARM64_REG_X1,0x66000000)
            machine.reg_write(regs.UC_ARM64_REG_X2,len(preview))
        machine.reg_write(regs.UC_ARM64_REG_SP,0x63008000)
        machine.reg_write(regs.UC_ARM64_REG_X30,0x65000000)
        counts=[0]
        machine.hook_add(UC_HOOK_BLOCK,lambda uc,address,length,user:counts.__setitem__(0,counts[0]+length//4))
        machine.emu_start(symbols['z8_lut_preview_surface' if mode=='preview' else 'z8_lut_surface'],0x65000000,count=2000000)
        assert machine.reg_read(regs.UC_ARM64_REG_PC)==0x65000000
        assert machine.reg_read(regs.UC_ARM64_REG_X0)==0
        if (u,v)==(100,180):assert machine.mem_read(0x62000000,pixels)==bytes([71])*pixels
        records.append(dict(mode=mode,sample=label,chroma=[u,v],width=size,height=size,
                            instructions=counts[0],instructions_per_pixel=counts[0]/pixels))
    print(json.dumps(dict(code_sha256=meta['sha256'],measurements=records,physical_camera_timing=False),indent=2))


if __name__=='__main__':main()
