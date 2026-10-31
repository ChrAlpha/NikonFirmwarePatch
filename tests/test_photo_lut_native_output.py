"""Execute pinned C3.11 output construction, allocation and capacity forecasts.

Opt in with Z8_PHOTO_LUT_ROM=/path/to/z8-main-payload.bi. The proprietary ROM
is not a test fixture in this repository. These checks execute the native pool
allocator, size tables, and output-layout functions; OS heap/mutex, logging,
pool publication/availability, and cache-maintenance calls are isolated. They do not model
physical DMA/cache behaviour or replace the complete-camera NEF regression.
"""
import hashlib
import os
from pathlib import Path
import struct
import unittest

from z8photolut.cube import bundle,parse
from z8photolut.patcher import BASE,CODE,LUT,branch,compile_code

try:
    from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE,UC_HOOK_MEM_UNMAPPED
    from unicorn import arm64_const as regs
except ImportError:
    Uc=None


ROM_SHA256='2b43a31c9de838f84b12416de4348a7004e65be07f853ec56ec2b10f3a8e3853'
DATA_BIAS=0x9bdb95000
STATE=0xa101f0000
INFO=0xc40000008
FORECAST_INFO=0xa03bc6a24
STACK=0x500f0000
POOL=0x38
POOL_CONFIG=0xa02a24460
STOP=0x60000000
SCRATCH_SLICES=(8256*5504*2,1920*1080*2,640*480*2,192*120*2,0x3600)
SCRATCH=sum(SCRATCH_SLICES)
LEASE_MAGIC=0x3154504f4c385a4a
LEASE_FORMAT='<5Q2I2Q'
LEASE_BYTES=128


def align512(value):
    return (value+511)&~511


class NativeOutputMachine:
    def __init__(self,rom,code,symbols,metadata,looks,*,patched=True,separate=True,pool_units=None):
        self.m=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        self.rom=rom
        self.symbols=symbols
        self.heap=0xa11000000
        self.allocations=[]
        self.frees=[]
        self.constructed=[]
        self.tasks=[]
        self.native_bytes=0
        self.m.mem_map(BASE,(len(rom)+4095)&~4095)
        self.m.mem_write(BASE,rom)
        self.m.mem_map(0xa02000000,0x2000000)
        self.m.mem_map(self.heap,0x800000)
        self.m.mem_map(STATE,0x2000)
        # Complete C3.11 boot puts capture settings in pool0x22's cached
        # image-RAM payload, not the main metadata arena.
        self.m.mem_map(INFO&~4095,0x4000)
        self.m.mem_map(0xa0497d000,0x1000)
        self.m.mem_map(0x50000000,0x100000)
        self.m.mem_map(STOP,0x1000)
        # This ROM segment is copied to its linked RAM address during boot.
        source=0x44c6b000
        self.m.mem_write(source+DATA_BIAS,rom[source-BASE:])
        self.m.mem_write(CODE,code)
        self.m.mem_write(LUT,looks)
        self.m.mem_write(STATE,struct.pack('<Q6I',0x32545354554c385a,2,
            metadata['runtime_layout']['state_bytes'],2,256,1,0))
        self.m.mem_write(0xa03d482ba,b'\x02')
        # Execute native 41186718 with the image-RAM cached-alias contract.
        self.m.mem_write(0xa0497d030,struct.pack('<QIIQII',0x800000000,1,0,0xa00000000,0,0))
        if separate:
            self.m.mem_write(0x410590dc,branch(0x410590dc,0x410590e0))
        if patched:
            self.m.mem_write(0x41048e2c,branch(0x41048e2c,symbols['z8_photo_lut_output_alloc_entry'],True))
            self.m.mem_write(0x40f98958,branch(0x40f98958,symbols['z8_photo_lut_output_budget'],True))
        if pool_units is not None:
            unit=self.u32(POOL_CONFIG+8)
            self.m.mem_write(POOL_CONFIG+4,struct.pack('<III',unit*pool_units,unit,pool_units))
        self.m.hook_add(UC_HOOK_CODE,self._instruction)
        self.m.hook_add(UC_HOOK_MEM_UNMAPPED,self._map_footer)
        assert self.call(0x40cda794,POOL_CONFIG,POOL)==1
        # Native mode setup publishes this pool before the count wrappers
        # test its presence. The allocator and its free extents remain native.
        self.m.mem_write(0xa03f316c0+POOL*8,struct.pack('<Q',POOL_CONFIG))

    def u32(self,address):
        return struct.unpack('<I',self.m.mem_read(address,4))[0]

    def u64(self,address):
        return struct.unpack('<Q',self.m.mem_read(address,8))[0]

    def _return(self,value=None):
        if value is not None:
            self.m.reg_write(regs.UC_ARM64_REG_X0,value)
        self.m.reg_write(regs.UC_ARM64_REG_PC,self.m.reg_read(regs.UC_ARM64_REG_X30))

    def _map_footer(self,machine,access,address,size,value,user):
        # Map only the cached footer page, never fabricate image data or RAM
        # outside a successfully allocated owner's actual capacity.
        for owner,raw,capacity in self.allocations:
            footer=raw+0x400000000+capacity-LEASE_BYTES
            if footer<=address and address+size<=footer+LEASE_BYTES:
                machine.mem_map(address&~4095,4096)
                return True
        return False

    def _instruction(self,machine,address,size,user):
        x0=machine.reg_read(regs.UC_ARM64_REG_X0)
        if address==0x41048e20:
            self.native_bytes=x0
        elif address==0x410474c4 and x0:
            descriptor=self.u64(x0+8)
            self.allocations.append((x0,self.u64(descriptor),self.u32(descriptor+8)))
        elif address==0x410477c8:
            self.frees.append(x0)
        elif address==0x41048e38:
            self.constructed.append(machine.reg_read(regs.UC_ARM64_REG_X20))
        elif address==0x40d2c408:
            pointer=self.heap
            self.heap+=(x0+63)&~63
            if self.heap>0xa11800000:
                raise AssertionError('Native allocator metadata exceeded the isolated heap')
            self._return(pointer)
        elif address in (0x40d2c448,0x40d2c674,0x40d2c660,0x41050d1c,0x41051a70,0x41047a00,0x40d2b9c8):
            self._return(1)
        elif address in (0x40d2bc28,0x40d2d498):
            # Isolated OS event/task creation for the LUT JPEG worker that a
            # transform reservation starts; record it, return a valid id.
            self.tasks.append(address)
            machine.mem_write(x0,struct.pack('<i',100+len(self.tasks)))
            self._return()
        elif address==0x40d2d5cc:
            self.tasks.append(address)
            self._return()
        elif address==0x4127ab40:
            value=machine.reg_read(regs.UC_ARM64_REG_X1)&255
            length=machine.reg_read(regs.UC_ARM64_REG_X2)
            machine.mem_write(x0,bytes([value])*length)
            self._return()
        elif address==0x4127a900:
            source=machine.reg_read(regs.UC_ARM64_REG_X1)
            length=machine.reg_read(regs.UC_ARM64_REG_X2)
            machine.mem_write(x0,bytes(machine.mem_read(source,length)))
            self._return()

    def call(self,address,*arguments,stop=STOP):
        self.m.reg_write(regs.UC_ARM64_REG_SP,STACK)
        self.m.reg_write(regs.UC_ARM64_REG_X30,STOP)
        for index,value in enumerate(arguments):
            self.m.reg_write(getattr(regs,f'UC_ARM64_REG_X{index}'),value)
        self.m.emu_start(address,stop,count=2_000_000)
        if self.m.reg_read(regs.UC_ARM64_REG_PC)!=stop:
            raise AssertionError(f'Native execution did not return: {self.m.reg_read(regs.UC_ARM64_REG_PC):#x}')
        return self.m.reg_read(regs.UC_ARM64_REG_X0)

    def configure(self,*,format=0,dimension=0,quality=0,alternate=0,choice=2):
        self.m.mem_write(INFO,bytes(0x3000))
        for offset,value in [(0x294,format),(0x298,dimension),(0x290,quality),(0x2fc,alternate)]:
            self.m.mem_write(INFO+offset,struct.pack('<I',value))
        self.m.mem_write(0xa03d482ba,bytes([choice]))

    def construct(self):
        return self.call(0x41048d78,42,INFO)

    def forecast(self):
        # Enter the actual forecast BL setup with its native stack/settings
        # layout, then execute both pool-count calls. Stop before RAW policy.
        self.m.mem_write(STACK+0x10,bytes(self.m.mem_read(INFO,0x158)))
        self.m.mem_write(FORECAST_INFO+0x94,bytes(self.m.mem_read(INFO+0x26c,0x94)))
        self.m.reg_write(regs.UC_ARM64_REG_X21,FORECAST_INFO)
        total=self.call(0x40f98948,stop=0x40f9897c)
        return (self.m.reg_read(regs.UC_ARM64_REG_X19),
                self.m.reg_read(regs.UC_ARM64_REG_X20),total)

    def remaining(self,bytes):
        return self.call(0x4104d654,POOL,bytes)

    def free_units(self):
        # The separate native query deliberately passes zero to count units.
        return self.call(0x40f98a04,stop=0x40f98a10)

    def owner(self,resource):
        descriptor=self.u64(resource+8)
        return self.u64(descriptor),self.u32(descriptor+8)

    def footer(self,resource):
        raw,capacity=self.owner(resource)
        address=raw+0x400000000+capacity-LEASE_BYTES
        return struct.unpack(LEASE_FORMAT,self.m.mem_read(address,64))

    def mutable_footer(self,resource):
        raw,capacity=self.owner(resource)
        address=raw+0x400000000+capacity-LEASE_BYTES+64
        return bytes(self.m.mem_read(address,64))

    def normalized_metadata(self,resource):
        raw,capacity=self.owner(resource)
        data=bytearray(self.m.mem_read(resource+0x18,0xd50))
        for offset in range(0,len(data)-7,8):
            value=struct.unpack_from('<Q',data,offset)[0]
            if raw<=value<raw+capacity:
                struct.pack_into('<Q',data,offset,value-raw+0x100000000)
        return bytes(data)


@unittest.skipIf(Uc is None,'Unicorn is required for the native C3.11 output regression')
class PhotoLutNativeOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        supplied=os.environ.get('Z8_PHOTO_LUT_ROM')
        if not supplied:
            raise unittest.SkipTest('Set Z8_PHOTO_LUT_ROM to the original C3.11 z8-main-payload.bi')
        path=Path(supplied)
        if not path.is_file():
            raise unittest.SkipTest(f'Z8_PHOTO_LUT_ROM is unavailable: {path}')
        cls.rom=path.read_bytes()
        observed=hashlib.sha256(cls.rom).hexdigest()
        if observed!=ROM_SHA256:
            raise AssertionError(f'C3.11 payload SHA256 differs: {observed}')
        cls.code,cls.symbols,cls.metadata=compile_code()
        cubes=[]
        for cycle in (False,True):
            rows=[f'TITLE "{"Cycle" if cycle else "Identity"}"','LUT_3D_SIZE 2']
            for b in range(2):
                for g in range(2):
                    for r in range(2):
                        rows.append(' '.join(map(str,(g,b,r) if cycle else (r,g,b))))
            cubes.append(parse(('\n'.join(rows)+'\n').encode()))
        cls.looks,_=bundle(cubes,grid=17)

    def machine(self,**options):
        return NativeOutputMachine(self.rom,self.code,self.symbols,self.metadata,self.looks,**options)

    def table(self,address):
        return struct.unpack_from('<I',self.rom,address-DATA_BIAS-BASE)[0]

    def canonical_segments(self,format,dimension,quality):
        # Independent packing of published native capacities. The normal FX
        # cases below all exceed 1920x1080, so both thumbnail classes exist.
        jpeg=self.table(0xa02a2460c+dimension*12+quality*4)
        thumb=self.table(0xa02a24c0c+quality*4)
        small=self.table(0xa02a24c48+quality*4)
        raw=self.table(0xa02a244a4)
        preview=self.table(0xa02a2460c+8)
        tiny=0x3600
        segments={}
        cursor=0
        def append(pointer,capacity,bytes):
            nonlocal cursor
            cursor=align512(cursor)
            segments[pointer]=(cursor,bytes,capacity)
            cursor+=bytes
        append(0x40,0x30,jpeg)
        if format==0:
            append(0x68,0x50,raw)
        append(0xa8,0x98,thumb)
        append(0xc8,0xb8,small)
        append(0xe8,0xd8,tiny)
        if format==0:
            append(0x108,0xf8,preview)
            append(0x128,0x118,self.table(0xa02a24c0c))
            append(0x148,0x138,self.table(0xa02a24c48))
            append(0xe8,0xd8,tiny)
            append(0x170,None,0x22000)
            append(0x190,None,0x34200)
        return segments,cursor

    def test_actual_constructor_keeps_native_segments_outside_private_tail(self):
        cases=[(0,0,0),(0,0,2),(0,2,2),(0,1,1),(3,0,0),(3,2,2)]
        for format,dimension,quality in cases:
            with self.subTest(format=format,dimension=dimension,quality=quality):
                baseline=self.machine(patched=False)
                extended=self.machine()
                arguments=dict(format=format,dimension=dimension,quality=quality)
                baseline.configure(**arguments)
                extended.configure(**arguments)
                original=baseline.construct()
                resource=extended.construct()
                self.assertTrue(original and resource)
                self.assertEqual(extended.normalized_metadata(resource),baseline.normalized_metadata(original))
                expected,budget=self.canonical_segments(format,dimension,quality)
                self.assertEqual(extended.native_bytes,budget)
                raw,capacity=extended.owner(resource)
                lease=extended.footer(resource)
                self.assertEqual(lease,(LEASE_MAGIC,resource,raw,capacity,raw+align512(budget),SCRATCH,2,budget,0))
                self.assertEqual(extended.mutable_footer(resource),bytes(64))
                self.assertEqual(extended.u32(resource),42)
                self.assertEqual(extended.u32(resource+0x10),POOL)
                self.assertLessEqual(lease[4]+lease[5],raw+capacity-LEASE_BYTES)
                cursor=lease[4]
                for slice_bytes in SCRATCH_SLICES:
                    self.assertEqual(cursor&63,0)
                    self.assertLessEqual(cursor+slice_bytes,raw+capacity-LEASE_BYTES)
                    cursor+=slice_bytes
                for offset,(relative,segment_bytes,capacity_offset) in expected.items():
                    self.assertEqual(extended.u64(resource+offset),raw+relative)
                    if capacity_offset is not None:
                        self.assertEqual(extended.u32(resource+capacity_offset),segment_bytes)
                    # These native descriptor spans are the serializer's
                    # inputs; neither scratch nor its footer is one of them.
                    self.assertLessEqual(relative+segment_bytes,budget)
                    self.assertLessEqual(raw+relative+segment_bytes,lease[4])
                self.assertEqual(extended.call(0x410477c8,resource),1)
                self.assertEqual(extended.call(0x40cdafa0,POOL),1)

    def test_original_compressed_reuse_is_disabled_before_sizing_and_layout(self):
        stock=self.machine(patched=False,separate=False)
        separate=self.machine(patched=False)
        self.assertEqual(stock.call(0x410590dc,0,0,0,2,0),1)
        self.assertEqual(separate.call(0x410590dc,0,0,0,2,0),0)
        for machine in (stock,separate):
            machine.configure(quality=2)
        shared=stock.construct()
        split=separate.construct()
        self.assertEqual(stock.u64(shared+0x40),stock.u64(shared+0x108))
        self.assertNotEqual(separate.u64(split+0x40),separate.u64(split+0x108))
        self.assertEqual(separate.native_bytes-stock.native_bytes,self.table(0xa02a2460c+8))

    def test_off_identity_and_non_jpeg_outputs_keep_only_the_footer(self):
        for choice,format,alternate in [(0,0,0),(1,0,0),(2,1,0),(2,3,1)]:
            with self.subTest(choice=choice,format=format,alternate=alternate):
                machine=self.machine()
                machine.configure(choice=choice,format=format,alternate=alternate)
                resource=machine.construct()
                self.assertTrue(resource)
                lease=machine.footer(resource)
                self.assertEqual(lease[5],0)
                self.assertEqual(lease[6],choice)
                self.assertEqual(lease[7],machine.native_bytes)
                self.assertEqual(machine.mutable_footer(resource),bytes(64))
                self.assertGreaterEqual(lease[3],align512(lease[7])+LEASE_BYTES)
                self.assertEqual(machine.call(0x410477c8,resource),1)
                self.assertEqual(machine.call(0x40cdafa0,POOL),1)

    def test_forecast_matches_actual_admission_until_pool_is_full(self):
        # These pinned-ROM counterexamples previously advertised additional
        # photos after every enlarged output that could fit was already held.
        for format,quality,old_count,new_count in [(3,0,18,4),(0,0,5,2),(3,2,64,5)]:
            with self.subTest(format=format,quality=quality):
                machine=self.machine()
                machine.configure(format=format,quality=quality)
                _,native=self.canonical_segments(format,0,quality)
                required=align512(native)+SCRATCH+LEASE_BYTES
                unit=machine.u32(POOL_CONFIG+8)
                total_units=machine.u32(POOL_CONFIG+12)
                units=(required+unit-1)//unit
                self.assertEqual(machine.call(0x4104886c,INFO,INFO+0x26c,0),native)
                self.assertEqual(machine.remaining(native),old_count)
                self.assertEqual(machine.forecast(),(required,new_count,new_count))
                self.assertEqual(new_count,total_units//units)
                self.assertEqual(machine.free_units(),total_units)
                owners=[]
                for index in range(new_count):
                    resource=machine.construct()
                    self.assertTrue(resource)
                    owners.append(resource)
                    self.assertEqual(machine.owner(resource)[1],units*unit)
                    self.assertEqual(machine.footer(resource)[7],native)
                    self.assertEqual(machine.forecast(),(required,new_count-index-1,new_count))
                    self.assertEqual(machine.free_units(),total_units-(index+1)*units)
                self.assertGreater(machine.remaining(native),0)
                self.assertEqual(machine.construct(),0)
                self.assertEqual(len(machine.constructed),new_count)
                self.assertEqual(machine.forecast(),(required,0,new_count))
                for index,resource in enumerate(owners):
                    self.assertEqual(machine.call(0x41049504,resource),1)
                    self.assertEqual(machine.forecast(),(required,index+1,new_count))
                self.assertEqual(machine.remaining(native),old_count)
                self.assertEqual(machine.free_units(),total_units)
                self.assertEqual(machine.call(0x40cdafa0,POOL),1)

    def test_forecast_matches_minimal_footer_for_off_identity_and_non_jpeg(self):
        for choice,format,alternate in [(0,0,0),(1,0,0),(2,1,0),(2,3,1)]:
            with self.subTest(choice=choice,format=format,alternate=alternate):
                machine=self.machine()
                machine.configure(choice=choice,format=format,alternate=alternate)
                native=machine.call(0x4104886c,INFO,INFO+0x26c,0)
                required=align512(native)+LEASE_BYTES
                unit=machine.u32(POOL_CONFIG+8)
                total_units=machine.u32(POOL_CONFIG+12)
                units=(required+unit-1)//unit
                count=total_units//units
                self.assertEqual(machine.forecast(),(required,count,count))
                self.assertEqual(machine.free_units(),total_units)
                resource=machine.construct()
                self.assertTrue(resource)
                self.assertEqual(machine.owner(resource)[1],units*unit)
                self.assertEqual(machine.footer(resource)[5:8],(0,choice,native))
                self.assertEqual(machine.forecast(),(required,count-1,count))
                self.assertEqual(machine.free_units(),total_units-units)
                self.assertEqual(machine.call(0x41049504,resource),1)
                self.assertEqual(machine.forecast(),(required,count,count))
                self.assertEqual(machine.free_units(),total_units)

    def test_prospective_choice_preserves_allocated_look_and_zero_size_unit_query(self):
        machine=self.machine()
        machine.configure(format=3)
        _,native=self.canonical_segments(3,0,0)
        resource=machine.construct()
        self.assertTrue(resource)
        lease=machine.footer(resource)
        mutable=machine.mutable_footer(resource)
        self.assertEqual(lease[5:8],(SCRATCH,2,native))
        unit=machine.u32(POOL_CONFIG+8)
        total_units=machine.u32(POOL_CONFIG+12)
        free_units=total_units-machine.owner(resource)[1]//unit
        for choice in (0,1,2):
            with self.subTest(prospective_choice=choice):
                machine.m.mem_write(0xa03d482ba,bytes([choice]))
                required=align512(native)+LEASE_BYTES+(SCRATCH if choice==2 else 0)
                units=(required+unit-1)//unit
                self.assertEqual(machine.forecast(),(required,free_units//units,total_units//units))
                self.assertEqual(machine.free_units(),free_units)
                self.assertEqual(machine.footer(resource),lease)
                self.assertEqual(machine.mutable_footer(resource),mutable)
                self.assertEqual(machine.call(0x4104886c,INFO,INFO+0x26c,0),native)
        self.assertEqual(machine.call(0x41049504,resource),1)
        self.assertEqual(machine.free_units(),total_units)

    def test_clamped_and_exhausted_native_allocations_return_null_before_layout(self):
        clamped=self.machine(pool_units=1)
        clamped.configure(format=3)
        self.assertEqual(clamped.forecast()[1:],(0,0))
        self.assertEqual(clamped.construct(),0)
        self.assertEqual(len(clamped.allocations),1)
        self.assertEqual(clamped.frees,[clamped.allocations[0][0]])
        self.assertEqual(clamped.constructed,[])
        self.assertEqual(clamped.call(0x40cdafa0,POOL),1)
        full=self.machine()
        total=full.u32(POOL_CONFIG+4)
        held=full.call(0x41047478,17,POOL,total)
        self.assertTrue(held)
        full.configure(format=3)
        self.assertEqual(full.construct(),0)
        self.assertEqual(full.constructed,[])
        self.assertEqual(full.frees,[])
        self.assertEqual(full.u32(held),17)
        self.assertEqual(full.call(0x410477c8,held),1)
        result=full.construct()
        self.assertTrue(result)
        self.assertEqual(full.footer(result)[5],SCRATCH)
        self.assertEqual(full.mutable_footer(result),bytes(64))
        self.assertEqual(full.call(0x410477c8,result),1)
        self.assertEqual(full.call(0x40cdafa0,POOL),1)


if __name__=='__main__':
    unittest.main()
