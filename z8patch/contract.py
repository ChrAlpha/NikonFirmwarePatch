"""Independent structural checks, using fixed firmware facts and group semantics.

No composer, assembler, linker or resource-generator imports.
"""
import struct


def verify_contract(old,new,features):
    def need(ok,message):
        if not ok:raise ValueError('Composition contract: '+message)
    base=0x40160000
    read=lambda image,fmt,at:struct.unpack_from(fmt,image,at)
    framing,quality,log,photo,lut=(s in features for s in ('framing','video-quality','log-colour','photo-tools','photo-lut'))
    video=framing or quality or log
    if video:
        rows,events,count=read(new,'<QQH',0x4BCC2C0)
        need((rows,events,count)==(0x40A30000,0x40A31000,45 if quality else 44),'video menu allocation/count')
        ids=[read(new,'<I',rows-base+44*i+24)[0] for i in range(count)]
        original=[read(old,'<I',0x3AA4758+44*i+24)[0] for i in range(44)]
        need(len(ids)==len(set(ids)) and set(ids)==set(original)|({0x315A} if quality else set()),'video menu items')
        if log:need(ids.index(0x2E38)==ids.index(0x2FC1)+1,'Log after file type')
        if quality:need(ids.index(0x311B)==ids.index(0x3118)+1 and ids.index(0x315A)==ids.index(0x311B)+1,'quality rows order')
        for enabled,site,item,load in ((log,0x40572940,0x2E38,0x40572930),(quality,0x4057295C,0x311B,0x4057294C),(framing,0x40572978,0x310F,0x40572968)):
            need(new[load-base:load-base+4]==old[load-base:load-base+4],f'original hidden-item identity {item:#x}')
            need(new[site-base:site-base+4]==(bytes.fromhex('02008052') if enabled else old[site-base:site-base+4]),f'visibility {item:#x}')
        mask=read(new,'<I',0x40980F00-base)[0]
        need(mask==int(framing)+2*int(quality)+4*int(log),'shared feature mask')
        ptrs=read(new,'<4Q',0x40980F08-base)
        need([bool(p) for p in ptrs]==[quality,quality,framing,log],'shared callback presence')
        need(0x40980000<=read(new,'<Q',0x4BF3A80)[0]<0x40980F00,'shared refresh entry')
    else:
        need(new[0x4BCC2B0:0x4BCC300]==old[0x4BCC2B0:0x4BCC300],'photo-only video page preservation')
        need(new[0xEB62EC:0xEB62F0]==old[0xEB62EC:0xEB62F0],'photo-only bank preservation')
    # 36 offset arrays: native text offsets are byte-for-byte retained, added IDs
    # are present exactly for their selected modules. String IDs stay fixed across masks.
    # Photo LUT strings follow one boundary id (3225) and live in its runtime-rewritten
    # text blocks (English, Simplified and Traditional Chinese); they end the table.
    ranges=[(3137,3142,framing),(3142,3147,photo),(3147,3153,quality),
            (3153,3161,framing),(3161,3217,quality),(3217,3225,log)]
    entries=3239 if lut else 3226
    for language in range(36):
        table=read(new,'<Q',0x311C5A0+8*language)[0]
        original_table=read(old,'<Q',0x311C5A0+8*language)[0]
        need(table==0x40A70000+language*entries*4,'language table position')
        offsets=read(new,f'<{entries}i',table-base)
        need(offsets[:3137]==read(old,'<3137i',original_table-base),'native string offsets')
        blob=read(new,'<Q',0x387AF58+8*language)[0]
        for lo,hi,enabled in ranges:
            for ident in range(lo,hi):
                a,b=blob+offsets[ident],blob+offsets[ident+1]
                need(0x40A50000<=a<=b<=0x40A70000,'added string bounds')
                need(bool(b-a)==enabled,'selected string presence')
                new[a-base:b-base].decode('utf-16le')
        need(0x40A50000<=blob+offsets[3225]<=0x40A70000,'shared string end')
        if lut:
            block=0x40B69000+0x800*(1 if language==8 else 2 if language==9 else 0)
            for ident in range(3226,3238):
                a,b=blob+offsets[ident],blob+offsets[ident+1]
                need(block<=a<b<=block+0x800 and a%2==0 and b%2==0,'photo LUT string bounds')
                new[a-base:b-base].decode('utf-16le')
    # Photo shooting menu: an unselected custom row stays hidden (0x2CAA photo-lut, 0x2C13 photo-tools).
    hidden=[item for item,enabled in ((0x2CAA,lut),(0x2C13,photo)) if not enabled]
    need(new[0x479704:0x479708]==struct.pack('<I',0xD28EF181 if hidden==[0x2C13] else 0xD28EF101),'photo menu hide list')
    need(new[0x479714:0x479718]==(struct.pack('<I',0x52800002|len(hidden)<<5) if len(hidden)<2 else old[0x479714:0x479718]),'photo menu hide count')
    refresh=read(new,'<Q',0x4BF1690)[0]
    if lut:
        need(0x40980000<=refresh<0x40980F00,'photo menu refresh dispatcher')
        base_refresh,row=read(new,'<2Q',0x40980F28-base)
        need(0x40990000<=base_refresh<0x409D8000 if photo else base_refresh==0x405D9808,'photo menu base refresh')
        need(0x40B50000<=row<0x40B68000,'photo LUT row refresh')
        item=read(new,'<11I',0x3C6F790+2*44)
        need(item[6]==0x2CAA and item[0]==item[2]==3226 and item[1]==3227 and item[5]==3228,'photo LUT row')
        events=[read(new,'<4Q',0x3CA94A0+32*i) for i in range(12)]
        need([e[3] for e in events if e[0] in (0x286,0x29F,0x2AE)]==[0xF8]*3,'photo LUT row opens state 0xf8')
        callbacks,page=read(new,'<Q',0x4C059C0+40)[0],read(new,'<Q',0x4C059C0+80)[0]
        need(0x40B68000<=callbacks<0x40B69000 and 0x40B68000<=page<0x40B69000,'photo LUT option page')
    # Log looks: the still-develop and live-view colour builder calls reach photo LUT code, and the shared
    # colour unit serves the provider lanes at the start of the zero develop pages (RAM alias of 0x40B6C000)
    # through its lane hooks; without photo-lut it is the base part alone, entered directly.
    def target(site):
        word=read(new,'<I',site-base)[0]
        return word>>26,site+(((word&0x3FFFFFF)^0x2000000)-0x2000000)*4
    for site in (0x41053C50,0x41053DA0,0x41052C14,0x41052E04):
        kind,to=target(site)
        need(kind==0x25 and (0x40B50000<=to<0x40B68000 if lut else new[site-base:site-base+4]==old[site-base:site-base+4]),
             'photo LUT colour build call')
    need(read(new,'<Q',0x40981F58-base)[0]==(0xA1016C000 if lut else 0),'colour provider lanes')
    if log or lut:
        entries=[target(site) for site in (0x4111A508,0x4111A6D4,0x41116CB4,0x41116010,0x4111A198)]
        need(all(kind==5 for kind,_ in entries),'colour chain sites')
        need(all((0x40981268<=to<0x40981F00) if lut else (0x40981000<=to<0x40981268) for _,to in entries),'colour lane hooks')
        if not lut:need(not any(new[0x40981268-base:0x40981F00-base]),'colour base part alone')
    if not lut:
        need(not any(new[0x40B50000-base:0x40C00000-base]),'photo LUT region unused')
        need(0x40990000<=refresh<0x409D8000 if photo else refresh==0x405D9808,'photo menu refresh')
    loader=read(new,'<II',0x1024D8C)
    end=((loader[0]>>5)&65535)|(((loader[1]>>5)&65535)<<16)
    need(0x40AE2000<=end<=0x40C00000 and end%4096==0,'copy boundary covers strings')
    if log:need(end>=0x40B50000 and not any(new[0x40B0E000-base:0x40B0F100-base]),'Log private state initialized')
    if lut:need(end>=0x40B6E000 and not any(new[0x40B6B000-base:0x40B6E000-base]),'Photo LUT private state and develop pages initialized')
    if quality:need(new[0x40A40000-base]==0,'star force initially clear')
    if framing:need(not any(new[0x4097F000-base:0x40980000-base]),'AP state initialized')
    return dict(status='PASS',video_menu=video,photo_menu_hidden=[hex(i) for i in hidden],language_tables=36,offsets_per_language=entries,
                exact_group_visibility=True,zero_state=True,loader_end=hex(end))
