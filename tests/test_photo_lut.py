"""Independent cube-format, native interpolation and image-boundary checks."""
import ctypes as C
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import unittest
import zlib

from z8photolut.cube import parse
from z8photolut.library import validate_runtime
from photo_lut_fixtures import runtime_bundle

ROOT=Path(__file__).resolve().parents[1]

def cube_text(size=2,cycle=False):
    rows=['TITLE "Cycle"' if cycle else 'TITLE "Identity"',f'LUT_3D_SIZE {size}']
    for b in range(size):
        for g in range(size):
            for r in range(size):
                values=(g,b,r) if cycle else (r,g,b)
                rows.append(' '.join(str(v/(size-1)) for v in values))
    return ('\n'.join(rows)+'\n').encode()

class Look(C.Structure):
    _fields_=[('samples',C.POINTER(C.c_uint16)),('grid',C.c_uint32),('identity',C.c_uint32),('strength',C.c_uint32)]

class Surface(C.Structure):
    _fields_=[('y',C.POINTER(C.c_uint8)),('uv',C.POINTER(C.c_uint8)),('y_bytes',C.c_uint64),('uv_bytes',C.c_uint64),
              ('width',C.c_uint32),('height',C.c_uint32),('y_stride',C.c_uint32),('uv_stride',C.c_uint32),('uv_divisor',C.c_uint32)]

class PhotoLutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='photo-lut-test-')
        lib=Path(cls.tmp.name)/'lut.so'
        subprocess.run(['cc','-std=c11','-O2','-Wall','-Wextra','-Werror','-shared','-fPIC',str(ROOT/'z8photolut/src/lut.c'),'-o',str(lib)],check=True)
        cls.lib=C.CDLL(str(lib))
        cls.lib.z8_lut_validate.argtypes=[C.c_void_p,C.c_uint32]
        cls.lib.z8_lut_validate.restype=C.c_uint
        cls.lib.z8_lut_rgb.argtypes=[C.POINTER(Look),C.POINTER(C.c_uint8),C.POINTER(C.c_uint8)]
        cls.lib.z8_lut_surface.argtypes=[C.POINTER(Surface),C.POINTER(Look)]
        cls.lib.z8_lut_surface.restype=C.c_uint
        cls.lib.z8_lut_preview_build.argtypes=[C.POINTER(Look),C.POINTER(C.c_uint64),C.c_uint32]
        cls.lib.z8_lut_preview_surface.argtypes=[C.POINTER(Surface),C.POINTER(C.c_uint64),C.c_uint32]
        cls.lib.z8_lut_surface_cancel.argtypes=cls.lib.z8_lut_surface.argtypes+[C.POINTER(C.c_uint32)]
        cls.lib.z8_lut_preview_build_cancel.argtypes=cls.lib.z8_lut_preview_build.argtypes+[C.POINTER(C.c_uint32)]
        cls.lib.z8_lut_preview_surface_cancel.argtypes=cls.lib.z8_lut_preview_surface.argtypes+[C.POINTER(C.c_uint32)]
        cls.identity=runtime_bundle([cube_text()])
        cls.cycle=runtime_bundle([cube_text(cycle=True)])

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def native_look(self,raw):
        storage=C.create_string_buffer(raw)
        at,_,grid,identity=struct.unpack_from('<4I',raw,64)
        look=Look(C.cast(C.addressof(storage)+at,C.POINTER(C.c_uint16)),grid,identity,256)
        return storage,look

    def test_cube_order_domains_and_comments(self):
        c=parse(cube_text(cycle=True))
        for observed,expected in zip(c.sample((.2,.4,.7)),(.4,.7,.2)):
            self.assertAlmostEqual(observed,expected,places=14)
        raw=cube_text().replace(b'TITLE "Identity"',b'TITLE "Identity # title" # comment\nDOMAIN_MIN -1 -1 -1\nDOMAIN_MAX 1 1 1')
        self.assertEqual(parse(raw).sample((0,0,0)),(.5,.5,.5))
        for raw in [cube_text()+b'0 0 0\n',cube_text().replace(b'LUT_3D_SIZE 2',b'LUT_3D_SIZE 0'),
                    cube_text().replace(b'0.0 0.0 0.0',b'nan 0 0'),cube_text()+b'DOMAIN_MIN 0 0 0\n',
                    cube_text().replace(b'LUT_3D_SIZE 2',b'LUT_1D_SIZE 2'),
                    cube_text().replace(b'LUT_3D_SIZE 2',b'LUT_3D_SIZE 2\nDOMAIN_MAX 0 1 1')]:
            with self.subTest(raw=raw[:80]),self.assertRaises(ValueError):parse(raw)

    def test_bundle_corruption_and_bounds(self):
        for raw in [self.identity,self.cycle]:
            self.assertEqual(validate_runtime(raw)['count'],1)
            storage=C.create_string_buffer(raw)
            self.assertEqual(self.lib.z8_lut_validate(storage,len(raw)),0)
            # Header magic/version/size/count/default/CRC, entry fields, name,
            # identity digest, entry padding and samples: both readers reject.
            for at in [0,8,12,16,20,24,32,63,64,68,72,76,80,84,88,150,152,183,184,len(raw)-1]:
                corrupt=bytearray(raw);corrupt[at]^=0x40
                with self.subTest(at=at),self.assertRaises(ValueError): validate_runtime(corrupt)
                copy=C.create_string_buffer(bytes(corrupt))
                self.assertNotEqual(self.lib.z8_lut_validate(copy,len(raw)),0)
            # The camera writes its own session tag into a published library;
            # a host file must carry tag 0.
            for at in [28]:
                corrupt=bytearray(raw);corrupt[at]^=0x40
                with self.subTest(at=at),self.assertRaises(ValueError): validate_runtime(corrupt)
                self.assertEqual(self.lib.z8_lut_validate(C.create_string_buffer(bytes(corrupt)),len(raw)),0)
        # Valid checksums cannot legitimize an overlapping/truncated sample span.
        corrupt=bytearray(self.cycle);struct.pack_into('<I',corrupt,64,64)
        struct.pack_into('<I',corrupt,24,zlib.crc32(corrupt[64:]))
        self.assertNotEqual(self.lib.z8_lut_validate(C.create_string_buffer(bytes(corrupt)),len(corrupt)),0)

    def test_native_tetrahedral_independent_oracle(self):
        rng=random.Random(417)
        grid=17
        values=[rng.randrange(65536) for _ in range(grid**3*3)]
        table=(C.c_uint16*len(values))(*values)
        look=Look(table,grid,0,256)
        for _ in range(6000):
            rgb=tuple(rng.randrange(256) for _ in range(3))
            coords=[v*(grid-1)/255 for v in rgb]
            lows=[min(grid-2,int(v)) for v in coords]
            frac=[v-lo for v,lo in zip(coords,lows)]
            order=sorted(range(3),key=lambda c:frac[c],reverse=True)
            vertices=[lows.copy()]
            for axis in order:
                v=vertices[-1].copy();v[axis]+=1;vertices.append(v)
            weights=[1-frac[order[0]],frac[order[0]]-frac[order[1]],frac[order[1]]-frac[order[2]],frac[order[2]]]
            expected=[]
            for c in range(3):
                value=sum(w*values[((r*grid+g)*grid+b)*3+c] for (r,g,b),w in zip(vertices,weights))/257
                expected.append(int(value+.5))
            output=(C.c_uint8*3)()
            self.lib.z8_lut_rgb(C.byref(look),(C.c_uint8*3)(*rgb),output)
            self.assertEqual(list(output),expected)

    def test_surface_identity_padding_and_invalid_no_write(self):
        for divisor in [1,2]:
            width,height,stride=30,18,40
            y=(C.c_uint8*(height*stride))(*[(i*29)%256 for i in range(height*stride)])
            uv=(C.c_uint8*(height//divisor*stride))(*[(i*17)%256 for i in range(height//divisor*stride)])
            f=Surface(y,uv,len(y),len(uv),width,height,stride,stride,divisor)
            y_before,uv_before=bytes(y),bytes(uv)
            storage,identity=self.native_look(self.identity)
            self.assertEqual(self.lib.z8_lut_surface(C.byref(f),C.byref(identity)),0)
            self.assertEqual((bytes(y),bytes(uv)),(y_before,uv_before))
            storage,look=self.native_look(self.cycle)
            f.y_bytes-=1
            self.assertNotEqual(self.lib.z8_lut_surface(C.byref(f),C.byref(look)),0)
            self.assertEqual((bytes(y),bytes(uv)),(y_before,uv_before))
            f.y_bytes+=1
            self.assertEqual(self.lib.z8_lut_surface(C.byref(f),C.byref(look)),0)
            self.assertNotEqual(bytes(y),y_before)
            for row in range(height):self.assertEqual(bytes(y)[row*stride+width:(row+1)*stride],y_before[row*stride+width:(row+1)*stride])
            for row in range(height//divisor):self.assertEqual(bytes(uv)[row*stride+width:(row+1)*stride],uv_before[row*stride+width:(row+1)*stride])

    def test_cancelled_work_never_writes_a_surface_or_table(self):
        storage,look=self.native_look(self.cycle)
        y=(C.c_uint8*64)(*range(64));uv=(C.c_uint8*32)(*range(32))
        surface=Surface(y,uv,64,32,8,8,8,8,2)
        cache=(C.c_uint64*(256*65*65))()
        C.memset(cache,0x59,C.sizeof(cache))
        before=(bytes(y),bytes(uv),bytes(cache))
        cancel=C.c_uint32(1)
        self.assertEqual(self.lib.z8_lut_surface_cancel(C.byref(surface),C.byref(look),C.byref(cancel)),6)
        self.assertEqual(self.lib.z8_lut_preview_build_cancel(C.byref(look),cache,C.sizeof(cache),C.byref(cancel)),6)
        self.assertEqual(self.lib.z8_lut_preview_surface_cancel(C.byref(surface),cache,C.sizeof(cache),C.byref(cancel)),6)
        self.assertEqual((bytes(y),bytes(uv),bytes(cache)),before)

    def test_preview_cache_matches_rgb_reference_with_bounded_quantization(self):
        storage,look=self.native_look(self.cycle)
        nodes=256*65*65
        cache=(C.c_uint64*nodes)()
        self.assertEqual(self.lib.z8_lut_preview_build(C.byref(look),cache,C.sizeof(cache)),0)
        self.assertNotEqual(self.lib.z8_lut_preview_build(C.byref(look),cache,C.sizeof(cache)-8),0)
        rng=random.Random(661)
        for divisor in [1,2]:
            width,height,stride=256,128,272
            raw_y=bytes(rng.randrange(256) for _ in range(stride*height))
            raw_uv=bytes(rng.randrange(256) for _ in range(stride*height//divisor))
            exact_y=(C.c_uint8*len(raw_y)).from_buffer_copy(raw_y)
            exact_uv=(C.c_uint8*len(raw_uv)).from_buffer_copy(raw_uv)
            fast_y=(C.c_uint8*len(raw_y)).from_buffer_copy(raw_y)
            fast_uv=(C.c_uint8*len(raw_uv)).from_buffer_copy(raw_uv)
            exact=Surface(exact_y,exact_uv,len(raw_y),len(raw_uv),width,height,stride,stride,divisor)
            fast=Surface(fast_y,fast_uv,len(raw_y),len(raw_uv),width,height,stride,stride,divisor)
            self.assertEqual(self.lib.z8_lut_surface(C.byref(exact),C.byref(look)),0)
            self.assertEqual(self.lib.z8_lut_preview_surface(C.byref(fast),cache,C.sizeof(cache)),0)
            delta=sorted(abs(a-b) for plane_a,plane_b in [(exact_y,fast_y),(exact_uv,fast_uv)] for a,b in zip(plane_a,plane_b))
            self.assertLessEqual(max(delta),4)
            self.assertLessEqual(delta[len(delta)*99//100],2)
            for y in range(height):self.assertEqual(bytes(fast_y)[y*stride+width:(y+1)*stride],raw_y[y*stride+width:(y+1)*stride])
            for y in range(height//divisor):self.assertEqual(bytes(fast_uv)[y*stride+width:(y+1)*stride],raw_uv[y*stride+width:(y+1)*stride])


if __name__=='__main__':unittest.main()
