"""Execute the actual AArch64 bank trampolines and verify the native ABI."""
import random
from pathlib import Path
import json
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

from z8photolut.patcher import compile_code,CODE
from z8photolut import patcher


class PhotoLutPrebuiltTests(unittest.TestCase):
    def test_packaged_code_is_checked_without_compiling_or_fallback(self):
        code,symbols,native=compile_code()
        with tempfile.TemporaryDirectory(prefix='photo-lut-prebuilt-') as folder:
            root=Path(folder);source=root/'z8photolut';resources=root/'resources'
            shutil.copytree(patcher.ROOT/'src',source/'src');resources.mkdir()
            (resources/'photo-lut.bin').write_bytes(code)
            profile=dict(schema=1,native=native,symbols={k:hex(v) for k,v in symbols.items()})
            (resources/'photo-lut.json').write_text(json.dumps(profile))
            with patch.object(patcher,'ROOT',source),patch.object(patcher,'compile_code',side_effect=AssertionError('Unexpected compiler')):
                self.assertEqual(patcher.load_code('prebuilt'),(code,symbols,native))
                (resources/'photo-lut.bin').write_bytes(code[:-1]+bytes([code[-1]^1]))
                with self.assertRaisesRegex(ValueError,'digest differs'):patcher.load_code('prebuilt')
                (resources/'photo-lut.bin').write_bytes(code)
                (source/'src/lut.h').write_text('changed')
                with self.assertRaisesRegex(ValueError,'sources differ'):patcher.load_code('prebuilt')

try:
    from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    Uc=None


@unittest.skipIf(Uc is None,'Unicorn is required for AArch64 hook ABI checks')
class PhotoLutHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code,cls.symbols,cls.metadata=compile_code()

    def execute(self,name,identifier,length,flags,descriptor=0x4400bb98):
        machine=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        machine.mem_map(CODE,65536)
        machine.mem_write(CODE,self.code)
        machine.mem_map(0x4400b000,4096)
        machine.mem_map(0x60000000,8192)
        machine.mem_write(descriptor+4,struct.pack('<I',length))
        resume=self.symbols[f'z8_photo_lut_{name}_resume']
        machine.mem_map(resume&~4095,4096)
        rng=random.Random(892)
        before=[rng.randrange(1<<64) for _ in range(31)]
        before[8 if name!='current_default' else 9]=descriptor
        before[20]=identifier
        ids=[getattr(regs,f'UC_ARM64_REG_X{i}') for i in range(31)]
        for register,value in zip(ids,before):machine.reg_write(register,value)
        sp=0x60001800
        machine.reg_write(regs.UC_ARM64_REG_SP,sp)
        machine.reg_write(regs.UC_ARM64_REG_NZCV,flags)
        machine.emu_start(self.symbols[f'z8_photo_lut_{name}_entry'],resume,count=80)
        qualified=descriptor==0x4400bb98 if name=='current_default' else identifier&65535==0x600
        before[2]=8 if qualified and length==6 else length
        self.assertEqual(machine.reg_read(regs.UC_ARM64_REG_PC),resume)
        self.assertEqual(machine.reg_read(regs.UC_ARM64_REG_SP),sp)
        self.assertEqual(machine.reg_read(regs.UC_ARM64_REG_NZCV),flags)
        self.assertEqual([machine.reg_read(i) for i in ids],before)

    def test_copy_and_bank_reset_preserve_registers_and_flags(self):
        for name in ['bank','bank_default']:
            for identifier in [0x02000600,0x06000600,0x00000601,0x0200120b]:
                for length in [0,1,6,7,1024]:
                    for flags in [0,0x50000000,0xa0000000,0xf0000000]:
                        with self.subTest(name=name,identifier=identifier,length=length,flags=flags):
                            self.execute(name,identifier,length,flags)

    def test_current_default_uses_descriptor_identity(self):
        for descriptor in [0x4400bb98,0x4400bbb0]:
            for length in [0,6,7]:
                for flags in [0,0x50000000,0xa0000000,0xf0000000]:
                    with self.subTest(descriptor=descriptor,length=length,flags=flags):
                        self.execute('current_default',0x12345678,length,flags,descriptor)


@unittest.skipIf(Uc is None,'Unicorn is required for AArch64 queue ABI checks')
class PhotoLutQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code,cls.symbols,cls.metadata=compile_code()

    def fixture(self,channel=0,ready=True):
        m=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        state=0xa101f0000;context=0xa03000000;request=context+0x1000;stack=context+0x11800
        for at,size in [(CODE,65536),(0x409a0000,4096),(state,8192),(0xa03d48000,4096),
                        (context,0x20000),(0x40cd7000,4096),(0x40d2c000,4096),(0x40d2d000,4096),
                        (0x40e9a000,4096),(0x65000000,4096),(0xa02810000,4096),
                        (0xa02e70000,8192),(0xa02c90000,4096),(0xa03f31000,4096),(0x41047000,4096)]:m.mem_map(at,size)
        m.mem_write(CODE,self.code)
        layout=self.metadata['runtime_layout']
        m.mem_write(state,struct.pack('<Q6I',0x32545354554c385a,2,layout['state_bytes'],1,256,1,0))
        m.mem_write(0x409a0010,struct.pack('<I',2))
        m.mem_write(0xa03d482ba,b'\x01')
        m.mem_write(0xa02810470,struct.pack('<I',1 if channel==3 else 0))
        m.mem_write(context+0x10,struct.pack('<I',6))
        original=bytearray((i*29)&255 for i in range(0x98));original[0]=1
        struct.pack_into('<IIHH4xHH4xQQ',original,8,2,0,1024,1024,1024,682,0xa1f000000,0xa1f100000)
        struct.pack_into('<I',original,0x90,0x11223344)
        m.mem_write(request,bytes(original))
        m.mem_write(stack+8,struct.pack('<Q',context+0x2000))
        m.mem_write(stack+0x88,struct.pack('<Q',0x40e9a544))
        m.mem_write(stack+0x160,struct.pack('<Q',context))
        m.mem_write(stack+0x168,struct.pack('<Q',0x40e98d74 if channel==0 else 0x40e98d90))
        slot=state+layout['preview_offset']+layout['slots_offset']
        m.mem_write(slot,struct.pack('<II3Q8I2Q',4 if ready else 0,1,0xa10cfff00,
            0xa10d00000,0xa10daa800,1047552,1024,682,1024,1024,2,99,0,0,0xa1ef00000))
        before=[0x71000000+i*16 for i in range(31)]
        before[23]=request;before[25]=context+0x3000;before[27]=channel;before[28]=0x40e9a570
        ids=[getattr(regs,f'UC_ARM64_REG_X{i}') for i in range(31)]
        for register,value in zip(ids,before):m.reg_write(register,value)
        m.reg_write(regs.UC_ARM64_REG_SP,stack);m.reg_write(regs.UC_ARM64_REG_NZCV,0xa0000000)
        m.mem_write(0x40d2c660,bytes.fromhex('c0035fd6'))
        m.mem_write(0x40e9a570,bytes.fromhex('c0035fd6'))
        return m,state,slot,request,stack,bytes(original),before,ids

    def call(self,m,name,stack,**arguments):
        for index,value in arguments.items():m.reg_write(getattr(regs,'UC_ARM64_REG_'+index.upper()),value)
        m.reg_write(regs.UC_ARM64_REG_SP,stack)
        m.reg_write(regs.UC_ARM64_REG_X30,0x65000000)
        m.emu_start(self.symbols[name],0x65000000,count=100000)
        self.assertEqual(m.reg_read(regs.UC_ARM64_REG_PC),0x65000000)

    def apply_image(self,m,stack,y,uv):
        image=0xa03006000;saved=0xa03007000
        m.mem_write(0xa02e70c80,struct.pack('<Q',0xa02c90000))
        m.mem_write(0xa02c90008,struct.pack('<Q',0x15484000))
        m.mem_write(image+0x108,struct.pack('<2Q',y,uv))
        data=bytearray(256);struct.pack_into('<Q',data,19*8,image);struct.pack_into('<Q',data,21*8,0xa02e70c80)
        m.mem_write(saved,bytes(data))
        self.call(m,'z8_photo_lut_applied',stack,x0=saved)

    def test_accepted_queue_translation_and_callback_lifetime(self):
        for channel in [0,3]:
            m,state,slot,request,stack,original,before,ids=self.fixture(channel)
            stop=self.symbols['z8_photo_lut_queue_resume']
            m.emu_start(self.symbols['z8_photo_lut_queue_entry'],stop,count=10000)
            self.assertEqual(m.reg_read(regs.UC_ARM64_REG_PC),stop)
            private=slot+self.metadata['runtime_layout']['request_offset']
            before[1]=private;before[28]=self.symbols['preview_callback']
            self.assertEqual([m.reg_read(r) for r in ids],before)
            self.assertEqual(m.reg_read(regs.UC_ARM64_REG_SP),stack)
            self.assertEqual(m.reg_read(regs.UC_ARM64_REG_NZCV),0xa0000000)
            self.assertEqual(bytes(m.mem_read(request,0x98)),original)
            rewritten=bytearray(original)
            struct.pack_into('<2Q',rewritten,0x20,0xa10d00000,0xa10daa800)
            struct.pack_into('<I',rewritten,0x90,99)
            self.assertEqual(bytes(m.mem_read(private,0x98)),bytes(rewritten))
            self.assertEqual(int.from_bytes(m.mem_read(slot,4),'little'),5)
            forwarded=[]
            def observe(uc,address,size,user):
                if address==0x40e9a570:
                    forwarded.append((uc.reg_read(regs.UC_ARM64_REG_X0),bytes(uc.mem_read(uc.reg_read(regs.UC_ARM64_REG_X1),12))))
            m.hook_add(UC_HOOK_CODE,observe)
            payload=request+0x400
            def callback(event,cookie=99):
                m.mem_write(payload,struct.pack('<QI',0xa10d00000,cookie))
                m.reg_write(regs.UC_ARM64_REG_X0,event);m.reg_write(regs.UC_ARM64_REG_X1,payload)
                m.reg_write(regs.UC_ARM64_REG_X30,0x65000000)
                m.emu_start(self.symbols['preview_callback'],0x65000000,count=10000)
            callback(0x43130005+channel)
            self.assertEqual(int.from_bytes(m.mem_read(slot,4),'little'),5)
            callback(0x43130009+channel,98)
            self.assertEqual(int.from_bytes(m.mem_read(slot,4),'little'),5)
            callback(0x43130009+channel)
            self.assertEqual(int.from_bytes(m.mem_read(slot,4),'little'),0)
            callback(0x43130009+channel)
            self.assertEqual(len(forwarded),2)
            expected=struct.pack('<QI',0xa1f000000,0x11223344)
            self.assertEqual([data for _,data in forwarded],[expected,expected])

    def test_missing_ready_unlocks_without_fifo_or_source_write(self):
        m,state,slot,request,stack,original,before,ids=self.fixture(ready=False)
        calls=[]
        m.hook_add(UC_HOOK_CODE,lambda uc,address,size,user:calls.append(uc.reg_read(regs.UC_ARM64_REG_X0)) if address==0x40d2c660 else None)
        stop=self.symbols['z8_photo_lut_queue_skip']
        m.emu_start(self.symbols['z8_photo_lut_queue_entry'],stop,count=10000)
        self.assertEqual(m.reg_read(regs.UC_ARM64_REG_PC),stop)
        self.assertEqual(calls,[0xa03002000])
        before[0]=0;before[1]=request
        self.assertEqual([m.reg_read(r) for r in ids],before)
        self.assertEqual(bytes(m.mem_read(request,0x98)),original)
        self.assertEqual(m.reg_read(regs.UC_ARM64_REG_SP),stack)
        self.assertEqual(m.reg_read(regs.UC_ARM64_REG_NZCV),0xa0000000)

    def test_real_apply_and_retire_do_not_override_deferred_cache_pin(self):
        m,state,slot,request,stack,original,before,ids=self.fixture()
        m.emu_start(self.symbols['z8_photo_lut_queue_entry'],self.symbols['z8_photo_lut_queue_resume'],count=10000)
        self.apply_image(m,stack,0xa10d00000,0xa10daa800)
        self.apply_image(m,stack,0xa1f000000,0xa1f100000)
        self.apply_image(m,stack,0xa1f200000,0xa1f300000)
        m.mem_write(0xa02e71908,struct.pack('<I',1))
        m.mem_write(0xa02e71a20,struct.pack('<2Q',0xa10d00000,0xa10daa800))
        payload=request+0x400;m.mem_write(payload,struct.pack('<QI',0xa10d00000,99))
        self.call(m,'preview_callback',stack,x0=0x43130009,x1=payload)
        self.assertEqual(int.from_bytes(m.mem_read(slot,4),'little'),6)
        # State0 invalidates replay even though the cached pointer bytes remain.
        m.mem_write(0xa02e71908,struct.pack('<I',0))
        self.call(m,'preview_callback',stack,x0=0x43130009,x1=payload)
        self.assertEqual(int.from_bytes(m.mem_read(slot,4),'little'),0)

    def test_mode_pause_drains_worker_and_detaches_each_lease_once(self):
        m,state,slot,request,stack,original,before,ids=self.fixture()
        layout=self.metadata['runtime_layout'];preview=state+layout['preview_offset']
        paused=preview+layout['paused_offset'];busy=paused+4
        producer=preview+112
        resource_at=preview+layout['cache_resource_offset'];resource=0xa11800000
        m.mem_write(resource_at,struct.pack('<Q',resource))
        m.mem_write(busy,struct.pack('<I',1))
        m.mem_write(producer,struct.pack('<I',1))
        m.mem_write(0x40d2d654,bytes.fromhex('c0035fd6'))
        m.mem_write(0x410477c8,bytes.fromhex('20008052c0035fd6'))
        calls=[]
        def observe(uc,address,size,user):
            if address==0x40d2d654:
                self.assertGreater(int.from_bytes(uc.mem_read(paused,4),'little'),0)
                if int.from_bytes(uc.mem_read(producer,4),'little'):
                    uc.mem_write(producer,bytes(4))
                else:uc.mem_write(busy,bytes(4))
            if address==0x410477c8:
                self.assertEqual(int.from_bytes(uc.mem_read(busy,4),'little'),0)
                self.assertEqual(int.from_bytes(uc.mem_read(producer,4),'little'),0)
                calls.append(uc.reg_read(regs.UC_ARM64_REG_X0))
        m.hook_add(UC_HOOK_CODE,observe)
        self.call(m,'z8_photo_lut_mode_enter',stack)
        self.call(m,'z8_photo_lut_mode_enter',stack)
        self.assertEqual(calls,[resource])
        self.assertEqual(int.from_bytes(m.mem_read(paused,4),'little'),2)
        self.assertEqual(int.from_bytes(m.mem_read(slot,4),'little'),0)
        self.call(m,'z8_photo_lut_mode_exit',stack)
        self.assertEqual(int.from_bytes(m.mem_read(paused,4),'little'),1)
        self.call(m,'z8_photo_lut_mode_exit',stack)
        self.assertEqual(int.from_bytes(m.mem_read(paused,4),'little'),0)

    def test_power_quiesces_preview_without_rejecting_native_capture_ownership(self):
        m,state,slot,request,stack,original,before,ids=self.fixture()
        layout=self.metadata['runtime_layout'];preview=state+layout['preview_offset']
        paused=preview+layout['paused_offset'];power=preview+layout['power_paused_offset']
        jpeg=state+layout['jpeg_busy_offset'];saved=request+0x400
        m.mem_write(saved,struct.pack('<Q',0x05020102)+bytes(248))
        m.mem_write(jpeg,struct.pack('<I',1))
        m.mem_write(0x40d2d654,bytes.fromhex('c0035fd6'))
        sleeps=[]
        def observe(uc,address,size,user):
            if address==0x40d2d654:
                sleeps.append(address)
                self.fail('An early JPEG wait delays native capture shutdown admission')
        m.hook_add(UC_HOOK_CODE,observe)
        self.call(m,'z8_photo_lut_shutdown',stack,x0=saved)
        self.call(m,'z8_photo_lut_shutdown',stack,x0=saved)
        self.assertEqual(sleeps,[])
        self.assertEqual(int.from_bytes(m.mem_read(paused,4),'little'),1)
        self.call(m,'z8_photo_lut_mode_enter',stack)
        self.call(m,'z8_photo_lut_mode_exit',stack)
        self.assertEqual(int.from_bytes(m.mem_read(paused,4),'little'),1)
        self.assertEqual(int.from_bytes(m.mem_read(jpeg,4),'little'),1)
        self.call(m,'z8_photo_lut_power_cancel',stack)
        self.call(m,'z8_photo_lut_power_cancel',stack)
        self.assertEqual(int.from_bytes(m.mem_read(paused,4),'little'),0)
        self.assertEqual(int.from_bytes(m.mem_read(power,4),'little'),0)
        self.assertEqual(int.from_bytes(m.mem_read(jpeg,4),'little'),1)

    def test_mode_transition_waits_for_already_detached_cache_cleanup(self):
        m,state,slot,request,stack,original,before,ids=self.fixture()
        layout=self.metadata['runtime_layout'];preview=state+layout['preview_offset']
        cleanup=state+layout['jpeg_busy_offset']+4
        # Another task detached resource0 but has not finished its CPU clean.
        m.mem_write(cleanup,struct.pack('<I',1))
        m.mem_write(preview+152,struct.pack('<Q',0xca83fb000))
        m.mem_write(0x40d2d654,bytes.fromhex('c0035fd6'))
        waited=[]
        def finish_other_owner(uc,address,size,user):
            if address==0x40d2d654:
                self.assertEqual(int.from_bytes(uc.mem_read(cleanup,4),'little'),1)
                self.assertEqual(int.from_bytes(uc.mem_read(preview+152,8),'little'),0xca83fb000)
                waited.append(address)
                uc.mem_write(preview+152,bytes(8));uc.mem_write(cleanup,bytes(4))
        m.hook_add(UC_HOOK_CODE,finish_other_owner)
        self.call(m,'z8_photo_lut_mode_enter',stack)
        self.assertEqual(len(waited),1)
        self.assertEqual(int.from_bytes(m.mem_read(cleanup,4),'little'),0)

    def test_main_jpeg_is_fully_transformed_while_preview_is_power_paused(self):
        from z8photolut.cube import parse,bundle
        m,state,slot,request,stack,original,before,ids=self.fixture()
        rows=['LUT_3D_SIZE 2']+['%d %d %d'%(g,b,r) for b in (0,1) for g in (0,1) for r in (0,1)]
        data,_=bundle([parse(('\n'.join(rows)+'\n').encode())],grid=17)
        m.mem_map(0x409a1000,0xf000);m.mem_write(0x409a0000,data)
        m.mem_map(0x41186000,4096);m.mem_map(0xc10000000,4096)
        m.mem_write(0x41186718,struct.pack('<3I',0xd2c00088,0x8b080000,0xd65f03c0))
        parameters=0xa03008000;descriptor=parameters+0x100;saved=parameters+0x200
        m.mem_write(parameters+8,struct.pack('<Q',descriptor))
        image=bytearray(0x28);struct.pack_into('<I',image,0,1)
        struct.pack_into('<2H',image,8,8,8);struct.pack_into('<2H',image,16,8,8)
        struct.pack_into('<2Q',image,24,0x810000000,0x810000040);m.mem_write(descriptor,bytes(image))
        registers=[0]*32;registers[1]=parameters;registers[30]=0x40eaeb90
        m.mem_write(saved,struct.pack('<32Q',*registers))
        m.mem_write(0xc10000000,bytes([100])*64+bytes([100,180])*32)
        layout=self.metadata['runtime_layout'];preview=state+layout['preview_offset']
        m.mem_write(preview+layout['paused_offset'],struct.pack('<I',1))
        m.mem_write(preview+layout['power_paused_offset'],struct.pack('<I',1))
        self.call(m,'z8_photo_lut_jpeg',stack,x0=saved)
        self.assertEqual(bytes(m.mem_read(0xc10000000,128)),bytes([71])*64+bytes([186,129])*32)
        self.assertEqual(struct.unpack('<3Q',m.mem_read(state+40,24)),(1,1,64))
        self.assertEqual(int.from_bytes(m.mem_read(state+layout['jpeg_busy_offset'],4),'little'),0)

    def test_trim_only_frees_unowned_uncached_allocations(self):
        m,state,slot,request,stack,original,before,ids=self.fixture()
        layout=self.metadata['runtime_layout'];preview=state+layout['preview_offset']
        m.mem_map(0xa10d00000,4096)
        m.mem_write(0x40d2c448,bytes.fromhex('c0035fd6'))
        for i,status in enumerate((0,5,0)):
            m.mem_write(slot+i*layout['slot_bytes'],struct.pack('<II3QI',status,1,
                0xa10d00000+i*128,0xa10d00000+i*128,0xa10d00040+i*128,64))
        m.mem_write(preview+120,struct.pack('<I',192))
        m.mem_write(0xa02e71908,struct.pack('<I',1))
        m.mem_write(0xa02e71a20,struct.pack('<2Q',0xa10d00100,0xa10d00140))
        freed=[]
        m.hook_add(UC_HOOK_CODE,lambda uc,at,size,user:freed.append(uc.reg_read(regs.UC_ARM64_REG_X0)) if at==0x40d2c448 else None)
        self.call(m,'trim_frames',stack,x0=0)
        self.assertEqual(freed,[0xa10d00000])
        self.assertEqual(int.from_bytes(m.mem_read(preview+120,4),'little'),128)
        self.assertEqual(int.from_bytes(m.mem_read(slot+8,8),'little'),0)
        self.assertEqual(int.from_bytes(m.mem_read(slot+layout['slot_bytes'],4),'little'),5)

    def test_successful_bank_reset_changes_only_target_lut_and_current_if_selected(self):
        for kind in (0,1,5,9,10,11,12,13,14):
            for selected in range(4):
                for success in (0,1):
                    with self.subTest(kind=kind,selected=selected,success=success):
                        m,state,slot,request,stack,original,before,ids=self.fixture()
                        m.mem_map(0x41012000,4096)
                        m.mem_write(0x41012bf4,bytes.fromhex('c0035fd6'))
                        m.mem_write(0x409a0014,struct.pack('<I',1))
                        m.mem_write(0xa03d48164,struct.pack('<I',selected))
                        for bank in range(5):m.mem_write(0xa03d482b4+bank*0x2e0,b'FOLDER\x02\x5a')
                        saved=request+0x400;registers=[0]*32;registers[0]=success;registers[19]=kind
                        m.mem_write(saved,struct.pack('<32Q',*registers))
                        saves=[]
                        m.hook_add(UC_HOOK_CODE,lambda uc,at,size,user:saves.append(at) if at==0x41012bf4 else None)
                        self.call(m,'z8_photo_lut_bank_reset_result',stack,x0=saved)
                        target=0 if kind==9 else kind-10
                        valid=success and 0<=target<4
                        for bank in range(5):
                            changed=valid and (bank==target+1 or bank==0 and selected==target)
                            self.assertEqual(bytes(m.mem_read(0xa03d482b4+bank*0x2e0,8)),b'FOLDER'+bytes([1 if changed else 2,0x5a]))
                        self.assertEqual(len(saves),1 if valid else 0)


if __name__=='__main__':unittest.main()
