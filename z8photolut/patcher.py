"""Build one self-contained photo LUT firmware from the exact C3.11 image."""
import binascii
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

from z8prores.menu import Image, NRAW_LAYOUT, NRAW_ROWS, NRAW_PAGE, NRAW_EVENTS, NRAW_OK, NRAW_CALLBACKS
from z8prores.package import require, sha
from z8prores.patcher import public_words
from .library import ENTRY, HEADER, validate_runtime

ROOT=Path(__file__).resolve().parent
BASE=0x40160000
STOCK_SHA='f85e0138e2f5e660ece8560de62e0d12cb795e0e14983e50f20b61dce906c46c'
CODE, LUT, UI, STATE_SOURCE = 0x40990000, 0x409a0000, 0x40950000, 0x40bf0000


def branch(at,to,link=False):
    delta=to-at
    require(delta%4==0 and -(1<<27)<=delta<(1<<27),'Branch out of range')
    return struct.pack('<I',(0x94000000 if link else 0x14000000)|((delta//4)&0x3ffffff))


def compile_code(compiler='clang-19'):
    cc=shutil.which(compiler)
    require(cc is not None,'A freestanding AArch64 Clang compiler is required')
    flags=['--target=aarch64-none-elf','-O2','-ffreestanding','-fno-builtin','-fno-stack-protector',
           '-fno-unwind-tables','-fno-asynchronous-unwind-tables','-mgeneral-regs-only',
           '-mstrict-align','-mno-outline-atomics','-fno-vectorize','-fno-slp-vectorize',
           '-Wall','-Wextra','-Werror']
    sources={p.name:sha(p.read_bytes()) for p in sorted((ROOT/'src').iterdir()) if p.is_file()}
    with tempfile.TemporaryDirectory(prefix='z8-photo-lut-') as folder:
        out=Path(folder)
        objects=[]
        for name in ('lut.c','metadata.c','firmware.c','hooks.S'):
            obj=out/(name+'.o')
            subprocess.run([cc,*flags,'-c',str(ROOT/'src'/name),'-o',str(obj)],check=True,capture_output=True,text=True)
            objects.append(str(obj))
        elf=out/'photo-lut.elf'
        subprocess.run(['aarch64-linux-gnu-ld','-T',str(ROOT/'src/link.ld'),'--build-id=none',*objects,'-o',str(elf)],check=True,capture_output=True,text=True)
        require(not subprocess.check_output(['aarch64-linux-gnu-nm','-u',str(elf)]).strip(),'Native code has undefined symbols')
        symbols={}
        for line in subprocess.check_output(['aarch64-linux-gnu-nm','-n',str(elf)],text=True).splitlines():
            fields=line.split()
            if len(fields)==3: symbols[fields[2]]=int(fields[0],16)
        binary=out/'photo-lut.bin'
        subprocess.run(['aarch64-linux-gnu-objcopy','-O','binary',str(elf),str(binary)],check=True,capture_output=True,text=True)
        raw=binary.read_bytes()
        disasm=subprocess.check_output(['aarch64-linux-gnu-objdump','-d',str(elf)],text=True)
        instructions=[line.split('\t',2)[-1] for line in disasm.splitlines() if re.match(r'^\s*[0-9a-f]+:\s+[0-9a-f]{8}\s',line)]
        require(not any(re.search(r'\b(?:v|q|d|s|h|b)[0-9]+\b',i) or re.match(r'(?:svc|hvc|smc)\b',i) for i in instructions),'Unexpected SIMD, FP or system trap')
        for name,old,resume in [('boot','93f499d2',0x41184df4),
                                ('mode','ffc301d1',0x410463e8),('mode_exit','a0020012',0x41046708),
                                ('reset','e00314aa',0x40cd92ac),('construct','e00315aa',0x40cd760c),
                                ('pool_init','f75bbda9',0x41045ba4),('pool_init_exit','60020012',0x41045df8),
                                ('shutdown','f40f1ef8',0x40d67264),('bank_reset_result','95ac8352',0x40d68208)]:
            start=symbols[f'z8_photo_lut_{name}_entry']-CODE
            expected=bytes.fromhex(old)
            at=raw.find(expected,start,start+256)
            require(at>=0 and raw[at+4:at+8]==branch(CODE+at+4,resume),'Native displaced instruction mismatch')
    require(0<len(raw)<=65536,'Native code region overflow')
    require(sources=={p.name:sha(p.read_bytes()) for p in sorted((ROOT/'src').iterdir()) if p.is_file()},
            'Native sources changed during compilation')
    offset=symbols['z8_photo_lut_runtime_layout']-CODE
    layout=dict(zip(('state_bytes','preview_offset','slots_offset','slot_bytes','request_offset','channel_offset',
                     'cache_resource_offset','paused_offset','apply_sequence_offset','flags_offset',
                     'apply_counter_offset','display_epoch_offset','slot_count','power_paused_offset',
                     'jpeg_busy_offset','jpeg_preview_offset','jpeg_worker_offset','card_offset','xmp_offset'),
                    struct.unpack_from('<19I',raw,offset)))
    return raw,symbols,dict(sha256=sha(raw),bytes=len(raw),flags=flags,
        compiler=subprocess.check_output([cc,'--version'],text=True).splitlines()[0],
        sources=sources,runtime_layout=layout)


def load_code(code_source='source',compiler='clang-19'):
    if code_source=='source':
        return compile_code(compiler)
    require(code_source=='prebuilt','Select source or prebuilt code')
    profile=json.loads((ROOT.parent/'resources/photo-lut.json').read_text())
    require(profile['schema']==1,'Unsupported photo LUT code profile')
    native=profile['native']
    observed={p.name:sha(p.read_bytes()) for p in sorted((ROOT/'src').iterdir()) if p.is_file()}
    require(observed==native['sources'],'Photo LUT native sources differ from the packaged code')
    path=ROOT.parent/'resources/photo-lut.bin'
    require(path.is_file(),'Prebuilt photo LUT code is missing; use the complete matching toolkit or --code-source source')
    require(path.stat().st_size==native['bytes']<=65536,'Prebuilt photo LUT code size differs')
    code=path.read_bytes()
    require(sha(code)==native['sha256'],'Prebuilt photo LUT code digest differs')
    symbols={k:int(v,16) for k,v in profile['symbols'].items()}
    return code,symbols,native


TEXT_BLOCKS,BLOCK_BYTES,LOOK_SLOTS,FIRST_STRING=0x408d0000,0x800,8,3137
# Every language except Simplified (8) and Traditional (9) Chinese uses group 0.
MENU_TEXT=(
    ('Photo LUT','Apply a look to photo preview and JPEG; RAW is unchanged. Card looks last until power off.',
     'Off','Load LUTs from card'),
    ('照片 LUT','对照片预览和 JPEG 应用风格；RAW 不变。存储卡风格在关机前有效。','关闭','从存储卡载入 LUT'),
    ('照片 LUT','對照片預覽和 JPEG 套用風格，RAW 不變。記憶卡風格在關機前有效。','關閉','從記憶卡載入 LUT'))


def text_block(group,names):
    """One language group's 12 strings packed as UTF-16, with 13 start offsets.

    Stock tables store start offsets only: a string ends where the next one
    starts, so ours stay contiguous. Order: title, help, Off, card-load row,
    eight looks, end. Firmware rewrites everything from the card-load row on
    (firmware.c menu_text) at boot and after a card load."""
    # Looks read "N. Name" (firmware.c menu_text); unused rows are hidden.
    parts=[*MENU_TEXT[group],*(f'{i+1}. {names[i]}' if i<len(names) else '-' for i in range(LOOK_SLOTS))]
    starts,raw=[],bytearray()
    for text in parts:
        starts.append(len(raw));raw.extend(text.encode('utf-16le'))
    starts.append(len(raw))
    # Room for the longest card status and eight 63-unit names after Off.
    require(starts[3]+2*(68+LOOK_SLOTS*(3+63))<=BLOCK_BYTES,'Menu text exceeds its block')
    return bytes(raw).ljust(BLOCK_BYTES,b'\0'),starts


def add_menu(img,names,symbols):
    # Use the existing hidden photo row2 and its otherwise unreachable state1d.
    # The native pixel-shift row44 and every visible stock row remain available.
    # Rows: Off, Load LUTs from card, eight look slots.
    blocks=[text_block(group,names) for group in range(3)]
    tables=bytearray()
    first=FIRST_STRING
    for language in range(36):
        old_table=img.u64(0x311c5a0+language*8)-BASE
        old_blob=img.u64(0x387af58+language*8)-BASE
        offsets=list(struct.unpack_from('<3137i',img.old,old_table))
        group=1 if language==8 else 2 if language==9 else 0
        offsets.extend(TEXT_BLOCKS+group*BLOCK_BYTES+start-BASE-old_blob for start in blocks[group][1])
        require(len(offsets)==first+13,'Unexpected photo LUT string count')
        pointer=0x408e0000+len(tables)
        tables.extend(struct.pack('<'+'i'*len(offsets),*offsets))
        img.put(0x311c5a0+language*8,struct.pack('<Q',pointer),f'photo LUT language directory {language}')
    require(0x408e0000+len(tables)<=UI,'Photo LUT strings overflow')
    img.put(TEXT_BLOCKS-BASE,b''.join(raw for raw,_ in blocks),'photo LUT menu text blocks',expect_zero=True)
    img.put(0x408e0000-BASE,tables,'photo LUT language tables',expect_zero=True)

    layout=bytearray(img.old[NRAW_LAYOUT:NRAW_LAYOUT+0x110])
    require(struct.unpack_from('<I',layout,0x44)[0]==0x8a6,'Stock option-page title differs')
    struct.pack_into('<I',layout,0x44,first)
    template=list(struct.unpack_from('<11I',img.old,NRAW_ROWS))
    count=LOOK_SLOTS+2
    rows=bytearray()
    for i in range(count):
        # Rows: Off, Load LUTs from card, looks 1..8 (firmware.h Z8_LOAD_ROW).
        # Strings FIRST+2 Off, +3 load row, +4.. looks: row i uses FIRST+2+i.
        row=template.copy();row[0]=first+2+i;row[6]=0x35b5+i
        rows.extend(struct.pack('<11I',*row))
    events=bytearray(img.old[NRAW_EVENTS:NRAW_EVENTS+14*32])
    changes=0
    for i in range(14):
        event,action,guard,target=struct.unpack_from('<4Q',events,i*32)
        if action==NRAW_OK:
            struct.pack_into('<Q',events,i*32+8,symbols['z8_photo_lut_menu_ok']);changes+=1
    require(changes==3,'Stock option OK transitions differ')
    parts=bytearray()
    def append(data):
        while len(parts)&7: parts.append(0)
        at=UI+len(parts);parts.extend(data);return at
    layout_va=append(layout);rows_va=append(rows);events_va=append(events)
    pointers_va=append(struct.pack('<'+'Q'*count,*[events_va]*count))
    callbacks_va=append(struct.pack('<5Q',symbols['z8_photo_lut_menu_entry'],*NRAW_CALLBACKS[1:]))
    page=bytearray(img.old[NRAW_PAGE:NRAW_PAGE+0x50])
    struct.pack_into('<3Q',page,8,layout_va,rows_va,pointers_va)
    struct.pack_into('<H',page,0x20,count)
    page_va=append(page)
    require(UI+len(parts)<CODE,'Photo LUT menu region overflow')
    img.put(UI-BASE,parts,'photo LUT native option page',expect_zero=True)
    record=0x4bffcc0+29*96
    require(img.u64(record)==0x1d0002,'Stock hidden photo state differs')
    img.put(record+40,struct.pack('<Q',callbacks_va),'photo LUT state callbacks')
    img.put(record+80,struct.pack('<Q',page_va),'photo LUT state page')
    row_at=63371152+2*44
    row=list(struct.unpack_from('<11I',img.old,row_at))
    require(row[6]==0x2caa,'Stock hidden photo row differs')
    row[0]=row[2]=first;row[1]=first+1;row[4]=0x101045;row[5]=first+2
    img.put(row_at,struct.pack('<11I',*row),'photo LUT row in photo shooting menu')
    img.put(0x405d9704-BASE,struct.pack('<I',0xd2800001|(0x778c<<5)),
            'photo menu hide list starts at remaining hidden row',expect=struct.pack('<I',0xd2800001|(0x7788<<5)))
    img.put(0x405d9714-BASE,struct.pack('<I',0x52800022),'photo menu hides one remaining row',expect=bytes.fromhex('e2031f32'))
    img.put(79632016,struct.pack('<Q',symbols['z8_photo_lut_photo_refresh']),'photo LUT selected look value',expect=struct.pack('<Q',0x405d9808))
    return dict(state=29,item=0x2caa,options=count,page=hex(page_va))


def build(stock,luts,version,tag,compiler='clang-19',code_source='source'):
    require(len(stock)==81807711 and sha(stock)==STOCK_SHA,'Exact original C3.11 EG is required')
    validate_runtime(luts)
    public_words(version)
    require(re.fullmatch(r'[a-z][0-9]',tag),'Tag must be lowercase letter plus digit')
    code,symbols,native=load_code(code_source,compiler)
    img=Image(stock)
    img.put(CODE-BASE,code,'photo LUT native code',expect_zero=True)
    img.put(LUT-BASE,luts,'photo LUT immutable built-in look bundle (selection marker 0)',expect_zero=True)
    require(not any(stock[STATE_SOURCE-BASE:STATE_SOURCE-BASE+4096]),'Private runtime source is occupied')
    img.put(0x40eaeb8c-BASE,branch(0x40eaeb8c,symbols['z8_photo_lut_jpeg_entry'],True),
            'main JPEG encode with an isolated input surface',expect=branch(0x40eaeb8c,0x40e82090,True))
    img.put(0x41048e2c-BASE,branch(0x41048e2c,symbols['z8_photo_lut_output_alloc_entry'],True),
            'reserve JPEG LUT input inside the native capture output owner',expect=branch(0x41048e2c,0x41047478,True))
    img.put(0x40f98958-BASE,branch(0x40f98958,symbols['z8_photo_lut_output_budget'],True),
            'include the private JPEG reservation in native capture capacity',expect=branch(0x40f98958,0x4104886c,True))
    for at,name in [(0x40eafe58,'medium'),(0x40eb00ec,'small'),(0x40eb02dc,'thumbnail')]:
        target='z8_photo_lut_'+name+('_entry' if name!='thumbnail' else '')
        img.put(at-BASE,branch(at,symbols[target],True),'isolate JPEG '+name+' from NEF',expect=branch(at,0x40e82090,True))
    img.put(0x4032b748-BASE,branch(0x4032b748,symbols['z8_photo_lut_text_font_entry'],True),
            'photo LUT label font family (photo LUT strings only)',expect=branch(0x4032b748,0x4032e19c,True))
    # Both JPEG APP1 XMP composer calls: native packet first, then a bounded
    # in-place provenance description for an applied (or no-op) latched look.
    for at in (0x406c35c4,0x406c40ec):
        img.put(at-BASE,branch(at,symbols['z8_photo_lut_xmp_entry'],True),
                'JPEG XMP provenance after the native composer',expect=branch(at,0x406e3698,True))
    for at in (0x40eb1784,0x40eb1dc0):
        img.put(at-BASE,branch(at,symbols['z8_photo_lut_file_graph'],True),
                'serialize the dedicated JPEG thumbnail',expect=branch(at,0x40eb17e0,True))
    # The sizing, layout and encode calls must agree throughout a capture,
    # including when the menu changes. Give NEF an independent compressed
    # image even in quality modes that normally reuse the main JPEG bytes.
    img.put(0x410590dc-BASE,branch(0x410590dc,0x410590e0),
            'keep NEF compressed JPEG separate from the main output',expect=bytes.fromhex('60000034'))
    img.put(0x41184df0-BASE,branch(0x41184df0,symbols['z8_photo_lut_boot_entry']),'post-mapping cold/warm initialization',expect=bytes.fromhex('93f499d2'))
    img.put(0x40e991b0-BASE,branch(0x40e991b0,symbols['z8_photo_lut_frame_entry']),
            'completed photo frame copy and LUT',expect=bytes.fromhex('e0010037'))
    img.put(0x40cd7d54-BASE,branch(0x40cd7d54,symbols['z8_photo_lut_queue_entry']),
            'accepted LCD queue private image handoff',expect=bytes.fromhex('e10317aa'))
    img.put(0x410463e4-BASE,branch(0x410463e4,symbols['z8_photo_lut_mode_entry']),
            'quiesce CPU LUT cache before image pool transition',expect=bytes.fromhex('ffc301d1'))
    img.put(0x41046704-BASE,branch(0x41046704,symbols['z8_photo_lut_mode_exit_entry']),
            'resume preview after native pool transition result',expect=bytes.fromhex('a0020012'))
    for at,old,name in [(0x40cd92a8,'e00314aa','reset'),(0x40cd7608,'e00315aa','construct'),
                        (0x402d14b0,'00010035','applied')]:
        img.put(at-BASE,branch(at,symbols[f'z8_photo_lut_{name}_entry']),
                'photo LUT display ownership '+name,expect=bytes.fromhex(old))
    img.put(0x402d1480-BASE,branch(0x402d1480,symbols['z8_photo_lut_will_apply_entry'],True),
            'pin private frame before native Y address write',expect=branch(0x402d1480,0x402ab038,True))
    for at,old,name in [(0x41045ba0,'f75bbda9','pool_init'),(0x41045df4,'60020012','pool_init_exit'),
                        (0x40d67260,'f40f1ef8','shutdown'),(0x40d619b8,'c0035fd6','power_cancel')]:
        img.put(at-BASE,branch(at,symbols[f'z8_photo_lut_{name}_entry']),
                'photo LUT resource lifecycle '+name,expect=bytes.fromhex(old))
    for at,old,name in [(0x41016350,'020540b9','bank'),(0x410169c8,'020540b9','bank_default'),
                        (0x41016874,'220540b9','current_default'),(0x40d68204,'95ac8352','bank_reset_result')]:
        img.put(at-BASE,branch(at,symbols[f'z8_photo_lut_{name}_entry']),
                'photo LUT setting '+name.replace('_',' '),expect=bytes.fromhex(old))
    # Group6 has five 0x2e0-byte photo banks. Original item0x600 occupies
    # bytes0..5, item0x601 starts at+8. Getter/setter length stays6.
    require(stock[0x3ece4dc:0x3ece4e4]==b'NCZ_8\0\0\0','Photo folder defaults/alignment differ')
    require(struct.unpack_from('<6I',stock,0x3eabb98)[:2]==(10,6),'Photo folder descriptor differs')
    initial=struct.unpack_from('<I',luts,20)[0]
    img.put(0x3ece4e2,bytes([initial,0]),'photo LUT default selection and built-in library marker',expect=bytes(2))
    names=[luts[HEADER+i*ENTRY+24:HEADER+i*ENTRY+88].split(b'\0',1)[0].decode() for i in range(struct.unpack_from('<I',luts,16)[0])]
    menu=add_menu(img,names,symbols)
    menu['default']='Off' if initial==0 else names[initial-1]
    end=STATE_SOURCE+4096
    loader=struct.pack('<II',0xd2800008|((end&65535)<<5),0xf2a00008|((end>>16)<<5))
    img.put(0x1024d8c,loader,'text loader includes code, LUTs, menu and private state',expect=bytes.fromhex('081e9fd26811a8f2'))
    identity=f'{version}.{tag}'.encode()
    for at,data,why in [(0xeb29fc,public_words(version),'public version'),(0x14eaa90,identity,'runtime identity'),(len(stock)-11,identity,'trailer identity')]:
        img.put(at,data,why)
    img.put(len(stock)-2,binascii.crc_hqx(img.new[:-2],0).to_bytes(2,'big'),'EG CRC16')
    raw=bytes(img.new)
    require(binascii.crc_hqx(raw,0)==0,'EG checksum mismatch')
    end=0;edits=[]
    for e in sorted(img.edits,key=lambda e:e['offset']):
        require(end<=e['offset'] and raw[end:e['offset']]==stock[end:e['offset']],'Undeclared payload modification')
        edits.append(dict(offset=e['offset'],bytes=len(e['new']),purpose=e['purpose'],before=sha(e['old']),after=sha(e['new'])))
        end=e['end']
    require(raw[end:]==stock[end:],'Undeclared trailing modification')
    return raw,dict(native=native,menu=menu,symbols={k:hex(v) for k,v in symbols.items()},
        edits=edits,all_bytes_outside_edits_preserved=True,lut_sha256=sha(luts),
        runtime=dict(address='0xa101f0000',source='0x40bf0000',bytes=4096,layout=native['runtime_layout']),
        settings=dict(group=6,current='0xa03d482ba',banks=5,stride=0x2e0,
                      copy_hooks=['0x41016350','0x410169c8','0x41016874'],reset_success_hook='0x40d68204',save_request='0x41012bf4'),
        color_contract=dict(stage='native rendered 8-bit photo YUV before display or JPEG',
                            model_conversion='integer BT.601 full-range YCbCr',camera_colorimetry_verified=False,
                            preview='65x65 chroma cache with all256 luma samples; bilinear approximation',
                            jpeg='RGB tetrahedral interpolation per pixel'),
        acceptance=dict(full_jpeg=False,continuous_preview=False,physical_camera=False))
