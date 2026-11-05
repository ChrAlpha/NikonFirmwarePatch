"""Single owners for language resources, video and photo menus, bank copy and loader."""
import struct
from z8prores.package import require
from z8patch.image import BASE, branch, movz

STRING_IDS = {'opengate-nraw': (3137,5), 'pixelshift': (3142,5), 'prores': (3147,6),
              'anamorphic': (3153,8), 'h265': (3161,56), 'log': (3217,8)}
# Strings kept in their provider's own rewritable text blocks. Entry SHARED_END ends the shared
# text, so id SHARED_END is never displayed; the private ids follow it and their last entry is the
# table end mark. Tables carry this range only when the provider is selected.
PRIVATE_IDS = {'photo-lut': (3226,12)}
TEXT, TABLES, TABLES_END = 0x40A50000, 0x40A70000, 0x40AF0000
SHARED_END, STRING_END = 3225, 3238
BANK, CONFIG = 0x40980000, 0x40980F00
ROWS, EVENTS, PAGE, CALLBACK = 0x3AA4758, 0x3AA4EE8, 0x4BCC2B0, 0x4BF3A80
VIDEO_ROWS, VIDEO_EVENTS = 0x40A30000, 0x40A31000
# Photo shooting menu: hide list address (movz x1,#0x7788 -> 0x42B27788: 0x2CAA, 0x2C13, ...;
# 4-byte items) and count (mov w2,#2) passed to 0x403B20A4; refresh callback data (stock 0x405D9808).
PHOTO_LIST, PHOTO_COUNT, PHOTO_REFRESH, PHOTO_STOCK_REFRESH = 0x405D9704, 0x405D9714, 0x4BF1690, 0x405D9808
PHOTO_HIDDEN = ((0x2CAA,'photo-lut'),(0x2C13,'photo-tools'))
PHOTO_CONFIG = CONFIG+0x28              # u64 base refresh, u64 photo-lut row refresh


class Strings:
    def __init__(self):
        self.groups = {}
        self.private = {}

    def add(self, name, translations):
        first,count=STRING_IDS[name]
        require(name not in self.groups, 'Duplicate string provider')
        require(len(translations)==36 and all(len(t)==count for t in translations),'String provider shape')
        self.groups[name]=translations
        return list(range(first,first+count))

    def add_private(self,name,blocks,language_block):
        """Ids whose UTF-16 text the provider writes itself. blocks[g] = (address, count+1 byte starts
        from address, the last one ending the final string); language_block[k] picks language k's block."""
        first,count=PRIVATE_IDS[name]
        require(name not in self.private and len(language_block)==36 and set(language_block)<=set(range(len(blocks))),
                'Private string provider shape')
        require(all(len(starts)==count+1 and starts[0]==0 and list(starts)==sorted(starts) and not any(x%2 for x in starts)
                    for _,starts in blocks),'Private string block shape')
        self.private[name]=(blocks,language_block)
        return list(range(first,first+count))

    def emit(self,img):
        if not self.groups and not self.private:
            return {}
        end=STRING_END if self.private else SHARED_END
        text,tables=bytearray(),bytearray()
        for language in range(36):
            table=img.u64(0x311C5A0+8*language)-BASE
            blob=img.u64(0x387AF58+8*language)-BASE
            offsets=list(struct.unpack_from('<3137i',img.old,table))
            for ident in range(3137,SHARED_END):
                value=''
                for name,translations in self.groups.items():
                    first,count=STRING_IDS[name]
                    if first<=ident<first+count:
                        value=translations[language][ident-first]
                offsets.append(TEXT+len(text)-BASE-blob)
                text.extend(value.encode('utf-16le'))
            offsets.append(TEXT+len(text)-BASE-blob)
            for name,(blocks,language_block) in self.private.items():
                first,count=PRIVATE_IDS[name]
                require(len(offsets)==first,'Private string position')
                address,starts=blocks[language_block[language]]
                offsets.extend(address+start-BASE-blob for start in starts)
            require(len(offsets)==end+1,'Shared string shape')
            at=TABLES+len(tables)
            tables.extend(struct.pack(f'<{len(offsets)}i',*offsets))
            img.put(0x311C5A0+8*language,struct.pack('<Q',at),f'shared language directory {language}')
        require(TEXT+len(text)<=TABLES and TABLES+len(tables)<=TABLES_END,'Shared strings overflow')
        img.put(TEXT-BASE,text,'shared selected-feature strings',expect_zero=True)
        img.put(TABLES-BASE,tables,'shared 36 language offset tables',expect_zero=True)
        return dict(features=sorted([*self.groups,*self.private]),entries=end+1,text_bytes=len(text),table_bytes=len(tables),
                    text=hex(TEXT),tables=hex(TABLES),private={name:hex(blocks[0][0]) for name,(blocks,_) in self.private.items()})


class VideoMenu:
    def __init__(self,stock):
        self.stock=stock
        self.rows=[stock[ROWS+44*i:ROWS+44*(i+1)] for i in range(44)]
        self.pointers=list(struct.unpack_from('<44Q',stock,EVENTS))
        self.items=[struct.unpack_from('<I',r,24)[0] for r in self.rows]
        require(len(set(self.items))==44,'Stock video item identities')
        self.replacements={}

    def collect(self,part,item):
        matches=[i for i in range(44) if struct.unpack_from('<I',part.new,ROWS+44*i+24)[0]==item]
        require(len(matches)==1,'Feature row missing or duplicated')
        i=matches[0]
        self.add(item,bytes(part.new[ROWS+44*i:ROWS+44*(i+1)]),struct.unpack_from('<Q',part.new,EVENTS+8*i)[0])

    def add(self,item,row,pointer):
        require(item not in self.replacements and len(row)==44 and struct.unpack_from('<I',row,24)[0]==item,'Duplicate or invalid feature row')
        self.replacements[item]=(row,pointer)

    @staticmethod
    def consumed(edit):
        at,end=edit['offset'],edit['end']
        for start,stop in ((ROWS,ROWS+44*44),(EVENTS,EVENTS+44*8),(CALLBACK,CALLBACK+8)):
            if at<stop and start<end:
                require(start<=at and end<=stop,'Partial shared-menu edit')
                return True
        # Standalone wrappers used another hidden item. Keep original item IDs
        # and set each selected item's hide bit to zero instead.
        if at in (0x40572930-BASE,0x4057294C-BASE,0x40572968-BASE):
            require(end==at+4,'Unexpected hide edit')
            return True
        return False

    def emit(self,img,chosen,code,entries,callbacks):
        mask=sum(bit for name,bit in (('framing',1),('video-quality',2),('log-colour',4)) if name in chosen)
        if not mask:
            require(not self.replacements,'Unexpected video menu without video features')
            return None
        records={item:(row,ptr) for item,row,ptr in zip(self.items,self.rows,self.pointers)}
        records.update(self.replacements)
        order=list(self.items)
        if 'log-colour' in chosen:
            order.remove(0x2E38);order.insert(order.index(0x2FC1)+1,0x2E38)
        if 'video-quality' in chosen:
            order.remove(0x311B);pos=order.index(0x3118)+1
            order[pos:pos]=[0x311B,0x315A]
        require(set(order)==set(records) and len(order)==len(set(order)),'Video menu loses or duplicates an item')
        rows=b''.join(records[i][0] for i in order)
        pointers=struct.pack(f'<{len(order)}Q',*(records[i][1] for i in order))
        require(VIDEO_ROWS+len(rows)<=VIDEO_EVENTS and VIDEO_EVENTS+len(pointers)<=0x40A40000,'Shared video menu capacity')
        img.put(VIDEO_ROWS-BASE,rows,'shared final video menu rows',expect_zero=True)
        img.put(VIDEO_EVENTS-BASE,pointers,'shared final video menu transition pointers',expect_zero=True)
        old=struct.pack('<QQH',BASE+ROWS,BASE+EVENTS,44)
        img.put(PAGE+0x10,struct.pack('<QQH',VIDEO_ROWS,VIDEO_EVENTS,len(order)),'shared video menu page: final rows and count',expect=old)
        hides=[]
        for feature,site,item in (('log-colour',0x40572940,0x2E38),('video-quality',0x4057295C,0x311B),('framing',0x40572978,0x310F)):
            if feature in chosen:
                img.put(site-BASE,struct.pack('<I',0x52800002),f'show selected menu item {item:#x}',expect=bytes.fromhex('e2030032'))
                hides.append(hex(item))
        require(len(code)<=CONFIG-BANK,'Shared code reaches configuration')
        img.put(BANK-BASE,code[:entries['shared_video_end']-BANK],'shared bank and refresh code',expect_zero=True)
        ptrs=[callbacks.get(k,0) for k in ('prores','h265','anamorphic','log')]
        img.put(CONFIG-BASE,struct.pack('<II4Q',mask,0,*ptrs),'shared feature mask and row callbacks',expect_zero=True)
        img.put(CALLBACK,struct.pack('<Q',entries['shared_refresh']),'shared video refresh dispatcher',expect=struct.pack('<Q',0x40572B2C))
        img.put(0x410162EC-BASE,branch(0x410162EC,entries['shared_bank']),'shared menu-bank copy dispatcher',expect=bytes.fromhex('ea2340a9'))
        return dict(items=[hex(i) for i in order],count=len(order),visible_custom_items=hides,rows=hex(VIDEO_ROWS),events=hex(VIDEO_EVENTS),mask=mask,
                    callbacks={k:hex(v) for k,v in callbacks.items()},entries={k:hex(v) for k,v in entries.items()},native_refresh_calls=1,
                    text_slots=dict(prores='0x4a',h265='0x48',anamorphic='0x49',log='0x47'))


class PhotoMenu:
    """Photo shooting menu owner once photo-lut is selected: hidden items and refresh dispatch.

    Without photo-lut, pixel-shift keeps its own edits (hide only 0x2CAA; psm_photo_refresh).
    With it, the pixel-shift edits of the hide count and the refresh callback are consumed;
    the final hide list keeps each unselected custom row hidden, and shared_photo_refresh calls
    the base refresh (psm_photo_refresh with photo-tools, else native) then the photo-lut row."""
    def __init__(self,chosen):
        self.active='photo-lut' in chosen
        self.base=PHOTO_STOCK_REFRESH
        self.consumed_edits=[]

    def consumed(self,edit):
        if not self.active:return False
        at,end=edit['offset'],edit['end']
        for start,size in ((PHOTO_REFRESH,8),(PHOTO_COUNT-BASE,4),(PHOTO_LIST-BASE,4)):
            if at<start+size and start<end:
                require(at==start and end==start+size and start!=PHOTO_LIST-BASE,'Unexpected photo menu edit')
                if start==PHOTO_REFRESH:self.base=struct.unpack('<Q',edit['new'])[0]
                self.consumed_edits.append(hex(start))
                return True
        return False

    def emit(self,img,chosen,code,entries,row):
        require(self.active and row,'Photo menu without photo-lut')
        hidden=[item for item,feature in PHOTO_HIDDEN if feature not in chosen]
        if hidden==[0x2C13]:
            img.put(PHOTO_LIST-BASE,movz(1,0x778C),'shared photo menu hide list starts at item 0x2C13',expect=movz(1,0x7788))
        img.put(PHOTO_COUNT-BASE,movz(2,len(hidden),w=True),f'shared photo menu hides {len(hidden)} item(s)',expect=bytes.fromhex('e2031f32'))
        start=entries['shared_photo_refresh']
        require(start>=entries['shared_video_end'] and BANK+len(code)<=CONFIG,'Shared code reaches configuration')
        img.put(start-BASE,code[start-BANK:],'shared photo menu refresh dispatcher',expect_zero=True)
        img.put(PHOTO_CONFIG-BASE,struct.pack('<2Q',self.base,row),'shared photo menu refresh: base and photo-lut row',expect_zero=True)
        img.put(PHOTO_REFRESH,struct.pack('<Q',start),'shared photo menu refresh callback -> dispatcher',expect=struct.pack('<Q',PHOTO_STOCK_REFRESH))
        return dict(hidden=[hex(i) for i in hidden],base=hex(self.base),row=hex(row),dispatcher=hex(start),
                    config=hex(PHOTO_CONFIG),consumed=self.consumed_edits)


def loader(img,features):
    floor=0x408BF8F0
    if 'framing' in features:floor=max(floor,0x40980000)
    if 'photo-tools' in features:floor=max(floor,0x409F0000)
    if 'video-quality' in features:floor=max(floor,0x40A42000)
    if 'log-colour' in features:floor=max(floor,0x40B50000)
    if 'photo-lut' in features:floor=max(floor,0x40B6E000)   # z8photolut.patcher FLOOR: zero state and develop pages
    used=max([floor]+[BASE+e['end'] for e in img.edits if 0x75F8F0<=e['offset']<0xAA0000])
    end=(used+4095)&~4095
    require(end<=0x40C00000,'Text loader exceeds original mapped section')
    data=struct.pack('<II',0xD2800008|((end&65535)<<5),0xF2A00008|((end>>16)<<5))
    img.put(0x1024D8C,data,'shared text copy boundary',expect=bytes.fromhex('081e9fd26811a8f2'))
    return dict(end=hex(end),includes_zero_state=True)
