"""Execute the actual AArch64 bank trampolines and verify the native ABI."""
import random
import struct
import unittest

from z8photolut import library as lib
from z8photolut.patcher import CODE,STILL_VA,branch
from photo_lut_fixtures import (CODE_BYTES,JPEG_STORAGE,LEASE_BYTES,LEASE_MUTABLE,LIBRARY,RESUME,STATE,bundle_ids,desire,isp_look,
                                native_code,record_bytes,runtime_bundle,snapshot_bytes)


try:
    from unicorn import Uc,UC_ARCH_ARM64,UC_MODE_ARM,UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    Uc=None


class NativeScheduler:
    """Priority-preemptive per-CPU task model over one Unicorn engine.

    Each round, every CPU runs its highest-priority runnable task for a bounded
    instruction slice (one tick). Native sleep and event waits block the caller
    and return control here; the task later resumes at its return address.
    Task, event and heap services are recorded stubs at their C3.11 addresses.
    Smaller numbers are higher priority, as in the native kernel.
    """
    SLEEP,WAIT,SET,CREATE,START,EVENT,ALLOC,FREE=(0x40d2d654,0x40d2bdac,0x40d2bc8c,0x40d2d498,
                                                0x40d2d5cc,0x40d2bc28,0x40d2c408,0x40d2c448)
    STOP=0x65000000

    def __init__(self,m,stacks,heap=0xa11000000):
        self.m=m;self.tasks=[];self.current=None;self.round=0;self.stacks=list(stacks)
        self.heap=heap;self.created=[];self.events={};self.sets=[];self.on_sleep=None
        self.fail=dict(alloc=0,event=0,create=0);self.allocations=[];self.event_calls=[];self.create_calls=[];self.starts=[]
        m.mem_map(0x40d2b000,4096);m.mem_map(0x40d2e000,4096);m.mem_map(heap,0x10000)
        for at in (self.SLEEP,self.WAIT,self.SET,self.CREATE,self.START,self.EVENT,self.ALLOC,self.FREE):
            m.mem_write(at,bytes.fromhex('c0035fd6'))
        m.hook_add(UC_HOOK_CODE,self._service,begin=0x40d2b000,end=0x40d2dfff)

    def task(self,name,cpu,priority,pc,arguments=()):
        context=self.m.context_save()
        task=dict(name=name,cpu=cpu,priority=priority,state='ready',wake=0,event=None,
                  slices=[],sleeps=0,finished=False)
        self.m.reg_write(regs.UC_ARM64_REG_PC,pc)
        self.m.reg_write(regs.UC_ARM64_REG_SP,self.stacks.pop(0))
        self.m.reg_write(regs.UC_ARM64_REG_X30,self.STOP)
        for index,value in enumerate(arguments):self.m.reg_write(getattr(regs,f'UC_ARM64_REG_X{index}'),value)
        task['context']=self.m.context_save();self.m.context_restore(context)
        self.tasks.append(task)
        return task

    def _return(self,value=None):
        if value is not None:self.m.reg_write(regs.UC_ARM64_REG_X0,value)
        self.m.reg_write(regs.UC_ARM64_REG_PC,self.m.reg_read(regs.UC_ARM64_REG_X30))

    def _block(self,state,value=1):
        self.current['state']=state;self._return(value);self.m.emu_stop()

    def _service(self,uc,at,size,user):
        x=lambda i:uc.reg_read(getattr(regs,f'UC_ARM64_REG_X{i}'))
        if at==self.SLEEP:
            self.current['sleeps']+=1;self.current['wake']=self.round+max(1,x(0))
            if self.on_sleep:self.on_sleep(self.current)
            self._block('sleep')
        elif at==self.WAIT:
            event=struct.unpack('<i',uc.mem_read(x(0),4))[0]
            if self.events.get(event,0)&x(1):self.events[event]&=~x(1);self._return(1)
            else:self.current['event']=(event,x(1));self._block('wait')
        elif at==self.SET:
            event=struct.unpack('<i',uc.mem_read(x(0),4))[0]
            self.events[event]=self.events.get(event,0)|x(1)
            self.sets.append((self.current['name'] if self.current else None,event,x(1),self.round));self._return(1)
        elif at==self.EVENT:
            identifier=300+len(self.events)
            if self.fail['event']:self.fail['event']-=1;identifier=-0x22
            else:self.events[identifier]=x(1)
            self.event_calls.append(identifier)
            uc.mem_write(x(0),struct.pack('<i',identifier));self._return()
        elif at==self.CREATE:
            raw=bytes(uc.mem_read(x(2),28))
            cpu,priority,stack=struct.unpack_from('<3I',raw)
            record=dict(cpu=cpu,priority=priority,stack=stack,attribute=raw[12],
                        name=raw[13:].split(b'\0')[0].decode(),entry=x(1),block=x(0))
            if self.fail['create']:self.fail['create']-=1;identifier=-0x22
            else:identifier=self.create_identifier(record)
            self.create_calls.append(identifier)
            if identifier>0:self.created.append(record)
            uc.mem_write(x(0),struct.pack('<i',identifier));self._return()
        elif at==self.START:
            self.starts.append(struct.unpack('<i',uc.mem_read(x(0),4))[0])
            record=next(r for r in self.created if r['block']==x(0))
            context=uc.context_save()
            self.task(record['name'],record['cpu'],record['priority'],record['entry'],(x(1),))
            uc.context_restore(context);self._return()
        elif at==self.ALLOC:
            if self.fail['alloc'] and (getattr(self,'alloc_failure_size',None) is None or x(0)==self.alloc_failure_size):self.fail['alloc']-=1;self.allocations.append(0);self._return(0);return
            pointer=self.heap;self.heap+=(x(0)+63)&~63;self.allocations.append(pointer);self._return(pointer)
        elif at==self.FREE:
            self._return()

    def create_identifier(self,record):
        return 100+len(self.create_calls)

    def runnable(self,task):
        if task['finished']:return False
        if task['state']=='sleep':return self.round>=task['wake']
        if task['state']=='wait':
            event,bits=task['event']
            return bool(self.events.get(event,0)&bits)
        return True

    def run(self,until,tick=4000,rounds=2000):
        """Each round is one tick of `tick` instructions per CPU. A task that
        blocks leaves the rest of its tick to lower-priority runnable tasks."""
        for self.round in range(rounds):
            if until():return
            for cpu in sorted({t['cpu'] for t in self.tasks}):
                budget=tick() if callable(tick) else tick
                while budget>0:
                    ready=[t for t in self.tasks if t['cpu']==cpu and self.runnable(t)]
                    if not ready:break
                    task=min(ready,key=lambda t:t['priority'])
                    if task['state']=='wait':
                        event,bits=task['event'];self.events[event]&=~bits
                    task['state']='ready';self.current=task
                    self.m.context_restore(task['context'])
                    executed=[0]
                    counter=self.m.hook_add(UC_HOOK_CODE,lambda uc,a,s,u:executed.__setitem__(0,executed[0]+1))
                    self.m.emu_start(self.m.reg_read(regs.UC_ARM64_REG_PC),self.STOP,count=budget)
                    self.m.hook_del(counter)
                    budget-=executed[0]
                    task['slices'].append((self.round,executed[0]))
                    if self.m.reg_read(regs.UC_ARM64_REG_PC)==self.STOP:
                        task['finished']=True;task['result']=self.m.reg_read(regs.UC_ARM64_REG_X0)
                    task['context']=self.m.context_save()
                    self.current=None
                    if task['state']=='ready' and not task['finished']:break
        raise AssertionError('Native tasks did not reach the expected state')


# A periodic lower-priority CPU1 service: count one unit of progress, then
# sleep one tick. x19 keeps the counter pointer across the native call.
SERVICE=0x40d2e000
def service_code():
    return (struct.pack('<5I',0xaa0003f3,0xb9400261,0x11000421,0xb9000261,0x52800020)+
            branch(SERVICE+20,NativeScheduler.SLEEP,True)+branch(SERVICE+24,SERVICE+4))


@unittest.skipIf(Uc is None,'Unicorn is required for AArch64 queue ABI checks')
class PhotoLutQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code,cls.symbols,cls.metadata=native_code()

    def fixture(self,channel=0,ready=True):
        m=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
        state=STATE;context=0xa03000000;request=context+0x1000;stack=context+0x11800
        for at,size in [(CODE,CODE_BYTES),(LIBRARY,4096),(state,8192),(0xa03d48000,4096),
                        (context,0x20000),(0x40cd7000,4096),(0x40d2c000,4096),(0x40d2d000,4096),
                        (0x40e9a000,4096),(0x65000000,4096),(0xa02810000,4096),
                        (0xa02e70000,8192),(0xa02c90000,4096),(0xa03f31000,4096),(0x41047000,4096)]:m.mem_map(at,size)
        m.mem_write(CODE,self.code)
        layout=self.metadata['runtime_layout']
        m.mem_write(state,struct.pack('<Q6I',0x32545354554c385a,2,layout['state_bytes'],1,256,1,0))
        m.mem_write(LIBRARY+16,struct.pack('<I',2))         # a published card library of two looks
        desire(m,layout,1)
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

    def call(self,m,name,stack,count=100000,**arguments):
        for index,value in arguments.items():m.reg_write(getattr(regs,'UC_ARM64_REG_'+index.upper()),value)
        m.reg_write(regs.UC_ARM64_REG_SP,stack)
        m.reg_write(regs.UC_ARM64_REG_X30,0x65000000)
        m.emu_start(self.symbols[name],0x65000000,count=count)
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
            stop=RESUME['queue_resume']
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
        stop=RESUME['queue_skip']
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
        m.emu_start(self.symbols['z8_photo_lut_queue_entry'],RESUME['queue_resume'],count=10000)
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

    SERVICE_COUNTER=0xa0301f800
    PRIVATE=0x820001000
    SOURCE=bytes([100])*64+bytes([100,180])*32
    FILTERED=bytes([71])*64+bytes([186,129])*32

    STACKS=(0xa03015000,0xa03017800,0xa0301a000,0xa0301c800,0xa0301f000)

    CYCLE=('TITLE "Cycle"\nLUT_3D_SIZE 2\n'+''.join('%d %d %d\n'%(g,b,r) for b in (0,1) for g in (0,1) for r in (0,1))).encode()

    def jpeg_fixture(self,selected=1,capacity=None,available=True,jpeg_enabled=True,fail=None,width=8,height=8,
                     looks=None,transform=None):
        """looks: a library of Look objects instead of the Cycle and Identity cpu-3d looks; transform: whether the
        selected look reserves a CPU transform (default: look 1 of the standard library)."""
        m,state,slot,request,stack,original,before,ids=self.fixture()
        identity=['TITLE "Identity"','LUT_3D_SIZE 2']+['%d %d %d'%(r,g,b) for b in (0,1) for g in (0,1) for r in (0,1)]
        data=lib.Library(looks).runtime() if looks else runtime_bundle([self.CYCLE,('\n'.join(identity)+'\n').encode()])
        m.mem_map(LIBRARY+0x1000,0x1f000);m.mem_write(LIBRARY,data)
        m.mem_map(0x41186000,4096);m.mem_map(0xc10000000,4096)
        m.mem_map(0x41059000,4096);m.mem_map(0x40e82000,4096)
        m.mem_write(0x41186718,struct.pack('<3I',0xd2c00088,0x8b080000,0xd65f03c0))
        m.mem_write(0x4105903c,struct.pack('<2I',0x52800000|(int(jpeg_enabled)<<5),0xd65f03c0))
        owner=0xa03009000;allocation_descriptor=owner+0x1000;raw=0x820000000
        transform=(selected==1 if transform is None else transform) and jpeg_enabled
        required=4096+(JPEG_STORAGE+snapshot_bytes(17) if transform else 0)+LEASE_BYTES
        if capacity is None:capacity=(required+511)&~511
        m.mem_write(owner+8,struct.pack('<Q',allocation_descriptor))
        m.mem_write(allocation_descriptor,struct.pack('<QI',raw,capacity))
        m.mem_map(raw+0x400000000,8192)
        # The latched private look grid follows the JPEG scratch.
        if transform:
            first=(raw+0x400000000+4096+JPEG_STORAGE)&~4095
            m.mem_map(first,((raw+0x400000000+4096+JPEG_STORAGE+snapshot_bytes(17)+4095)&~4095)-first)
        self.snapshot=raw+0x400000000+4096+JPEG_STORAGE
        footer=raw+0x400000000+capacity-LEASE_BYTES
        if footer>=raw+0x400000000+8192:
            try:m.mem_map(footer&~4095,4096)
            except Exception:pass # Shares the snapshot's last page.
        desire(m,self.metadata['runtime_layout'],selected)
        allocations=[];freed=[];encodes=[];self.encode_log=[];self.encode_failures={}
        pixels=width*height
        self.source=bytes([100])*pixels+bytes([100,180])*(pixels//2)
        self.filtered=bytes([71])*pixels+bytes([186,129])*(pixels//2)
        self.scheduler=NativeScheduler(m,self.STACKS)
        if fail:self.scheduler.fail[fail]=1
        def native(uc,at,size,user):
            if at==0x41047478:
                allocations.append(tuple(uc.reg_read(getattr(regs,f'UC_ARM64_REG_X{i}')) for i in range(3)))
                uc.reg_write(regs.UC_ARM64_REG_X0,owner if available else 0)
                uc.reg_write(regs.UC_ARM64_REG_PC,uc.reg_read(regs.UC_ARM64_REG_X30))
            elif at==0x410477c8:
                freed.append(uc.reg_read(regs.UC_ARM64_REG_X0));uc.reg_write(regs.UC_ARM64_REG_X0,1)
                uc.reg_write(regs.UC_ARM64_REG_PC,uc.reg_read(regs.UC_ARM64_REG_X30))
            elif at==0x40e82090:
                params=uc.reg_read(regs.UC_ARM64_REG_X1)
                desc=int.from_bytes(uc.mem_read(params+8,8),'little')
                planes=struct.unpack('<2Q',uc.mem_read(desc+24,16))
                encodes.append((uc.reg_read(regs.UC_ARM64_REG_X0),desc,planes))
                # The NEF/native source is never modified by the LUT path, and
                # every private Y/UV byte equals the exact LUT result.
                self.assertEqual(bytes(uc.mem_read(0xc10000000,len(self.source))),self.source)
                private=None
                if planes[0]!=0x810000000:
                    whole=bytes(uc.mem_read(planes[0]+0x400000000,len(self.filtered)))
                    self.assertEqual(whole,self.filtered)
                    private=whole[:128]
                    self.assertGreaterEqual(int.from_bytes(uc.mem_read(state+self.metadata['runtime_layout']['jpeg_busy_offset'],4),'little'),1)
                task=self.scheduler.current['name'] if self.scheduler and self.scheduler.current else None
                self.encode_log.append(dict(task=task,planes=planes,private=private,round=self.scheduler.round,
                    service=int.from_bytes(uc.mem_read(self.SERVICE_COUNTER,4),'little')))
                # Native inline control changes must reach the original caller.
                uc.mem_write(params+0x24,struct.pack('<I',0x12345678))
                output=int.from_bytes(uc.mem_read(params+0x10,8),'little')
                uc.mem_write(output+0x10,struct.pack('<I',128+len(encodes)))
                # Native 40e82090: returns 1 unless its encode start failed; a
                # result block gets the completion flag in byte0 (40e82250),
                # then sizes. Here the size word also records the call order.
                failure=self.encode_failures.get(len(encodes)-1,'')  # by call index
                result=int.from_bytes(uc.mem_read(params+0x48,8),'little')
                if result:uc.mem_write(result,struct.pack('<B3xI',int(failure!='incomplete'),len(encodes)))
                uc.reg_write(regs.UC_ARM64_REG_X0,int(failure!='start'))
                uc.reg_write(regs.UC_ARM64_REG_PC,uc.reg_read(regs.UC_ARM64_REG_X30))
        m.hook_add(UC_HOOK_CODE,native)
        # A Log look's reservation also hashes its ISP tables (SHA-256 of 2304 bytes) for provenance.
        self.call(m,'z8_photo_lut_output_alloc',stack,count=1000000,x0=42,x1=0x38,x2=4096,x3=0xa03000000)
        allocation=m.reg_read(regs.UC_ARM64_REG_X0)
        parameters=0xa03008000;descriptor=parameters+0x100
        m.mem_write(parameters+8,struct.pack('<2Q',descriptor,owner+0x38))
        image=bytearray(0x28);struct.pack_into('<I',image,0,1)
        struct.pack_into('<2H',image,8,width,width);struct.pack_into('<2H',image,16,width,height)
        struct.pack_into('<2Q',image,24,0x810000000,0x810000000+width*height);m.mem_write(descriptor,bytes(image))
        m.mem_write(0xc10000000,self.source)
        return m,state,stack,parameters,descriptor,bytes(image),allocations,freed,encodes,allocation,footer

    def preview_request(self,m,owner,kind,parameters=0xa03008800):
        """A medium/small JPEG request for the same capture as the main JPEG."""
        descriptor=parameters+0x100;capture=0xa03002000
        m.mem_write(capture+0x6a8,struct.pack('<Q',owner))
        image=bytearray(0x28);struct.pack_into('<I',image,0,1)
        struct.pack_into('<2H',image,8,8,8);struct.pack_into('<2H',image,16,8,8)
        struct.pack_into('<2Q',image,24,0x810000000,0x810000040)
        m.mem_write(descriptor,bytes(image))
        m.mem_write(parameters+8,struct.pack('<2Q',descriptor,owner+(0xc0 if kind==1 else 0xa0)))
        private=self.PRIVATE+8256*5504*2+(1920*1080*2 if kind==2 else 0)
        try:m.mem_map((private+0x400000000)&~4095,8192)
        except Exception:pass # Already mapped for the same capture.
        return parameters,capture,private

    def run_jpeg(self,m,requests,service=True,on_sleep=None,heavy=False,tick=4000,rounds=2000):
        """Native encoder tasks at priority22 (CPU1 unless given) and an optional
        CPU1 priority30 periodic service, as observed for STGJPEGB and EXMCUSRV."""
        scheduler=self.scheduler;scheduler.on_sleep=on_sleep
        jobs=[scheduler.task(request[0],request[3] if len(request)>3 else 1,22,self.symbols[request[1]],request[2])
              for request in requests]
        if service:
            m.mem_write(SERVICE,service_code())
            scheduler.task('service',1,30,SERVICE,(self.SERVICE_COUNTER,))
        executed={}
        if heavy:
            # Instructions per task in the full-surface CPU paths: the shared
            # LUT kernel and the copy/cache routine feeding it. Geometry
            # validation alone enters the kernel briefly before its pixel loop.
            names=sorted(self.symbols.items(),key=lambda item:item[1])
            ranges={name:(address,names[index+1][1]) for index,(name,address) in enumerate(names)
                    if name in ('jpeg_worker','z8_lut_surface_cancel')}
            def count(uc,at,size,user):
                if not scheduler.current:return
                for name,(first,end) in ranges.items():
                    if first<=at<end:
                        key=(scheduler.current['name'],name);executed[key]=executed.get(key,0)+1
            m.hook_add(UC_HOOK_CODE,count)
        scheduler.run(lambda:all(job['finished'] for job in jobs),tick=tick,rounds=rounds)
        return scheduler,jobs,executed

    def assert_full_surface_work(self,executed,performers):
        """Only `performers` copy or transform pixels; others at most validate."""
        for (task,name),instructions in executed.items():
            if name=='jpeg_worker':self.assertEqual(task,'PhotoJPG')
            elif task not in performers:self.assertLess(instructions,100,(task,name))
        for task in performers:
            self.assertGreater(executed.get((task,'z8_lut_surface_cancel'),0),2000,task)

    def jpeg_worker_state(self,m,state):
        base=state+self.metadata['runtime_layout']['jpeg_worker_offset']
        fields=struct.unpack('<Q2I4I5Q',m.mem_read(base,72))
        record=dict(zip(('worker','id','state','event_ready','alive','submit_lock','peak','sequence','submitted',
                         'completed','start_failures','waits'),fields))
        record['jobs']=[struct.unpack('<IIQ',m.mem_read(base+72+i*112,16)) for i in range(8)]
        record['job_states']=[job[0] for job in record['jobs']]
        return record

    def test_main_jpeg_uses_owned_copy_and_preserves_nef_source_while_power_paused(self):
        m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture()
        self.assertNotEqual(owner,0)
        layout=self.metadata['runtime_layout'];preview=state+layout['preview_offset']
        m.mem_write(preview+layout['paused_offset'],struct.pack('<I',1))
        m.mem_write(preview+layout['power_paused_offset'],struct.pack('<I',1))
        # A menu change after reservation does not alter this capture's look.
        desire(m,layout,0)
        _,jobs,_=self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params))],service=False)
        self.assertEqual(jobs[0]['result'],1)
        self.assertEqual(struct.unpack('<3Q',m.mem_read(state+40,24)),(1,1,64))
        self.assertEqual(int.from_bytes(m.mem_read(state+layout['jpeg_busy_offset'],4),'little'),0)
        self.assertEqual(bytes(m.mem_read(desc,0x28)),image)
        self.assertEqual(int.from_bytes(m.mem_read(params+8,8),'little'),desc)
        self.assertEqual(int.from_bytes(m.mem_read(params+0x24,4),'little'),0x12345678)
        self.assertEqual(encodes[0][2],(0x820001000,0x820001040))
        self.assertEqual(allocations,[(42,0x38,4096+JPEG_STORAGE+snapshot_bytes(17)+LEASE_BYTES)])
        self.assertEqual(freed,[]) # The native output owner retains the tail.
        self.assertEqual(int.from_bytes(m.mem_read(footer,8),'little'),0x3154504f4c385a4a)

    def test_applied_status_requires_native_encode_success_and_is_withdrawn_on_retry(self):
        # Stage bits: 1 main, 2 medium, 4 small, 8 tiny. Native 40e82090 returns
        # 0 when its encode start fails; a result block records completion.
        for failure,expected in [('',1),('start',0),('incomplete',0)]:
            with self.subTest(failure=failure):
                m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture()
                result=params+0x400;m.mem_write(params+0x48,struct.pack('<Q',result))
                self.encode_failures={0:failure} if failure else {}
                self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params))],service=False)
                self.assertEqual(int.from_bytes(m.mem_read(footer+LEASE_MUTABLE+52,4),'little'),expected)
        # A later failed encode of the same output withdraws its earlier success.
        m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture()
        self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params))],service=False)
        self.assertEqual(int.from_bytes(m.mem_read(footer+LEASE_MUTABLE+52,4),'little'),1)
        self.encode_failures={1:'start'}
        self.run_jpeg(m,[('STGJPEGC','z8_photo_lut_jpeg',(0,params))],service=False)
        self.assertEqual(int.from_bytes(m.mem_read(footer+LEASE_MUTABLE+52,4),'little'),0)

    def test_log_look_residual_applies_only_after_its_own_develop(self):
        # Z8_LUT_NOT_DEVELOPED in jpeg_error when the most recent still develop used other tables.
        look=isp_look('Cycle Log',residual=self.CYCLE,basis='f-log2/f-gamut-c')
        for developed in (True,False):
            with self.subTest(developed=developed):
                m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture(
                    looks=(look,isp_look('Other')))
                self.assertEqual(allocations,[(42,0x38,4096+JPEG_STORAGE+snapshot_bytes(17)+LEASE_BYTES)])
                rendering,basis=struct.unpack('<2I',m.mem_read(footer+176,8))
                self.assertEqual((rendering,basis),(1,4))
                self.assertEqual(bytes(m.mem_read(footer+192,32)),lib.fit_id(look.isp))
                self.assertEqual(bytes(m.mem_read(footer+80,32)),look.id)
                # The residual copied into the lease is the grid after the ISP tables.
                self.assertEqual(bytes(m.mem_read(self.snapshot,len(look.samples))),look.samples)
                m.mem_write(STILL_VA+0x48,look.id if developed else bytes(32))
                self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params))],service=False)
                self.assertEqual(encodes[0][2],(0x820001000,0x820001040) if developed else (0x810000000,0x810000040))
                self.assertEqual(int.from_bytes(m.mem_read(footer+LEASE_MUTABLE+52,4),'little'),int(developed))
                self.assertEqual(int.from_bytes(m.mem_read(state+92,4),'little'),0 if developed else 8)

    def test_log_look_with_an_identity_residual_reserves_no_transform(self):
        look=isp_look('Log')
        for developed in (True,False):
            with self.subTest(developed=developed):
                m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture(
                    looks=(look,),transform=False)
                self.assertEqual(allocations,[(42,0x38,4096+LEASE_BYTES)])
                m.mem_write(STILL_VA+0x48,look.id if developed else bytes(32))
                self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params))],service=False)
                # The develop is the look: the native surface is encoded and, only if it was developed, applied.
                self.assertEqual(encodes[0][2],(0x810000000,0x810000040))
                self.assertEqual(int.from_bytes(m.mem_read(footer+LEASE_MUTABLE+52,4),'little'),int(developed))

    def test_full_surface_work_runs_in_low_priority_worker_while_cpu1_services_progress(self):
        # 64x32 keeps the worker's LUT computation busy for many ticks.
        m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture(width=64,height=32)
        scheduler=self.scheduler
        # The transform reservation started exactly one worker: below every
        # native C3.11 priority (2..40), CPU3, attribute byte zero, name at +13.
        self.assertEqual([(r['cpu'],r['priority'],r['stack'],r['attribute'],r['name']) for r in scheduler.created],
                         [(3,48,16384,0,'PhotoJPG')])
        self.assertGreater(scheduler.created[0]['priority'],40)
        self.assertEqual(scheduler.starts,[scheduler.create_calls[0]])
        scheduler,jobs,heavy=self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params))],heavy=True)
        self.assertEqual(jobs[0]['result'],1)
        self.assert_full_surface_work(heavy,{'PhotoJPG'})
        # The lower-priority CPU1 service advanced while the encoder task
        # waited; the native encoder call itself stayed with the requester.
        self.assertEqual([(row['task'],row['private']) for row in self.encode_log],[('STGJPEGB',self.filtered[:128])])
        # Sustained fairness: the periodic priority30 service (the EXMCUSRV
        # role) completed a period in essentially every tick the request was
        # outstanding, not just once.
        submitted=[row[3] for row in scheduler.sets if row[0]=='STGJPEGB'][0]
        encoded=self.encode_log[0]['round']
        self.assertGreaterEqual(encoded-submitted,20)
        self.assertGreaterEqual(self.encode_log[0]['service'],encoded-1)
        requester=jobs[0]
        self.assertGreater(requester['sleeps'],0)
        self.assertLess(max(count for _,count in requester['slices'][1:]),600)
        worker=self.jpeg_worker_state(m,state)
        self.assertEqual((worker['state'],worker['event_ready'],worker['alive'],worker['submit_lock']),(2,1,1,0))
        self.assertEqual((worker['sequence'],worker['submitted'],worker['completed'],worker['start_failures'],worker['peak']),(1,1,1,0,1))
        self.assertEqual(worker['job_states'],[0]*8)
        self.assertEqual(int.from_bytes(m.mem_read(state+self.metadata['runtime_layout']['jpeg_busy_offset'],4),'little'),0)

    def test_concurrent_jpeg_jobs_do_not_monopolize_cpu1(self):
        m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture()
        medium,capture,private=self.preview_request(m,owner,1)
        scheduler,jobs,heavy=self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params)),
                                              ('STGPJPEG','z8_photo_lut_medium',(0,medium,capture,0))],heavy=True)
        self.assertEqual([job['result'] for job in jobs],[1,1])
        self.assertEqual(len(scheduler.created),1)
        self.assert_full_surface_work(heavy,{'PhotoJPG'})
        self.assertEqual(sorted(row['task'] for row in self.encode_log),['STGJPEGB','STGPJPEG'])
        self.assertEqual(sorted(row['planes'][0] for row in self.encode_log),sorted([self.PRIVATE,private]))
        self.assertEqual([row['private'] for row in self.encode_log],[self.FILTERED]*2)
        # Equal-priority encoders both block: the service still progresses.
        self.assertGreaterEqual(min(row['service'] for row in self.encode_log),1)
        for job in jobs:self.assertLess(max(count for _,count in job['slices'][1:]),600,job['name'])
        worker=self.jpeg_worker_state(m,state)
        self.assertEqual((worker['submitted'],worker['completed'],worker['start_failures'],worker['peak']),(2,2,0,2))
        self.assertEqual(worker['job_states'],[0]*8)
        statistics=state+self.metadata['runtime_layout']['jpeg_preview_offset']
        self.assertEqual(struct.unpack('<3Q',m.mem_read(state+40,24)),(1,1,64))
        self.assertEqual(struct.unpack('<3Q',m.mem_read(statistics,24)),(1,64,0))

    def served_order(self,m,state):
        """Sequence of each request at the moment the worker starts its pixels."""
        base=state+self.metadata['runtime_layout']['jpeg_worker_offset']+72;served=[]
        def observe(uc,at,size,user):
            if self.scheduler.current and self.scheduler.current['name']=='PhotoJPG':
                rows=[struct.unpack('<IIQ',uc.mem_read(base+i*112,16)) for i in range(8)]
                running=[sequence for status,error,sequence in rows if status==3]
                self.assertEqual(len(running),1)
                served.append(running[0])
        m.hook_add(UC_HOOK_CODE,observe,begin=self.symbols['z8_lut_surface_cancel'],end=self.symbols['z8_lut_surface_cancel'])
        return served

    def test_requests_are_served_in_exact_publication_order_under_parallel_preemption(self):
        import random
        for seed in range(12):
            with self.subTest(seed=seed):
                rng=random.Random(seed)
                m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture()
                medium,capture,_=self.preview_request(m,owner,1)
                small,_,_=self.preview_request(m,owner,2,parameters=0xa03008c00)
                # Encoders on three CPUs with ticks of a few instructions, so a
                # producer can be preempted anywhere, including between its
                # sequence assignment and publication or mid worker scan.
                requests=[('STGJPEGB','z8_photo_lut_jpeg',(0,params),1),
                          ('STGPJPEG','z8_photo_lut_medium',(0,medium,capture,0),2),
                          ('STGJPEGA','z8_photo_lut_small',(0,small,capture,0),3)]
                rng.shuffle(requests)
                served=self.served_order(m,state)
                scheduler,jobs,_=self.run_jpeg(m,requests,service=False,tick=lambda:rng.randint(3,40),rounds=100000)
                self.assertEqual([job['result'] for job in jobs],[1,1,1])
                self.assertEqual(served,[1,2,3])
                self.assertEqual([row['private'] for row in self.encode_log],[self.FILTERED]*3)
                worker=self.jpeg_worker_state(m,state)
                self.assertEqual((worker['sequence'],worker['completed'],worker['submit_lock']),(3,3,0))
                self.assertEqual(worker['job_states'],[0]*8)

    def test_worker_start_failure_refuses_transform_reservation_and_retry_reuses_resources(self):
        for step in ('alloc','event','create'):
            with self.subTest(step=step):
                m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture(fail=step)
                scheduler=self.scheduler
                # Refused before the native pool allocation: the ordinary
                # native allocation-failure path, with no task started.
                self.assertEqual(owner,0)
                self.assertEqual(allocations,[])
                self.assertEqual(scheduler.starts,[])
                worker=self.jpeg_worker_state(m,state)
                self.assertEqual((worker['state'],worker['start_failures'],worker['id']),(0,1,0))
                # Steps that completed are retained, never leaked or repeated.
                self.assertEqual(worker['worker']!=0,step!='alloc')
                self.assertEqual(worker['event_ready'],int(step=='create'))
                self.call(m,'z8_photo_lut_output_alloc',stack,x0=42,x1=0x38,x2=4096,x3=0xa03000000)
                self.assertEqual(m.reg_read(regs.UC_ARM64_REG_X0),0xa03009000)
                self.assertEqual(len(allocations),1)
                self.assertEqual([a for a in scheduler.allocations if a],[worker['worker'] or scheduler.allocations[-1]])
                self.assertEqual([e for e in scheduler.event_calls if e>0],scheduler.event_calls[-1:])
                self.assertEqual(len(scheduler.event_calls),2 if step=='event' else 1 if step!='alloc' else 1)
                self.assertEqual(len(scheduler.create_calls),2 if step=='create' else 1)
                # Started once, only with a positive task id from a fresh create.
                self.assertEqual(scheduler.starts,[scheduler.create_calls[-1]])
                self.assertGreater(scheduler.starts[0],0)
                worker=self.jpeg_worker_state(m,state)
                self.assertEqual((worker['state'],worker['start_failures'],worker['event_ready']),(2,1,1))
                _,jobs,heavy=self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params))],heavy=True)
                self.assertEqual(jobs[0]['result'],1)
                self.assert_full_surface_work(heavy,{'PhotoJPG'})
                self.assertEqual([row['private'] for row in self.encode_log],[self.FILTERED])

    def test_identity_and_off_reservations_do_not_start_the_worker(self):
        for selected in (0,2):
            with self.subTest(selected=selected):
                self.jpeg_fixture(selected)
                self.assertEqual((self.scheduler.created,self.scheduler.allocations,self.scheduler.event_calls),([],[],[]))

    def test_transform_without_running_worker_never_runs_inline(self):
        # Unreachable through admission; the guard must not restore the
        # starving inline path even if the invariant were violated.
        m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture()
        base=state+self.metadata['runtime_layout']['jpeg_worker_offset']
        m.mem_write(base+12,struct.pack('<I',0))
        scheduler,jobs,heavy=self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params))],service=False,heavy=True)
        self.assertEqual(jobs[0]['result'],1)
        self.assert_full_surface_work(heavy,set())
        self.assertEqual(struct.unpack('<3Q',m.mem_read(state+40,24)),(1,0,0))
        self.assertEqual(int.from_bytes(m.mem_read(state+92,4),'little'),7) # jpeg_error: Z8_LUT_NO_WORKER
        self.assertEqual(self.jpeg_worker_state(m,state)['submitted'],0)

    def test_queued_request_survives_shutdown_and_mode_quiescing(self):
        m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture()
        layout=self.metadata['runtime_layout'];preview=state+layout['preview_offset']
        saved=0xa03007800;m.mem_write(saved,struct.pack('<Q',0x05020102)+bytes(248))
        control=[]
        def quiesce(task):
            # Power-off notification (and its mode/pool transition entry) and a
            # separate mode transition arrive while the request is outstanding.
            if control or task['name']!='STGJPEGB':return
            control.append(self.scheduler.task('BSShutdown',2,10,self.symbols['z8_photo_lut_shutdown'],(saved,)))
            control.append(self.scheduler.task('mode',2,12,self.symbols['z8_photo_lut_mode_enter']))
        scheduler,jobs,heavy=self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(0,params))],service=False,
                                           on_sleep=quiesce,heavy=True,tick=1500)
        self.assertEqual(jobs[0]['result'],1)
        self.assertTrue(all(task['finished'] for task in control))
        # Both quiescing calls completed while the request was still queued
        # or running, without cancelling or waiting for it.
        self.assertLess(max(task['slices'][-1][0] for task in control),self.encode_log[0]['round'])
        self.assertEqual(int.from_bytes(m.mem_read(preview+layout['power_paused_offset'],4),'little'),1)
        self.assertEqual(int.from_bytes(m.mem_read(preview+layout['paused_offset'],4),'little'),2)
        self.assert_full_surface_work(heavy,{'PhotoJPG'})
        self.assertEqual([row['private'] for row in self.encode_log],[self.FILTERED])
        self.assertEqual(self.jpeg_worker_state(m,state)['job_states'],[0]*8)
        self.assertEqual(freed,[])

    def test_preview_worker_name_uses_native_task_parameter_layout(self):
        m,state,slot,request,stack,original,before,ids=self.fixture()
        scheduler=NativeScheduler(m,[0xa03011800,0xa0301b800]);self.scheduler=scheduler
        preview=state+self.metadata['runtime_layout']['preview_offset']
        task=scheduler.task('caller',1,22,self.symbols['preview_start_worker'],(preview,))
        scheduler.run(lambda:task['finished'])
        self.assertEqual(task['result'],1)
        self.assertEqual([(r['cpu'],r['priority'],r['attribute'],r['name']) for r in scheduler.created],[(2,40,0,'PhotoLUT')])

    def test_off_and_identity_snapshot_do_not_allocate_full_surface_or_transform(self):
        for selected in (0,2):
            with self.subTest(selected=selected):
                m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture(selected)
                desire(m,self.metadata['runtime_layout'],1)
                scheduler,jobs,_=self.run_jpeg(m,[('STGJPEGB','z8_photo_lut_jpeg',(1,params))],service=False)
                self.assertEqual(encodes,[(1,desc,(0x810000000,0x810000040))])
                self.assertEqual(allocations,[(42,0x38,4096+LEASE_BYTES)])
                self.assertEqual(struct.unpack('<3Q',m.mem_read(state+40,24)),(1,0,0))
                self.assertEqual(freed,[])

    def test_nef_only_reservation_has_no_unused_full_jpeg_surface(self):
        *_,allocations,freed,encodes,owner,footer=self.jpeg_fixture(jpeg_enabled=False)
        self.assertNotEqual(owner,0)
        self.assertEqual(allocations,[(42,0x38,4096+LEASE_BYTES)])
        self.assertEqual(encodes,[])
        self.assertEqual(freed,[])

    def test_reservation_oom_and_clamped_capacity_return_to_native_admission(self):
        for capacity,available in ((5120,True),(None,False)):
            with self.subTest(capacity=capacity,available=available):
                *_,allocations,freed,encodes,owner,footer=self.jpeg_fixture(capacity=capacity,available=available)
                self.assertEqual(owner,0)
                self.assertEqual(freed,[0xa03009000] if available else [])
                self.assertEqual(encodes,[])

    def test_small_jpeg_variants_use_separate_slices_and_nef_selectors_bypass(self):
        for name,kind in (('medium',1),('small',2)):
            for selector in (0,1,2):
                with self.subTest(name=name,selector=selector):
                    m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture()
                    request,capture,private=self.preview_request(m,owner,kind)
                    self.run_jpeg(m,[('STGPJPEG','z8_photo_lut_'+name,(0,request,capture,selector))],service=False)
                    self.assertEqual(encodes[0][2],(private,private+64) if selector==0 else (0x810000000,0x810000040))
                    statistics=state+self.metadata['runtime_layout']['jpeg_preview_offset']
                    self.assertEqual(struct.unpack('<3Q',m.mem_read(statistics,24)),(1,64,0) if selector==0 else (0,0,0))
                    self.assertEqual(bytes(m.mem_read(request+0x100,0x28)),image)
                    self.assertEqual(len(allocations),1)
                    self.assertEqual(freed,[])

    def test_tiny_jpeg_duplicate_preserves_native_thumbnail_result_and_waits_for_owner(self):
        m,state,stack,params,desc,image,allocations,freed,encodes,owner,footer=self.jpeg_fixture()
        original_output=owner+0xe0;original_result=params+0x400
        m.mem_write(original_output+8,struct.pack('<Q',0x820000200))
        m.mem_write(params+0x10,struct.pack('<Q',original_output))
        m.mem_write(params+0x48,struct.pack('<Q',original_result))
        private=0x820001000+8256*5504*2+1920*1080*2+640*480*2
        m.mem_map((private+0x400000000)&~4095,8192)
        lock=footer+LEASE_MUTABLE+32+3*4
        m.mem_write(lock,struct.pack('<I',1))
        waited=[]
        def release_other_user(task):
            if waited:return
            self.assertEqual(encodes,[])
            self.assertEqual(int.from_bytes(m.mem_read(lock,4),'little'),1)
            waited.append(task['name']);m.mem_write(lock,bytes(4))
        self.run_jpeg(m,[('STGPJPEG','z8_photo_lut_thumbnail',(0,params))],service=False,on_sleep=release_other_user)
        self.assertEqual(waited,['STGPJPEG'])
        self.assertEqual([row[2] for row in encodes],[(0x810000000,0x810000040),(private,private+64)])
        self.assertEqual(int.from_bytes(m.mem_read(original_output+0x10,4),'little'),129)
        self.assertEqual(int.from_bytes(m.mem_read(footer+LEASE_MUTABLE+0x10,4),'little'),130)
        self.assertEqual(struct.unpack('<B3xI',m.mem_read(original_result,8)),(1,1))
        self.assertEqual(int.from_bytes(m.mem_read(params+0x48,8),'little'),original_result)
        self.assertEqual(int.from_bytes(m.mem_read(footer+LEASE_MUTABLE+48,4),'little'),1)
        self.assertEqual(int.from_bytes(m.mem_read(lock,4),'little'),0)
        self.assertEqual(int.from_bytes(m.mem_read(params+8,8),'little'),desc)
        self.assertEqual(bytes(m.mem_read(desc,0x28)),image)
        self.assertEqual(len(allocations),1)
        self.assertEqual(freed,[])


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

    def test_successful_bank_reset_resets_only_the_target_bank_record(self):
        layout=self.metadata['runtime_layout']
        for kind in (0,1,5,9,10,11,12,13,14):
            for success in (0,1):
                for ready in (0,1):
                    with self.subTest(kind=kind,success=success,ready=ready):
                        m,state,slot,request,stack,original,before,ids=self.fixture()
                        m.mem_map(0x41012000,4096)
                        m.mem_write(0x41012bf4,bytes.fromhex('c0035fd6'))
                        looks=bundle_ids(m)
                        persist=state+layout['persist_offset']
                        banks=[looks[1],None,looks[1],'unresolved']
                        m.mem_write(persist,record_bytes(banks))
                        m.mem_write(persist+layout['persist_ready_offset'],struct.pack('<I',ready))
                        m.mem_write(persist+layout['persist_authoritative_offset'],struct.pack('<I',ready))
                        m.mem_write(persist+layout['persist_blob_offset'],struct.pack('<I',1))   # stored record read
                        saved=request+0x400;registers=[0]*32;registers[0]=success;registers[19]=kind
                        m.mem_write(saved,struct.pack('<32Q',*registers))
                        saves=[]
                        m.hook_add(UC_HOOK_CODE,lambda uc,at,size,user:saves.append(at) if at==0x41012bf4 else None)
                        self.call(m,'z8_photo_lut_bank_reset_result',stack,x0=saved)
                        target=0 if kind==9 else kind-10
                        valid=success and 0<=target<4
                        expected=list(banks)
                        if valid and ready:expected[target]=None         # the firmware default: Off
                        self.assertEqual(bytes(m.mem_read(persist,0xc0)),record_bytes(expected))
                        # A reset before the record is decided is kept and merged when it is.
                        pending=struct.unpack('<I',m.mem_read(persist+layout['persist_pending_offset'],4))[0]
                        self.assertEqual(pending,1<<target if valid and not ready else 0)
                        self.assertEqual(saves,[0x41012bf4] if valid and ready else [])


if __name__=='__main__':unittest.main()
