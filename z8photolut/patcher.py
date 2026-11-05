"""Photo LUT feature group: native hooks, option page and runtime menu text (composed by features.py).

The group owns 0x40b50000..0x40c00000. The firmware carries no looks: the library comes only from
the card (Z8LUTLIB.BIN). Shared owners in z8patch/shared.py provide the language-table entries
(Strings), the photo-menu hide list and refresh dispatch (PhotoMenu) and the text loader floor; the
shared colour unit (z8colour) serves this group's still-develop provider block to the native
colour builders.
"""
import struct

from z8prores.menu import Image, NRAW_LAYOUT, NRAW_ROWS, NRAW_PAGE, NRAW_EVENTS, NRAW_OK, NRAW_CALLBACKS
from z8prores.package import require

BASE = 0x40160000
ALIAS = 0xA0F800000-0x40200000          # loaded-image RAM copy, writable
# Region layout. STATE is zero in the image; the text loader copies it to RAM (STATE_VA) at cold start.
CODE, CODE_END = 0x40B50000, 0x40B68000
PAGE, PAGE_END = 0x40B68000, 0x40B69000
TEXT, TEXT_BLOCK, TEXT_GROUPS = 0x40B69000, 0x800, 3
STATE, STATE_BYTES = 0x40B6B000, 0x1000
# Still develop page (firmware.h Z8StillDevelop): the colour provider block (z8colour CONFIG +0x58)
# first, then the develop record and the served Log look's ISP table copy. Zero in the image.
STILL, STILL_BYTES = 0x40B6C000, 0x1000
REGION_END = 0x40C00000
FLOOR = STILL+STILL_BYTES               # z8patch/shared.py loader floor: both zero pages are loaded
STATE_VA, STILL_VA = STATE+ALIAS, STILL+ALIAS
PROVIDER = STILL_VA
# Native still-develop builder calls the wrappers replace (call site, native target, entry).
STILL_CALLS = ((0x41053C50, 0x41112DCC, 'z8_photo_lut_still_m_entry'),
               (0x41053DA0, 0x41112DD8, 'z8_photo_lut_still_curve_entry'))
LOOK_SLOTS, OPTION_FIRST = 8, 0x35B5
# Photo shooting menu row 2 (item 0x2caa, hidden in stock) opens state 0xf8, otherwise unreachable.
PHOTO_ROWS, ROW, ITEM = 0x3C6F790, 2, 0x2CAA
ROW_EVENTS, ROW_OK_EVENTS, ROW_OK_ACTION = 0x43E094A0, (0x286, 0x29F, 0x2AE), 0x403B1BA8
STOCK_TARGET, STATE_ID = 0x1D, 0xF8
STATES, STATE_RECORD = 0x4BFFCC0, 96
EVENTS_END = 0x3BB
# Every language except Simplified (8) and Traditional (9) Chinese uses group 0.
MENU_TEXT = (
    ('Photo LUT', 'LUT for preview/JPEG; RAW unchanged. Import once; keep the card file for automatic restore.',
     'Off', 'Load LUTs from card'),
    ('照片 LUT', '预览与 JPEG 使用风格，RAW 不变。导入后开机自动恢复。请保留卡上库文件。', '关闭', '从存储卡载入 LUT'),
    ('照片 LUT', '預覽與 JPEG 使用風格，RAW 不變。匯入後開機自動恢復。請保留卡上庫檔。', '關閉', '從記憶卡載入 LUT'))
LANGUAGE_GROUPS = tuple(1 if language == 8 else 2 if language == 9 else 0 for language in range(36))


def branch(at, to, link=False):
    delta = to-at
    require(delta % 4 == 0 and -(1 << 27) <= delta < (1 << 27), 'Branch out of range')
    return struct.pack('<I', (0x94000000 if link else 0x14000000) | ((delta//4) & 0x3ffffff))


# Runtime layout table (firmware.c z8_photo_lut_runtime_layout), in order.
LAYOUT_FIELDS = (
    'state_bytes', 'preview_offset', 'slots_offset', 'slot_bytes', 'request_offset', 'channel_offset',
    'cache_resource_offset', 'paused_offset', 'apply_sequence_offset', 'flags_offset',
    'apply_counter_offset', 'display_epoch_offset', 'slot_count', 'power_paused_offset',
    'jpeg_busy_offset', 'jpeg_preview_offset', 'jpeg_worker_offset', 'card_offset', 'xmp_offset',
    'xmp_skipped_offset', 'persist_offset', 'persist_sequence_offset', 'persist_ready_offset',
    'persist_restore_offset', 'persist_source_offset', 'persist_blob_offset', 'persist_pending_offset',
    'persist_authoritative_offset', 'boots_offset', 'persist_reason_offset', 'persist_detail_offset',
    'persist_retries_offset', 'persist_library_generation_offset', 'persist_effective_offset',
    'persist_refused_forecasts_offset', 'persist_refused_allocations_offset', 'persist_commits_offset',
    'persist_blob_writes_offset', 'persist_refused_writes_offset', 'persist_blob_reads_offset',
    'persist_late_reads_offset', 'xmp_last_choice_offset', 'xmp_last_applied_offset')


def runtime_layout(code, entries):
    at = entries['z8_photo_lut_runtime_layout']-CODE
    layout = dict(zip(LAYOUT_FIELDS, struct.unpack_from(f'<{len(LAYOUT_FIELDS)}I', code, at)))
    require(layout['state_bytes'] <= STATE_BYTES, 'Photo LUT private state exceeds its page')
    return layout


# Hook entries that replay a displaced native instruction, then branch to its continuation.
DISPLACED = [('boot', '93f499d2', 0x41184df4), ('mode', 'ffc301d1', 0x410463e8),
             ('mode_exit', 'a0020012', 0x41046708), ('reset', 'e00314aa', 0x40cd92ac),
             ('construct', 'e00315aa', 0x40cd760c), ('pool_init', 'f75bbda9', 0x41045ba4),
             ('pool_init_exit', '60020012', 0x41045df8), ('shutdown', 'f40f1ef8', 0x40d67264),
             ('bank_reset_result', '95ac8352', 0x40d68208)]


def check_hooks(code, entries):
    for name, old, resume in DISPLACED:
        start = entries[f'z8_photo_lut_{name}_entry']-CODE
        at = code.find(bytes.fromhex(old), start, start+256)
        require(at >= 0 and code[at+4:at+8] == branch(CODE+at+4, resume), 'Native displaced instruction mismatch: '+name)


def text_block(group):
    """One language group's 12 strings packed as UTF-16 and their 13 start offsets.

    A string ends where the next one starts, so they stay contiguous. Order: title, help, Off,
    card-load row, eight looks, end. Firmware rewrites everything from the card-load row on
    (firmware.c menu_text) at boot and after a card load; look rows start as '-' and are hidden."""
    parts = [*MENU_TEXT[group], *('-' for _ in range(LOOK_SLOTS))]
    starts, raw = [], bytearray()
    for text in parts:
        starts.append(len(raw))
        raw.extend(text.encode('utf-16le'))
    starts.append(len(raw))
    # Room for the longest card status and eight "N. " + 63-unit names after Off.
    require(starts[3]+2*(68+LOOK_SLOTS*(3+63)) <= TEXT_BLOCK, 'Menu text exceeds its block')
    return bytes(raw).ljust(TEXT_BLOCK, b'\0'), starts


def add_menu(img, entries, strings):
    blocks = [text_block(group) for group in range(TEXT_GROUPS)]
    ids = strings.add_private('photo-lut', [(TEXT+group*TEXT_BLOCK, starts) for group, (_, starts) in enumerate(blocks)],
                              LANGUAGE_GROUPS)
    first = ids[0]
    require(TEXT+TEXT_GROUPS*TEXT_BLOCK <= STATE, 'Photo LUT text blocks reach the private state')
    img.put(TEXT-BASE, b''.join(raw for raw, _ in blocks), 'photo LUT menu text blocks (rewritten at runtime)', expect_zero=True)

    # Option page: stock N-RAW option layout, rows Off, Load LUTs from card, looks 1..8.
    layout = bytearray(img.old[NRAW_LAYOUT:NRAW_LAYOUT+0x110])
    require(struct.unpack_from('<I', layout, 0x44)[0] == 0x8a6, 'Stock option-page title differs')
    struct.pack_into('<I', layout, 0x44, first)
    template = list(struct.unpack_from('<11I', img.old, NRAW_ROWS))
    count = LOOK_SLOTS+2
    rows = bytearray()
    for i in range(count):
        # Strings FIRST+2 Off, +3 load row, +4.. looks: row i uses FIRST+2+i (firmware.h Z8_LOAD_ROW).
        row = template.copy()
        row[0] = first+2+i
        row[6] = OPTION_FIRST+i
        rows.extend(struct.pack('<11I', *row))
    events = bytearray(img.old[NRAW_EVENTS:NRAW_EVENTS+14*32])
    changes = 0
    for i in range(14):
        event, action, guard, target = struct.unpack_from('<4Q', events, i*32)
        if action == NRAW_OK:
            struct.pack_into('<Q', events, i*32+8, entries['z8_photo_lut_menu_ok'])
            changes += 1
    require(changes == 3, 'Stock option OK transitions differ')
    parts = bytearray()

    def append(data):
        while len(parts) & 7:
            parts.append(0)
        at = PAGE+len(parts)
        parts.extend(data)
        return at
    layout_va = append(layout)
    rows_va = append(rows)
    events_va = append(events)
    pointers_va = append(struct.pack('<'+'Q'*count, *[events_va]*count))
    callbacks_va = append(struct.pack('<5Q', entries['z8_photo_lut_menu_entry'], *NRAW_CALLBACKS[1:]))
    page = bytearray(img.old[NRAW_PAGE:NRAW_PAGE+0x50])
    struct.pack_into('<3Q', page, 8, layout_va, rows_va, pointers_va)
    struct.pack_into('<H', page, 0x20, count)
    page_va = append(page)
    require(PAGE+len(parts) <= PAGE_END, 'Photo LUT option page overflow')
    img.put(PAGE-BASE, bytes(parts), 'photo LUT option page: layout, rows, transitions, callbacks, page', expect_zero=True)

    record = STATES+STATE_ID*STATE_RECORD
    require(img.u64(record) == STATE_ID << 16 | 2 and img.u64(record+8) == img.u64(STATES+STOCK_TARGET*STATE_RECORD+8),
            'Stock photo-menu state 0xf8 differs')
    img.put(record+40, struct.pack('<Q', callbacks_va), 'photo LUT state 0xf8 callbacks')
    img.put(record+80, struct.pack('<Q', page_va), 'photo LUT state 0xf8 page')

    row_at = PHOTO_ROWS+ROW*44
    row = list(struct.unpack_from('<11I', img.old, row_at))
    require(row[6] == ITEM, 'Stock hidden photo row differs')
    row[0] = row[2] = first
    row[1] = first+1
    row[4] = 0x101045
    row[5] = first+2
    img.put(row_at, struct.pack('<11I', *row), 'photo LUT row 2 (item 0x2caa): label, help and value')
    table = ROW_EVENTS-BASE
    retargeted = []
    for i in range(64):
        event, action, guard, target = struct.unpack_from('<4Q', img.old, table+32*i)
        if event == EVENTS_END:
            break
        if event in ROW_OK_EVENTS:
            require(action == ROW_OK_ACTION and target == STOCK_TARGET, 'Stock row 2 OK transition differs')
            img.put(table+32*i+24, struct.pack('<Q', STATE_ID), f'photo LUT row 2 OK {event:#x}: state 0x1d -> 0xf8',
                    expect=struct.pack('<Q', STOCK_TARGET))
            retargeted.append(hex(event))
    require(len(retargeted) == len(ROW_OK_EVENTS), 'Stock row 2 OK transitions differ')
    return dict(state=hex(STATE_ID), row=ROW, item=hex(ITEM), options=count, page=hex(page_va), ok_events=retargeted,
                strings=dict(first=first, count=len(ids), text_blocks=hex(TEXT)))


# Enum values of firmware.h, in declaration order (test_photo_lut_persistence checks them).
DESIRED = ('off', 'look', 'unresolved')
SOURCES = ('none', 'stored', 'fresh', 'migrated', 'lost', 'defaults', 'retained', 'unread')
BLOBS = ('pending', 'valid', 'absent', 'corrupt', 'lost', 'unread', 'skipped')
RESTORES = ('waiting', 'reading', 'done')
EFFECTIVE = ('off', 'applied', 'restoring', 'unavailable', 'unresolved')
CARD_STATUS = ('idle', 'loaded', 'no-file', 'invalid', 'conflict', 'read-error', 'memory', 'busy', 'restoring',
               'not-in-library', 'unresolved')
LIBRARIES = ('none', 'card')


def persistence(layout):
    """Where the bank record lives and how to observe its lifecycle (read-only probes)."""
    persist = STATE_VA+layout['persist_offset']

    def probe(name, address, size, meaning, type='u32', values=None):
        entry = dict(name=name, address=hex(address), size=size, type=type, meaning=meaning)
        if values:
            entry['values'] = {str(i): v for i, v in enumerate(values)}
        return entry

    def field(name, size, meaning, values=None, type='u32'):
        return probe(name, persist+layout[f'persist_{name}_offset'], size, meaning, type, values)
    probes = [probe('state_magic', STATE_VA, 8, 'Z8LUTST2 after late boot; low bit set during next native preparation', 'u64'),
              probe('state_version', STATE_VA+8, 4, 'private state layout version (4)'),
              probe('boots', STATE_VA+layout['boots_offset'], 8, 'boot hook runs since cold power-on (state reset each boot)', 'u64'),
              probe('bank_index', 0xa03d48164, 4, 'native selected photo bank 0..3 (A..D)'),
              *(probe(f'owned_bytes_{copy}', 0xa03d482ba+copy*0x2e0, 2,
                      'photo-bank blob owned bytes: 0xffff record authoritative, 0 never written, other: pre-record selection',
                      'u16') for copy in range(5)),
              probe('record', persist, 0xc0, 'Z8BankRecord: u32 magic Z8BK, u16 version 1, u16 bytes 0xc0, u32 library '
                    '(0 none, 1 card), u32 reserved, banks A..D {u8 kind (0 off, 1 look, 2 unresolved), 3 reserved, '
                    '32-byte transform id}, sha256', 'bytes'),
              field('sequence', 4, 'record seqlock: odd while written; equal before/after a read = consistent snapshot'),
              field('ready', 4, 'record decided this boot (1) or still restoring (0)'),
              field('authoritative', 4, 'record decided from an actual read or native reset; only then stored'),
              field('source', 4, 'origin of the record this boot', SOURCES),
              field('blob', 4, 'outcome of reading the stored blob-16 region', BLOBS),
              field('pending', 4, 'changes kept until an actual read decides the record (bit per bank, 0x10 library)'),
              field('restore', 4, 'card library restore attempt', RESTORES),
              field('reason', 4, 'last card read result', CARD_STATUS),
              field('detail', 4, 'card slots of that result (1 slot 1, 2 slot 2, 3 both)'),
              field('retries', 4, 'card restore attempts this boot'),
              field('library_generation', 4, 'card library publications this boot'),
              field('effective', 4, 'selected bank, last evaluated', EFFECTIVE),
              field('refused_forecasts', 4, 'capacity forecasts withheld (selected look not applied)'),
              field('refused_allocations', 4, 'capture allocations withheld (selected look not applied)'),
              field('commits', 4, 'record store requests'),
              field('blob_writes', 4, 'blob-16 region writes with the record'),
              field('refused_writes', 4, 'blob-16 stores refused (stored record unreadable or no memory)'),
              field('blob_reads', 4, 'private tail reads by the settings bootstrap'),
              field('late_reads', 4, 'blob-16 reads after the record was decided (ignored)'),
              probe('xmp_last_choice', STATE_VA+layout['xmp_last_choice_offset'], 4, 'look position latched by the last JPEG'),
              probe('xmp_last_applied', STATE_VA+layout['xmp_last_applied_offset'], 4, 'applied bits latched by the last JPEG')]
    region = 'stock blob 16 (0xcdb0 B) then Z8BankRecord (0xc0 B); generation u32 at 0x1fff8, ENDM at 0x1fffc'
    return dict(schema=1,
                block=dict(address=hex(STATE_VA), bytes=layout['state_bytes'], magic='0x32545354554c385a', version=4,
                           record=hex(persist), record_bytes=0xc0, sequence=hex(persist+layout['persist_sequence_offset'])),
                record=dict(section=16, group=0x31, blob='0xa03dabdc0', stock_bytes=0xcdb0, bytes=0xc0, kinds=list(DESIRED),
                            libraries=list(LIBRARIES)),
                hooks=dict(settings_restored='0x4101248c', defaults=['0x41016a60', '0x41016a7c'],
                           settings_wait='0x41012924', bank_copy='0x410ddb8c', blob_write='0x41012558',
                           bank_reset_result='0x40d68204', save_request='0x41012bf4'),
                probes=probes,
                nor=[dict(name='photo-lut-record-primary', base='0x45e60000', size='0x20000', volume=16, section=16, format=region),
                     dict(name='photo-lut-record-mirror', base='0x46240000', size='0x20000', volume=16, section=16, format=region)])


def build_main(stock, code, entries, strings):
    """Photo LUT edits on the exact C3.11 EG; entries are the unit's linked symbol addresses."""
    require(CODE+len(code) <= CODE_END, 'Photo LUT code exceeds its region')
    require(not any(stock[CODE-BASE:REGION_END-BASE]), 'Photo LUT region is occupied')
    check_hooks(code, entries)
    layout = runtime_layout(code, entries)
    img = Image(stock)
    img.put(CODE-BASE, code, 'photo LUT native code', expect_zero=True)
    img.put(0x40eaeb8c-BASE, branch(0x40eaeb8c, entries['z8_photo_lut_jpeg_entry'], True),
            'main JPEG encode with an isolated input surface', expect=branch(0x40eaeb8c, 0x40e82090, True))
    img.put(0x41048e2c-BASE, branch(0x41048e2c, entries['z8_photo_lut_output_alloc_entry'], True),
            'reserve JPEG LUT input inside the native capture output owner', expect=branch(0x41048e2c, 0x41047478, True))
    img.put(0x40f98958-BASE, branch(0x40f98958, entries['z8_photo_lut_output_budget'], True),
            'include the private JPEG reservation in native capture capacity', expect=branch(0x40f98958, 0x4104886c, True))
    for at, name in [(0x40eafe58, 'medium'), (0x40eb00ec, 'small'), (0x40eb02dc, 'thumbnail')]:
        target = 'z8_photo_lut_'+name+('_entry' if name != 'thumbnail' else '')
        img.put(at-BASE, branch(at, entries[target], True), 'isolate JPEG '+name+' from NEF', expect=branch(at, 0x40e82090, True))
    img.put(0x4032b748-BASE, branch(0x4032b748, entries['z8_photo_lut_text_font_entry'], True),
            'photo LUT label font family (photo LUT strings only)', expect=branch(0x4032b748, 0x4032e19c, True))
    # Both JPEG APP1 XMP composer calls: native packet first, then a bounded
    # in-place provenance description for an applied (or no-op) latched look.
    for at in (0x406c35c4, 0x406c40ec):
        img.put(at-BASE, branch(at, entries['z8_photo_lut_xmp_entry'], True),
                'JPEG XMP provenance after the native composer', expect=branch(at, 0x406e3698, True))
    for at in (0x40eb1784, 0x40eb1dc0):
        img.put(at-BASE, branch(at, entries['z8_photo_lut_file_graph'], True),
                'serialize the dedicated JPEG thumbnail', expect=branch(at, 0x40eb17e0, True))
    # The sizing, layout and encode calls must agree throughout a capture,
    # including when the menu changes. Give NEF an independent compressed
    # image even in quality modes that normally reuse the main JPEG bytes.
    img.put(0x410590dc-BASE, branch(0x410590dc, 0x410590e0),
            'keep NEF compressed JPEG separate from the main output', expect=bytes.fromhex('60000034'))
    img.put(0x41184df0-BASE, branch(0x41184df0, entries['z8_photo_lut_boot_entry']), 'post-mapping cold/warm initialization',
            expect=bytes.fromhex('93f499d2'))
    img.put(0x40e991b0-BASE, branch(0x40e991b0, entries['z8_photo_lut_frame_entry']),
            'completed photo frame copy and LUT', expect=bytes.fromhex('e0010037'))
    img.put(0x40cd7d54-BASE, branch(0x40cd7d54, entries['z8_photo_lut_queue_entry']),
            'accepted LCD queue private image handoff', expect=bytes.fromhex('e10317aa'))
    img.put(0x410463e4-BASE, branch(0x410463e4, entries['z8_photo_lut_mode_entry']),
            'quiesce CPU LUT cache before image pool transition', expect=bytes.fromhex('ffc301d1'))
    img.put(0x41046704-BASE, branch(0x41046704, entries['z8_photo_lut_mode_exit_entry']),
            'resume preview after native pool transition result', expect=bytes.fromhex('a0020012'))
    for at, old, name in [(0x40cd92a8, 'e00314aa', 'reset'), (0x40cd7608, 'e00315aa', 'construct'),
                          (0x402d14b0, '00010035', 'applied')]:
        img.put(at-BASE, branch(at, entries[f'z8_photo_lut_{name}_entry']),
                'photo LUT display ownership '+name, expect=bytes.fromhex(old))
    img.put(0x402d1480-BASE, branch(0x402d1480, entries['z8_photo_lut_will_apply_entry'], True),
            'pin private frame before native Y address write', expect=branch(0x402d1480, 0x402ab038, True))
    for at, old, name in [(0x41045ba0, 'f75bbda9', 'pool_init'), (0x41045df4, '60020012', 'pool_init_exit'),
                          (0x40d67260, 'f40f1ef8', 'shutdown'), (0x40d619b8, 'c0035fd6', 'power_cancel')]:
        img.put(at-BASE, branch(at, entries[f'z8_photo_lut_{name}_entry']),
                'photo LUT resource lifecycle '+name, expect=bytes.fromhex(old))
    img.put(0x40d68204-BASE, branch(0x40d68204, entries['z8_photo_lut_bank_reset_result_entry']),
            'photo LUT bank reset result', expect=bytes.fromhex('95ac8352'))
    # Native preparation quarantines prior handles; post-mapping settings
    # wait reads the private tail before serialization. Native prefix loading
    # remains unchanged; the fixed-blob writer commits prefix and tail.
    for at, target, name, why in [(0x4101248c, 0x410130e0, 'settings_restored', 'quarantine prior state during native preparation'),
                                  (0x41012924, 0x40d2bd04, 'settings_wait', 'native settings wait with startup recovery'),
                                  (0x410ddb8c, 0x41013a14, 'bank_copy', 'explicit shooting-bank item copy'),
                                  (0x41016a60, 0x41016480, 'restore_defaults', 'native defaults before late private boot'),
                                  (0x41016a7c, 0x41016480, 'defaults', 'after defaults for a whole reset'),
                                  (0x41012558, 0x40d6c628, 'blob_write', 'blob-16 store with the bank record')]:
        img.put(at-BASE, branch(at, entries[f'z8_photo_lut_{name}'], True), 'photo LUT bank record: '+why,
                expect=branch(at, target, True))
    # Log looks: the still develop runs the native Log chain with the look's tables (served through
    # the shared colour unit to these two builds only); live view and other builds stay native.
    for at, native, entry in STILL_CALLS:
        img.put(at-BASE, branch(at, entries[entry], True), 'photo LUT still develop of Log looks: '+entry[14:-6],
                expect=branch(at, native, True))
    menu = add_menu(img, entries, strings)
    return img, dict(code_bytes=len(code), menu=menu, library='card only (Z8LUTLIB.BIN); no built-in looks',
                     still=dict(provider=hex(PROVIDER), page=hex(STILL), tables=hex(STILL_VA+0x100),
                                calls={hex(at): entry for at, _, entry in STILL_CALLS}, tone='S+0xbc'),
                     region=dict(code=hex(CODE), page=hex(PAGE), text=hex(TEXT), state=hex(STATE), still=hex(STILL),
                                 end=hex(REGION_END)),
                     runtime=dict(address=hex(STATE_VA), source=hex(STATE), bytes=STATE_BYTES, layout=layout),
                     persistence=persistence(layout),
                     colour_contract=dict(stage='nikon-sdr: native rendered 8-bit photo YUV before display or JPEG; '
                                          'Log looks: native Log still develop with the look ISP tables, residual on the '
                                          'JPEG input',
                                          model_conversion='integer BT.601 full-range YCbCr', camera_colorimetry_verified=False,
                                          preview='65x65 chroma cache with all256 luma samples; bilinear approximation',
                                          jpeg='RGB tetrahedral interpolation per pixel'))
