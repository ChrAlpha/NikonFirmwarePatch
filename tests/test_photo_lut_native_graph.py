"""Execute C3.11's per-file graph and native thumbnail header consumers.

Opt in with Z8_PHOTO_LUT_ROM=/path/to/z8-main-payload.bi. The pinned ROM is
supplied locally and is never a repository fixture. The real graph builder,
JPEGInterchangeFormatLength callback, thumbnail-copy basic block, and cached
address translation execute in Unicorn; cache maintenance and memcpy are
isolated. These checks do not establish physical cache/DMA behaviour, complete
camera output, or asynchronous lifetime safety.
"""
import hashlib
import os
from pathlib import Path
import struct
import unittest

from z8photolut.cube import bundle,parse
from z8photolut.patcher import BASE,CODE,LUT,branch,compile_code

try:
    from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    Uc=None


ROM_SHA256='2b43a31c9de838f84b12416de4348a7004e65be07f853ec56ec2b10f3a8e3853'
DATA_BIAS=0x9bdb95000
STOP=0x60000000
STATE=0xa101f0000
CAPTURE=0xa11000000
CONFIG=CAPTURE+0x1000
OWNER=CAPTURE+0x6000
HEADER_OWNER=CAPTURE+0x7000
HEADER_RESOURCE=CAPTURE+0x8000
# Native file jobs come from pool 0x23 and expose their raw image-RAM
# address (40e9dd68), without conversion to a cached or main-RAM alias.
FILE_JOB=0x8427a6000
JPEG_GRAPH=FILE_JOB+0xc0
NEF_GRAPH=FILE_JOB+0x1000
PRIVATE_DESCRIPTOR=CAPTURE+0xb000
HEADER_DESTINATION=CAPTURE+0xc000
IFD_ENTRY=CAPTURE+0xd000
OUTPUT_RESOURCE=CAPTURE+0xe000
RAW=0x800000000
CACHED=RAW+0x400000000
ORIGINAL_THUMBNAIL=b'original NEF thumbnail\xff\xd9'
PRIVATE_THUMBNAIL=b'private JPEG thumbnail with a different length\xff\xd9'


class NativeGraphMachine:
    def __init__(self,rom):
        self.m=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        self.m.mem_map(BASE,(len(rom)+4095)&~4095)
        self.m.mem_write(BASE,rom)
        self.m.mem_map(0xa02000000,0x2000000)
        source=0x44c6b000
        self.m.mem_write(source+DATA_BIAS,rom[source-BASE:])
        self.m.mem_map(CAPTURE,0x100000)
        self.m.mem_map(FILE_JOB,0x4000)
        self.m.mem_map(0xa0497d000,0x1000)
        self.m.mem_map(0x50000000,0x100000)
        self.m.mem_map(STOP,0x1000)
        self.m.mem_map(CACHED,0x10000)
        self.m.mem_write(0xa0497d030,struct.pack('<QIIQII',RAW,1,0,0xa00000000,0,0))
        self.write64(CAPTURE+0x790,CONFIG)
        self.write64(CAPTURE+0x6a8,OWNER)
        self.write64(HEADER_OWNER+8,HEADER_RESOURCE)
        self.write64(HEADER_RESOURCE,RAW+0x8000)
        self.descriptor(OWNER+0xe0,RAW,ORIGINAL_THUMBNAIL)
        self.descriptor(PRIVATE_DESCRIPTOR,RAW+0x1000,PRIVATE_THUMBNAIL)
        self.m.mem_write(CACHED,ORIGINAL_THUMBNAIL)
        self.m.mem_write(CACHED+0x1000,PRIVATE_THUMBNAIL)
        self.m.hook_add(UC_HOOK_CODE,self._instruction)

    def write64(self,address,value):
        self.m.mem_write(address,struct.pack('<Q',value))

    def write32(self,address,value):
        self.m.mem_write(address,struct.pack('<I',value))

    def u64(self,address):
        return struct.unpack('<Q',self.m.mem_read(address,8))[0]

    def u32(self,address):
        return struct.unpack('<I',self.m.mem_read(address,4))[0]

    def descriptor(self,address,raw,payload):
        self.m.mem_write(address,struct.pack('<IHHQII',1,160,120,raw,len(payload),0))

    def _instruction(self,machine,address,size,user):
        if address==0x40d2b940:
            pass
        elif address==0x4127a900:
            target=machine.reg_read(regs.UC_ARM64_REG_X0)
            source=machine.reg_read(regs.UC_ARM64_REG_X1)
            length=machine.reg_read(regs.UC_ARM64_REG_X2)
            machine.mem_write(target,bytes(machine.mem_read(source,length)))
        else:
            return
        machine.reg_write(regs.UC_ARM64_REG_PC,machine.reg_read(regs.UC_ARM64_REG_X30))

    def call(self,address,*arguments,stop=STOP):
        self.m.reg_write(regs.UC_ARM64_REG_SP,0x500f0000)
        self.m.reg_write(regs.UC_ARM64_REG_X30,stop)
        for index,value in enumerate(arguments):
            self.m.reg_write(getattr(regs,f'UC_ARM64_REG_X{index}'),value)
        self.m.emu_start(address,stop,count=1_000_000)
        if self.m.reg_read(regs.UC_ARM64_REG_PC)!=stop:
            raise AssertionError(f'Native execution did not finish at {stop:#x}')
        return self.m.reg_read(regs.UC_ARM64_REG_X0)

    def graph(self,file_id,graph):
        return self.call(0x40eb17e0,file_id,CAPTURE,OWNER,HEADER_OWNER,graph)

    def thumbnail_header(self,graph,*,nef=False):
        # ROM table 440cc930 binds Exif tag 0202 to this native callback.
        self.m.mem_write(IFD_ENTRY,bytes(12))
        self.call(0x406e4d9c,IFD_ENTRY,graph+0x48,0)
        length=self.u32(IFD_ENTRY+8)
        if length>4096:
            raise AssertionError('Fixture thumbnail length exceeds its destination')
        self.m.mem_write(HEADER_DESTINATION,bytes(4096))
        # Execute each format's actual copy block. Both translate descriptor+8,
        # invalidate descriptor+10 bytes and copy that exact byte count.
        if nef:
            self.m.reg_write(regs.UC_ARM64_REG_X21,graph)
            self.m.reg_write(regs.UC_ARM64_REG_X23,HEADER_DESTINATION)
            self.call(0x406c390c,stop=0x406c3938)
        else:
            self.m.reg_write(regs.UC_ARM64_REG_X19,graph+0x48)
            self.m.reg_write(regs.UC_ARM64_REG_X22,HEADER_DESTINATION)
            self.call(0x406c3524,stop=0x406c3554)
        return length,bytes(self.m.mem_read(HEADER_DESTINATION,length))


class CompiledGraphMachine(NativeGraphMachine):
    def __init__(self,rom,code,symbols,metadata,looks,*,choice=2):
        super().__init__(rom)
        self.symbols=symbols
        self.layout=metadata['runtime_layout']
        self.cached_pages=set(range(CACHED,CACHED+0x10000,4096))
        self.m.mem_map(STATE,0x2000)
        self.m.mem_write(CODE,code)
        self.m.mem_write(LUT,looks)
        self.m.mem_write(STATE,struct.pack('<Q6I',0x32545354554c385a,2,
            metadata['runtime_layout']['state_bytes'],choice,256,1,0))
        self.m.mem_write(0xa03d482ba,bytes([choice]))
        # Derive capacity and immutable lease fields by running the real LUT
        # allocator, isolating only the native pool allocation operation.
        allocated=self.call(symbols['z8_photo_lut_output_alloc'],42,0x38,0x20000,CONFIG)
        if allocated!=OWNER:
            raise AssertionError('Compiled allocator did not initialize the fixture owner')
        self.footer=CACHED+self.capacity-128
        self.private_descriptor=self.footer+64

    def map_cached(self,address,bytes):
        first=address&~4095
        end=(address+bytes+4095)&~4095
        for page in range(first,end,4096):
            if page not in self.cached_pages:
                self.m.mem_map(page,4096)
                self.cached_pages.add(page)

    def _instruction(self,machine,address,size,user):
        if address==0x41047478:
            requested=machine.reg_read(regs.UC_ARM64_REG_X2)
            self.capacity=(requested+511)&~511
            self.write64(OWNER+8,OUTPUT_RESOURCE)
            self.m.mem_write(OUTPUT_RESOURCE,struct.pack('<QI',RAW,self.capacity))
            self.map_cached(CACHED+self.capacity-128,128)
            machine.reg_write(regs.UC_ARM64_REG_X0,OWNER)
            machine.reg_write(regs.UC_ARM64_REG_PC,machine.reg_read(regs.UC_ARM64_REG_X30))
            return
        super()._instruction(machine,address,size,user)

    def finish_thumbnail(self):
        scratch=self.u64(self.footer+32)
        storage_bytes=self.u32(self.footer+40)
        if not storage_bytes:
            raise AssertionError('No private thumbnail storage was reserved')
        # This is the publication contract of the completed private encode.
        # Pixel conversion and encoding are covered by their own regressions.
        raw=scratch+storage_bytes-0x3600
        cached=raw+0x400000000
        self.map_cached(cached,len(PRIVATE_THUMBNAIL))
        self.m.mem_write(cached,PRIVATE_THUMBNAIL)
        self.descriptor(self.private_descriptor,raw,PRIVATE_THUMBNAIL)
        self.write32(self.footer+112,1)

    def compiled_graph(self,file_id,graph,callsite):
        wrapper=self.symbols['z8_photo_lut_file_graph']
        self.m.mem_write(callsite,branch(callsite,wrapper,True))
        return self.call(callsite,file_id,CAPTURE,OWNER,HEADER_OWNER,graph,stop=callsite+4)

    def diagnostics(self):
        # The diagnostic tail follows jpeg_busy/cache_cleanup_lock in the
        # exported runtime layout; it is separate from native image-job locks.
        tail=STATE+self.layout['jpeg_busy_offset']
        return self.u64(tail+24),self.u32(tail+32)


@unittest.skipIf(Uc is None,'Unicorn is required for native C3.11 graph checks')
class PinnedRomTestCase(unittest.TestCase):
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


class PhotoLutNativeGraphTests(PinnedRomTestCase):
    def test_stock_graphs_share_the_original_thumbnail(self):
        machine=NativeGraphMachine(self.rom)
        self.assertEqual(machine.graph(0,JPEG_GRAPH),0)
        self.assertEqual(machine.graph(2,NEF_GRAPH),1)
        for graph in (JPEG_GRAPH,NEF_GRAPH):
            self.assertEqual(machine.u64(graph+0x18),OWNER+0xe0)
            self.assertEqual(machine.thumbnail_header(graph,nef=graph==NEF_GRAPH),
                (len(ORIGINAL_THUMBNAIL),ORIGINAL_THUMBNAIL))

    def test_per_file_override_selects_matching_bytes_and_exif_length(self):
        machine=NativeGraphMachine(self.rom)
        machine.graph(0,JPEG_GRAPH)
        machine.graph(2,NEF_GRAPH)
        owner_before=bytes(machine.m.mem_read(OWNER,0xd68))
        nef_before=bytes(machine.m.mem_read(NEF_GRAPH,0x2d8))
        machine.write64(JPEG_GRAPH+0x18,PRIVATE_DESCRIPTOR)
        self.assertNotEqual(len(PRIVATE_THUMBNAIL),len(ORIGINAL_THUMBNAIL))
        self.assertEqual(machine.thumbnail_header(JPEG_GRAPH),
            (len(PRIVATE_THUMBNAIL),PRIVATE_THUMBNAIL))
        self.assertEqual(machine.thumbnail_header(NEF_GRAPH,nef=True),
            (len(ORIGINAL_THUMBNAIL),ORIGINAL_THUMBNAIL))
        self.assertEqual(bytes(machine.m.mem_read(OWNER,0xd68)),owner_before)
        self.assertEqual(bytes(machine.m.mem_read(NEF_GRAPH,0x2d8)),nef_before)

    def test_native_kind_distinguishes_jpeg_nef_and_ndf(self):
        machine=NativeGraphMachine(self.rom)
        for file_id,kind in ((0,0),(2,1),(4,0)):
            with self.subTest(file_id=file_id,ndf=False):
                self.assertEqual(machine.graph(file_id,JPEG_GRAPH),kind)
        machine.m.mem_write(CONFIG+0x130,b'\x01')
        for file_id in (0,4):
            with self.subTest(file_id=file_id,ndf=True):
                self.assertEqual(machine.graph(file_id,JPEG_GRAPH),4)


@unittest.skipIf(Uc is None,'Unicorn is required for compiled C3.11 graph checks')
class PhotoLutCompiledGraphTests(PinnedRomTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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
        return CompiledGraphMachine(self.rom,self.code,self.symbols,self.metadata,self.looks,**options)

    def test_both_native_callsites_keep_jpeg_and_nef_descriptors_independent(self):
        for callsite in (0x40eb1784,0x40eb1dc0):
            for file_id in (0,4):
                with self.subTest(callsite=hex(callsite),file_id=file_id):
                    machine=self.machine()
                    machine.finish_thumbnail()
                    owner_before=bytes(machine.m.mem_read(OWNER,0xd68))
                    self.assertEqual(machine.compiled_graph(2,NEF_GRAPH,callsite),1)
                    nef_before=bytes(machine.m.mem_read(NEF_GRAPH,0x2d8))
                    self.assertEqual(machine.compiled_graph(file_id,JPEG_GRAPH,callsite),0)
                    self.assertEqual(machine.u64(JPEG_GRAPH+0x18),machine.private_descriptor)
                    self.assertEqual(machine.thumbnail_header(JPEG_GRAPH),
                        (len(PRIVATE_THUMBNAIL),PRIVATE_THUMBNAIL))
                    self.assertEqual(machine.thumbnail_header(NEF_GRAPH,nef=True),
                        (len(ORIGINAL_THUMBNAIL),ORIGINAL_THUMBNAIL))
                    self.assertEqual(bytes(machine.m.mem_read(OWNER,0xd68)),owner_before)
                    self.assertEqual(bytes(machine.m.mem_read(NEF_GRAPH,0x2d8)),nef_before)

    def test_ndf_and_unpublished_thumbnail_retain_the_native_descriptor(self):
        for ndf in (False,True):
            with self.subTest(ndf=ndf):
                machine=self.machine()
                if ndf:
                    machine.finish_thumbnail()
                    machine.m.mem_write(CONFIG+0x130,b'\x01')
                self.assertEqual(machine.compiled_graph(0,JPEG_GRAPH,0x40eb1784),4 if ndf else 0)
                self.assertEqual(machine.u64(JPEG_GRAPH+0x18),OWNER+0xe0)
                rejected,error=machine.diagnostics()
                self.assertEqual(rejected,0 if ndf else 1)
                self.assertEqual(error==0,ndf)

    def test_invalid_lease_records_rejection_and_preserves_native_thumbnail(self):
        machine=self.machine()
        machine.finish_thumbnail()
        machine.write64(machine.footer,0)
        owner_before=bytes(machine.m.mem_read(OWNER,0xd68))
        self.assertEqual(machine.compiled_graph(0,JPEG_GRAPH,0x40eb1dc0),0)
        self.assertEqual(machine.u64(JPEG_GRAPH+0x18),OWNER+0xe0)
        self.assertEqual(machine.thumbnail_header(JPEG_GRAPH),
            (len(ORIGINAL_THUMBNAIL),ORIGINAL_THUMBNAIL))
        rejected,error=machine.diagnostics()
        self.assertEqual(rejected,1)
        self.assertNotEqual(error,0)
        self.assertEqual(bytes(machine.m.mem_read(OWNER,0xd68)),owner_before)

    def test_off_and_identity_keep_the_original_header_bytes_and_length(self):
        for choice in (0,1):
            with self.subTest(choice=choice):
                machine=self.machine(choice=choice)
                self.assertEqual(machine.compiled_graph(0,JPEG_GRAPH,0x40eb1dc0),0)
                self.assertEqual(machine.u64(JPEG_GRAPH+0x18),OWNER+0xe0)
                self.assertEqual(machine.thumbnail_header(JPEG_GRAPH),
                    (len(ORIGINAL_THUMBNAIL),ORIGINAL_THUMBNAIL))
                self.assertEqual(machine.diagnostics(),(0,0))


if __name__=='__main__':
    unittest.main()
