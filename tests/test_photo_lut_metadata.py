"""Exercise the compiled z8_meta_append helper on host and on AArch64.

The helper under test is z8photolut/src/metadata.c. Every check below drives the
actual compiled C, not a Python re-implementation: a host shared library via
ctypes for behaviour, and the real freestanding AArch64 build inside Unicorn for
the firmware toolchain. Emitted XMP is parsed with xml.etree, an independent
parser that knows nothing about the emitter.

Set Z8_PHOTO_LUT_SAMPLE_JPEG=/path/to/DSC_0001.JPG to additionally splice the
helper's output into an isolated COPY of a real capture and read it back with
exiftool. That readback is a SYNTHETIC HELPER READBACK, not a camera capture:
the pixels and all other metadata come from the original file, and only the XMP
packet bytes are replaced. The original sample is never modified.
"""
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

try:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
    from unicorn import arm64_const as regs
except ImportError:
    Uc = None

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'z8photolut/src'

NS = 'https://github.com/ChrAlpha/NikonFirmwarePatch/ns/photo-lut/1.0/'
RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
SEMANTICS = ('input=nikon-rendered-sdr 8-bit full-range BT.601 YCbCr;'
             'model=RGB tetrahedral uint16 grid;output=8-bit clipped;strength=1')

APPENDED, BAD_ARGUMENT, BAD_RECORD, BAD_PACKET, NO_SPACE, DUPLICATE = range(6)

HEAD = (b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">')
CLOSE = b'</rdf:RDF></x:xmpmeta>'
TRAILER = b'<?xpacket end="w"?>'

STOCK_BODY = (b'<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
              b'<xmp:CreatorTool>NIKON Z 8 Ver.03.91   </xmp:CreatorTool>'
              b'<xmp:CreateDate>2026-01-02T03:05:23.67</xmp:CreateDate>'
              b'<xmp:Rating>0</xmp:Rating> </rdf:Description>')

IPTC_BODY = STOCK_BODY + (
    b'<rdf:Description about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
    b'<dc:creator><rdf:Seq><rdf:li>Chrys</rdf:li></rdf:Seq></dc:creator>'
    b'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">a &amp; b</rdf:li>'
    b'</rdf:Alt></dc:description></rdf:Description>'
    b'<rdf:Description about="" xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">'
    b'<photoshop:City>Shanghai</photoshop:City></rdf:Description>')

CFLAGS_COMMON = ['-O2', '-ffreestanding', '-fno-builtin', '-fno-stack-protector',
                 '-Wall', '-Wextra', '-Werror']
# The firmware build's exact flags, mirrored from z8photolut/patcher.py.
CFLAGS_NATIVE = ['--target=aarch64-none-elf', '-O2', '-ffreestanding', '-fno-builtin',
                 '-fno-stack-protector', '-fno-unwind-tables',
                 '-fno-asynchronous-unwind-tables', '-mgeneral-regs-only',
                 '-mstrict-align', '-mno-outline-atomics', '-fno-vectorize',
                 '-fno-slp-vectorize', '-Wall', '-Wextra', '-Werror']


def packet(body=STOCK_BODY, total=33759, close=CLOSE, trailer=TRAILER, head=HEAD):
    """Build a packet shaped exactly like C3.11's: content, spaces, trailer."""
    fixed = head + body + close + trailer
    if total < len(fixed):
        raise ValueError('total smaller than the structural bytes')
    return head + body + close + b' ' * (total - len(fixed)) + trailer


class Provenance(ctypes.Structure):
    _fields_ = [('name', ctypes.c_char * 64), ('id', ctypes.c_ubyte * 32),
                ('grid', ctypes.c_uint32), ('strength', ctypes.c_uint32),
                ('identity', ctypes.c_uint32),
                ('firmware', ctypes.c_char * 32)]


def record(name=b'Cycle', digest=None, grid=33, strength=256, identity=0, firmware=b''):
    r = Provenance()
    r.name = name
    source = digest if digest is not None else bytes(range(32))
    for i, b in enumerate(source):
        r.id[i] = b
    r.grid, r.strength, r.identity = grid, strength, identity
    r.firmware = firmware
    return r


def which(*names):
    for n in names:
        if shutil.which(n):
            return n
    return None


class HostHelper:
    """metadata.c + lut.c built as a host shared library, driven by ctypes."""

    def __init__(self, folder):
        cc = which('clang-19', 'clang', 'cc', 'gcc')
        if cc is None:
            raise unittest.SkipTest('a host C compiler is required')
        library = Path(folder) / 'libz8meta.so'
        subprocess.run([cc, *CFLAGS_COMMON, '-fPIC', '-shared',
                        str(SRC / 'metadata.c'), str(SRC / 'lut.c'),
                        '-o', str(library)], check=True, capture_output=True, text=True)
        self.library = ctypes.CDLL(str(library))
        self.library.z8_meta_append.restype = ctypes.c_uint
        self.library.z8_meta_append.argtypes = [
            ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(Provenance),
            ctypes.POINTER(ctypes.c_uint32)]

    def append(self, data, prov, capacity=None):
        buffer = ctypes.create_string_buffer(data, len(data))
        written = ctypes.c_uint32(0xFFFFFFFF)
        result = self.library.z8_meta_append(
            buffer, len(data) if capacity is None else capacity,
            ctypes.byref(prov), ctypes.byref(written))
        return result, buffer.raw[:len(data)], written.value


class NativeHelper:
    """The real freestanding AArch64 build, executed in Unicorn."""

    BASE = 0x40990000
    DATA = 0x50000000
    STACK = 0x51000000
    STOP = 0x60000000

    def __init__(self, folder):
        cc = which('clang-19', 'clang')
        if cc is None or Uc is None:
            raise unittest.SkipTest('clang and unicorn are required')
        if not which('aarch64-linux-gnu-ld'):
            raise unittest.SkipTest('an AArch64 binutils linker is required')
        out = Path(folder)
        objects = []
        for name in ('metadata.c', 'lut.c'):
            obj = out / (name + '.o')
            subprocess.run([cc, *CFLAGS_NATIVE, '-c', str(SRC / name), '-o', str(obj)],
                           check=True, capture_output=True, text=True)
            objects.append(str(obj))
        elf = out / 'native.elf'
        subprocess.run(['aarch64-linux-gnu-ld', f'-Ttext={self.BASE:#x}',
                        '--build-id=none', '-e', 'z8_meta_append', *objects,
                        '-o', str(elf)], check=True, capture_output=True, text=True)
        binary = out / 'native.bin'
        subprocess.run(['aarch64-linux-gnu-objcopy', '-O', 'binary', str(elf),
                        str(binary)], check=True, capture_output=True, text=True)
        self.image = binary.read_bytes()
        self.symbols = {}
        for line in subprocess.check_output(
                ['aarch64-linux-gnu-nm', str(elf)], text=True).splitlines():
            fields = line.split()
            if len(fields) == 3:
                self.symbols[fields[2]] = int(fields[0], 16)

    def append(self, data, prov):
        m = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        m.mem_map(self.BASE, (len(self.image) + 0xFFFF) & ~0xFFFF)
        m.mem_write(self.BASE, self.image)
        m.mem_map(self.DATA, (len(data) + 0x10FFF) & ~0xFFF)
        m.mem_map(self.STACK - 0x100000, 0x200000)
        m.mem_write(self.DATA, data)
        blob = bytes(memoryview(prov).cast('B'))
        m.mem_write(self.DATA + len(data) + 0x100, blob)
        written = self.DATA + len(data) + 0x800
        m.mem_write(written, struct.pack('<I', 0xFFFFFFFF))
        stop = self.STOP
        m.mem_map(stop, 0x1000)
        m.reg_write(regs.UC_ARM64_REG_SP, self.STACK)
        m.reg_write(regs.UC_ARM64_REG_X30, stop)
        m.reg_write(regs.UC_ARM64_REG_X0, self.DATA)
        m.reg_write(regs.UC_ARM64_REG_X1, len(data))
        m.reg_write(regs.UC_ARM64_REG_X2, self.DATA + len(data) + 0x100)
        m.reg_write(regs.UC_ARM64_REG_X3, written)
        m.emu_start(self.symbols['z8_meta_append'], stop, count=50_000_000)
        result = m.reg_read(regs.UC_ARM64_REG_X0) & 0xFFFFFFFF
        return (result, bytes(m.mem_read(self.DATA, len(data))),
                struct.unpack('<I', m.mem_read(written, 4))[0])


def parse_provenance(data):
    """Parse the packet with an independent XML parser and return our fields."""
    start = data.index(b'<x:xmpmeta')
    end = data.index(b'</x:xmpmeta>') + len(b'</x:xmpmeta>')
    root = ET.fromstring(data[start:end].decode('utf-8'))
    fields = {}
    for description in root.iter(f'{{{RDF}}}Description'):
        for child in description:
            if child.tag.startswith(f'{{{NS}}}'):
                fields[child.tag.split('}', 1)[1]] = child.text or ''
    return fields


class MetadataHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory(prefix='z8-meta-test-')
        cls.host = HostHelper(cls._folder.name)

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def append(self, data, prov, capacity=None):
        return self.host.append(data, prov, capacity)

    # ---- structure and length preservation --------------------------------

    def test_appends_without_changing_length_or_surrounding_bytes(self):
        original = packet()
        result, after, written = self.append(original, record())
        self.assertEqual(result, APPENDED)
        self.assertEqual(len(after), len(original))
        self.assertGreater(written, 0)

        insert = original.index(CLOSE)
        self.assertEqual(after[:insert], original[:insert],
                         'bytes before the insertion point moved')
        self.assertEqual(after[insert:insert + written].count(b'</rdf:Description>'), 1)
        self.assertEqual(after[insert + written:insert + written + len(CLOSE)], CLOSE)
        self.assertTrue(after.endswith(TRAILER))
        self.assertEqual(after.index(TRAILER), original.index(TRAILER),
                         'trailer offset moved')
        self.assertEqual(original.count(b' ') - after.count(b' '),
                         written - after[insert:insert + written].count(b' '),
                         'padding consumed does not match the inserted length')

    def test_inserted_block_is_inside_rdf_and_well_formed(self):
        result, after, _ = self.append(packet(), record())
        self.assertEqual(result, APPENDED)
        body = after[after.index(b'<rdf:RDF'):after.index(b'</rdf:RDF>')]
        self.assertIn(NS.encode(), body, 'provenance was placed outside rdf:RDF')
        fields = parse_provenance(after)
        self.assertEqual(fields['Applied'], 'True')
        self.assertEqual(fields['Rendering'], 'baked-into-jpeg-pixels')
        self.assertEqual(fields['Schema'], '1.0')
        self.assertEqual(fields['InputBasis'], 'nikon-rendered-sdr')
        self.assertEqual(fields['Algorithm'], SEMANTICS)
        self.assertNotIn('NoOp', fields)
        self.assertNotIn('FirmwareIdentity', fields)

    def test_preserves_pre_existing_user_metadata(self):
        original = packet(body=IPTC_BODY)
        result, after, _ = self.append(original, record())
        self.assertEqual(result, APPENDED)
        for fragment in (b'<xmp:CreatorTool>NIKON Z 8 Ver.03.91   </xmp:CreatorTool>',
                         b'<xmp:CreateDate>2026-01-02T03:05:23.67</xmp:CreateDate>',
                         b'<rdf:li>Chrys</rdf:li>', b'a &amp; b',
                         b'<photoshop:City>Shanghai</photoshop:City>'):
            self.assertIn(fragment, after, f'lost existing metadata: {fragment!r}')
        root = ET.fromstring(after[after.index(b'<x:xmpmeta'):
                                   after.index(b'</x:xmpmeta>') + 12].decode())
        self.assertEqual(len(list(root.iter(f'{{{RDF}}}Description'))), 4)

    # ---- record fidelity ---------------------------------------------------

    def test_sha256_strength_and_grid_are_reported_exactly(self):
        digest = hashlib.sha256(b'a photo lut transform').digest()
        for grid in (17, 33, 65):
            for strength in (1, 128, 256):
                with self.subTest(grid=grid, strength=strength):
                    result, after, _ = self.append(
                        packet(), record(digest=digest, grid=grid, strength=strength))
                    self.assertEqual(result, APPENDED)
                    fields = parse_provenance(after)
                    self.assertEqual(fields['TransformSHA256'], digest.hex())
                    self.assertEqual(fields['Strength'], str(strength))
                    self.assertEqual(fields['StrengthScale'], '256')
                    self.assertEqual(fields['Grid'], str(grid))
                    self.assertEqual(fields['Applied'], 'True')

    def test_identity_and_zero_strength_are_explicit_not_applied(self):
        for name, prov in (('identity', record(identity=1)),
                           ('zero strength', record(strength=0))):
            with self.subTest(case=name):
                result, after, _ = self.append(packet(), prov)
                self.assertEqual(result, APPENDED)
                fields = parse_provenance(after)
                self.assertEqual(fields['Applied'], 'False')
                self.assertEqual(fields['NoOp'], 'True')

    def test_optional_firmware_identity(self):
        result, after, _ = self.append(packet(), record(firmware=b'C3.11 z8photolut 1.4.a1'))
        self.assertEqual(result, APPENDED)
        self.assertEqual(parse_provenance(after)['FirmwareIdentity'],
                         'C3.11 z8photolut 1.4.a1')

    def test_claims_no_vendor_or_reapplication_semantics(self):
        _, after, _ = self.append(packet(), record())
        start = after.index(NS.encode())
        # Scan the properties only; the namespace URI itself legitimately
        # contains the project name.
        body = after[after.index(b'>', start) + 1:after.index(b'</rdf:RDF>')]
        for forbidden in (b'crs:', b'crd:', b'photoshop:', b'tiff:', b'exif:',
                          b'aux:', b'Nikon', b'Panasonic', b'Adobe',
                          b'Lightroom', b'NX Studio'):
            self.assertNotIn(forbidden, body,
                             f'inserted properties reference {forbidden!r}')
        # Every element we add is in our own namespace.
        self.assertEqual(body.count(b'<photolut:'), body.count(b'</photolut:'))
        for tag in body.split(b'<')[1:]:
            if tag.startswith(b'/'):
                tag = tag[1:]
            if tag.startswith(b'rdf:Description'):
                continue
            self.assertTrue(tag.startswith(b'photolut:'),
                            f'unexpected element {tag[:32]!r}')

    # ---- escaping ----------------------------------------------------------

    def test_escapes_xml_metacharacters_and_keeps_non_ascii(self):
        names = [
            '照片风格 Cycle',
            'A & B',
            '<not-a-tag>',
            'quote " and apos \'',
            'back\\slash',
            'mixed &<>"\' 中文 look',
            '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>',
        ]
        for text in names:
            raw = text.encode('utf-8')
            if len(raw) > 63:
                continue
            with self.subTest(name=text):
                original = packet()
                result, after, _ = self.append(original, record(name=raw))
                self.assertEqual(result, APPENDED)
                # An independent parser must round-trip the exact name.
                self.assertEqual(parse_provenance(after)['LookName'], text)
                # The raw bytes must not contain an unescaped metacharacter
                # that could break out of the element.
                inserted = after[after.index(b'<photolut:LookName>'):
                                 after.index(b'</photolut:LookName>')]
                self.assertNotIn(b'<', inserted[len(b'<photolut:LookName>'):])
                self.assertEqual(len(after), len(original))
                self.assertTrue(after.endswith(TRAILER))

    def test_maximum_length_name_at_the_bound(self):
        for text in ('x' * 63, '中' * 21):
            raw = text.encode('utf-8')
            self.assertEqual(len(raw), 63)
            with self.subTest(name=text):
                result, after, _ = self.append(packet(), record(name=raw))
                self.assertEqual(result, APPENDED)
                self.assertEqual(parse_provenance(after)['LookName'], text)

    def test_escaping_expansion_is_accounted_for_in_the_length(self):
        # 21 ampersands expand to 105 bytes; the packet must still balance.
        raw = ('&' * 21).encode()
        original = packet()
        result, after, written = self.append(original, record(name=raw))
        self.assertEqual(result, APPENDED)
        self.assertEqual(len(after), len(original))
        self.assertEqual(parse_provenance(after)['LookName'], '&' * 21)
        plain = self.append(packet(), record(name=b'x' * 21))[2]
        self.assertEqual(written - plain, 21 * (len('&amp;') - 1))

    # ---- refusals leave the packet untouched -------------------------------

    def assert_refused(self, data, prov, expected, capacity=None):
        result, after, written = self.append(data, prov, capacity)
        self.assertEqual(result, expected)
        self.assertEqual(after, data, 'a refusal modified the packet')
        self.assertEqual(written, 0xFFFFFFFF, 'a refusal wrote the out-parameter')

    def test_refuses_invalid_records(self):
        cases = {
            'empty name': record(name=b''),
            'leading space': record(name=b' Cycle'),
            'trailing space': record(name=b'Cycle '),
            'invalid utf8': record(name=b'Cyc\xffle'),
            'truncated utf8': record(name='中'.encode()[:2]),
            'control character': record(name=b'Cyc\x01le'),
            'bad grid 0': record(grid=0),
            'bad grid 16': record(grid=16),
            'bad grid 64': record(grid=64),
            'strength over 256': record(strength=257),
            'identity over 1': record(identity=2),
        }
        for label, prov in cases.items():
            with self.subTest(case=label):
                self.assert_refused(packet(), prov, BAD_RECORD)

    def test_refuses_unterminated_or_unprintable_firmware(self):
        unterminated = record()
        unterminated.firmware = b'x' * 32
        self.assert_refused(packet(), unterminated, BAD_RECORD)
        control = record(firmware=b'ver\x07sion')
        self.assert_refused(packet(), control, BAD_RECORD)

    def test_refuses_bad_arguments(self):
        self.assert_refused(packet(), record(), BAD_ARGUMENT, capacity=8)
        result = self.host.library.z8_meta_append(None, 33759,
                                                  ctypes.byref(record()), None)
        self.assertEqual(result, BAD_ARGUMENT)

    # ---- opening envelope -------------------------------------------------

    def test_supervisor_envelope_counterexample_is_refused(self):
        """../metadata-envelope-counterexample.json: no XMP/RDF opening.

        The helper previously returned APPENDED and mutated these bytes even
        though an independent parser rejects them ("junk after document
        element"). Closing tags alone must never be enough.
        """
        body = b'<?xpacket begin=""?><not-xmp></not-xmp></rdf:RDF></x:xmpmeta>'
        data = body + b' ' * 2048 + TRAILER

        # Matches the report's geometry: the <not-xmp> document element ends
        # at column 39 and the closing tags that follow are junk. (The exact
        # expat message varies by version -- the report saw "junk after
        # document element", this build says "invalid token" -- so assert only
        # that an independent parser rejects it.)
        self.assertEqual(body.index(b'</rdf:RDF>'), 39)
        with self.assertRaises(ET.ParseError):
            ET.fromstring(data[:data.index(TRAILER)].decode())

        # Pinned digest of this reconstruction. The counterexample JSON's
        # own `source_sha256` is not the digest of the packet bytes it
        # describes -- searching every space count 0..40000 against both
        # begin-attribute forms and both trailer flags found no match -- so it
        # appears to digest the reporting harness input rather than the packet.
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         'e4326c0df3f29966a6dbe3f6776fb80fc4b626ec513ae2128d2d602663698f85')

        self.assert_refused(data, record(), BAD_PACKET)

    def test_refuses_missing_or_wrong_opening_envelope(self):
        def envelope(head):
            return packet(head=head)

        cases = {
            'no xpacket PI': b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                             b'<rdf:RDF xmlns:rdf="' + RDF.encode() + b'">',
            'no xmpmeta': b'<?xpacket begin=""?><rdf:RDF xmlns:rdf="' + RDF.encode() + b'">',
            'no rdf:RDF': b'<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/">',
            'xmpmeta without xmlns:x': b'<?xpacket begin=""?><x:xmpmeta>'
                                       b'<rdf:RDF xmlns:rdf="' + RDF.encode() + b'">',
            'wrong xmlns:x value': b'<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/X">'
                                   b'<rdf:RDF xmlns:rdf="' + RDF.encode() + b'">',
            'rdf without xmlns:rdf': b'<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
                                     b'<rdf:RDF>',
            'wrong rdf namespace': b'<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
                                   b'<rdf:RDF xmlns:rdf="http://example.invalid/rdf#">',
            'xmpmeta name prefix only': b'<?xpacket begin=""?><x:xmpmetaX xmlns:x="adobe:ns:meta/">'
                                        b'<rdf:RDF xmlns:rdf="' + RDF.encode() + b'">',
            'rdf name prefix only': b'<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
                                    b'<rdf:RDFX xmlns:rdf="' + RDF.encode() + b'">',
            'element between xmpmeta and rdf': b'<?xpacket begin=""?>'
                                               b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><wrap>'
                                               b'<rdf:RDF xmlns:rdf="' + RDF.encode() + b'">',
            'unterminated PI': b'<?xpacket begin="',
            'unterminated comment': b'<?xpacket begin=""?><!-- never closed ',
        }
        for label, head in cases.items():
            with self.subTest(case=label):
                self.assert_refused(envelope(head), record(), BAD_PACKET)

    def test_envelope_cannot_be_faked_in_a_comment_or_attribute(self):
        real = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="' + RDF.encode() + b'">'
        cases = {
            'inside a comment': b'<?xpacket begin=""?><!-- ' + real + b' --><not-xmp>',
            'inside an attribute value': b'<?xpacket begin=""?><fake decoy="' + real + b'">',
            'xmlns:x only inside another value': b'<?xpacket begin=""?>'
                b'<x:xmpmeta decoy=\'xmlns:x="adobe:ns:meta/"\'>'
                b'<rdf:RDF xmlns:rdf="' + RDF.encode() + b'">',
            'xmlns:rdf only inside another value': b'<?xpacket begin=""?>'
                b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                b'<rdf:RDF decoy=\'xmlns:rdf="' + RDF.encode() + b'"\'>',
            'namespace in a trailing comment': b'<?xpacket begin=""?><not-xmp><!-- ' + real + b' -->',
        }
        for label, head in cases.items():
            with self.subTest(case=label):
                self.assert_refused(packet(head=head), record(), BAD_PACKET)

    def test_accepts_valid_envelope_variants(self):
        """Native and prebuilt shapes with different attributes and whitespace."""
        variants = {
            'c3.11 native': HEAD,
            'with xmptk': b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
                          b'<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 5.1.2">\n'
                          b' <rdf:RDF xmlns:rdf="' + RDF.encode() + b'">\n',
            'single quoted attributes': b"<?xpacket begin=''?><x:xmpmeta xmlns:x='adobe:ns:meta/'>"
                                        b"<rdf:RDF xmlns:rdf='" + RDF.encode() + b"'>",
            'comment before xmpmeta': b'<?xpacket begin=""?><!-- generated --><x:xmpmeta '
                                      b'xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="'
                                      + RDF.encode() + b'">',
            'attribute with > inside': b'<?xpacket begin=""?><x:xmpmeta note="a&gt;b >"'
                                       b' xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="'
                                       + RDF.encode() + b'">',
            'newlines and tabs': b'<?xpacket begin=""?>\n\t<x:xmpmeta\n\txmlns:x="adobe:ns:meta/"\n>'
                                 b'\n\t<rdf:RDF\n\txmlns:rdf="' + RDF.encode() + b'"\n>',
        }
        for label, head in variants.items():
            with self.subTest(case=label):
                original = packet(head=head)
                result, after, written = self.append(original, record())
                self.assertEqual(result, APPENDED, f'{label} was refused')
                self.assertEqual(len(after), len(original))
                self.assertEqual(parse_provenance(after)['Applied'], 'True')
                self.assertEqual(after[:original.index(CLOSE)],
                                 original[:original.index(CLOSE)])

    # ---- pointer aliasing -------------------------------------------------

    def test_refuses_overlapping_packet_and_record(self):
        """A writable packet must not alias the supposedly immutable record."""
        data = packet(total=4096)
        arena = ctypes.create_string_buffer(data, len(data))
        prov = record()
        blob = bytes(memoryview(prov).cast('B'))
        # Place the record inside the packet's own bytes.
        inside = ctypes.cast(ctypes.addressof(arena) + 2048,
                             ctypes.POINTER(Provenance))
        ctypes.memmove(ctypes.addressof(arena) + 2048, blob, len(blob))
        before = arena.raw[:len(data)]
        written = ctypes.c_uint32(0xFFFFFFFF)
        result = self.host.library.z8_meta_append(
            arena, len(data), inside, ctypes.byref(written))
        self.assertEqual(result, BAD_ARGUMENT)
        self.assertEqual(arena.raw[:len(data)], before,
                         'an aliasing refusal modified the packet')
        self.assertEqual(written.value, 0xFFFFFFFF)

    def test_refuses_out_parameter_inside_the_packet(self):
        data = packet(total=4096)
        arena = ctypes.create_string_buffer(data, len(data))
        prov = record()
        before = arena.raw[:len(data)]
        inside = ctypes.cast(ctypes.addressof(arena) + 1024,
                             ctypes.POINTER(ctypes.c_uint32))
        result = self.host.library.z8_meta_append(
            arena, len(data), ctypes.byref(prov), inside)
        self.assertEqual(result, BAD_ARGUMENT)
        self.assertEqual(arena.raw[:len(data)], before)

    def test_accepts_adjacent_but_disjoint_buffers(self):
        data = packet(total=4096)
        arena = ctypes.create_string_buffer(data, len(data))
        prov = record()
        written = ctypes.c_uint32(0xFFFFFFFF)
        result = self.host.library.z8_meta_append(
            arena, len(data), ctypes.byref(prov), ctypes.byref(written))
        self.assertEqual(result, APPENDED)
        self.assertGreater(written.value, 0)

    def test_refuses_malformed_and_deceptive_packets(self):
        cases = {
            'no xpacket header': b'x' * 200 + CLOSE + b' ' * 100 + TRAILER,
            'no rdf close': packet().replace(b'</rdf:RDF>', b'</rdf:RDFX>', 1),
            'no xmpmeta close': packet(close=b'</rdf:RDF>'),
            'no trailer': packet()[:-len(TRAILER)] + b' ' * len(TRAILER),
            'non-space in padding': packet()[:-len(TRAILER) - 40]
                                    + b'X' + b' ' * 39 + TRAILER,
            'reversed closing order': packet(close=b'</x:xmpmeta></rdf:RDF>'),
            'truncated trailer': packet(trailer=b'<?xpacket end="w"')[:-2]
                                 + b'  ',
            'wrong trailer flag': packet(trailer=b'<?xpacket end="z"?>'),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                self.assert_refused(data, record(), BAD_PACKET)

    def test_deceptive_closing_tags_inside_a_value_are_not_used(self):
        # A property value that looks like the structural close. The real close
        # is the last one, so the insertion must land after the decoy.
        body = (b'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                b'<dc:title>&lt;/rdf:RDF&gt;&lt;/x:xmpmeta&gt;</dc:title>'
                b'</rdf:Description>')
        original = packet(body=body)
        result, after, written = self.append(original, record())
        self.assertEqual(result, APPENDED)
        self.assertEqual(len(after), len(original))
        self.assertIn(b'<dc:title>&lt;/rdf:RDF&gt;&lt;/x:xmpmeta&gt;</dc:title>', after)
        fields = parse_provenance(after)
        self.assertEqual(fields['Applied'], 'True')
        self.assertEqual(after.index(NS.encode()) > after.index(b'<dc:title>'), True)

    def test_padding_boundaries(self):
        baseline = self.append(packet(), record())[2]
        exact = packet(total=len(HEAD + STOCK_BODY + CLOSE + TRAILER) + baseline)
        result, after, written = self.append(exact, record())
        self.assertEqual(result, APPENDED, 'exactly sufficient padding was refused')
        self.assertEqual(written, baseline)
        self.assertEqual(len(after), len(exact))
        self.assertNotIn(b'  ', after[after.index(CLOSE):after.index(TRAILER)])

        for short in (1, 2, 17, baseline - 1):
            with self.subTest(missing=short):
                tight = packet(total=len(HEAD + STOCK_BODY + CLOSE + TRAILER)
                               + baseline - short)
                self.assert_refused(tight, record(), NO_SPACE)

        self.assert_refused(packet(total=len(HEAD + STOCK_BODY + CLOSE + TRAILER)),
                            record(), NO_SPACE)

    def test_duplicate_invocation_is_refused_and_idempotent(self):
        first, once, _ = self.append(packet(), record())
        self.assertEqual(first, APPENDED)
        second, twice, written = self.append(once, record())
        self.assertEqual(second, DUPLICATE)
        self.assertEqual(twice, once, 'the duplicate refusal modified the packet')
        self.assertEqual(written, 0xFFFFFFFF)
        self.assertEqual(once.count(NS.encode()), 1)
        # A different record is still refused: one provenance block per packet.
        third, again, _ = self.append(once, record(name=b'Other', grid=65))
        self.assertEqual(third, DUPLICATE)
        self.assertEqual(again, once)

    # ---- the real packet from a capture ------------------------------------

    def test_real_capture_packet_if_available(self):
        sample = os.environ.get('Z8_PHOTO_LUT_SAMPLE_JPEG')
        if not sample or not Path(sample).is_file():
            self.skipTest('set Z8_PHOTO_LUT_SAMPLE_JPEG to a baseline JPEG')
        data, offset, length = extract_packet(Path(sample).read_bytes())
        self.assertEqual(length, 33759)
        result, after, written = self.append(data, record(name='星轨 Cycle'.encode()))
        self.assertEqual(result, APPENDED)
        self.assertEqual(len(after), len(data))
        self.assertEqual(parse_provenance(after)['LookName'], '星轨 Cycle')
        self.assertIn(b'<xmp:CreatorTool>', after)
        self.assertTrue(after.endswith(TRAILER))


def walk_segments(blob):
    """Yield (offset, marker, size) for each JPEG marker segment up to SOS."""
    at = 2
    while at < len(blob) - 1 and blob[at] == 0xFF:
        marker = blob[at + 1]
        if marker == 0xDA:
            yield at, marker, 0
            return
        size = int.from_bytes(blob[at + 2:at + 4], 'big')
        yield at, marker, size
        at += 2 + size


def extract_packet(blob):
    """Return (packet bytes, file offset, length) for the XMP APP1 segment."""
    header = b'http://ns.adobe.com/xap/1.0/\x00'
    for at, marker, size in walk_segments(blob):
        if marker == 0xE1 and blob[at + 4:at + 4 + len(header)] == header:
            start = at + 4 + len(header)
            return blob[start:at + 2 + size], start, size - 2 - len(header)
    raise AssertionError('no XMP APP1 segment found')


def find_sos(blob):
    """Offset of the real start-of-scan marker, by walking the segments."""
    for at, marker, _ in walk_segments(blob):
        if marker == 0xDA:
            return at
    raise AssertionError('no SOS marker found')


@unittest.skipIf(Uc is None, 'Unicorn is required for the AArch64 build check')
class NativeAArch64Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory(prefix='z8-meta-native-')
        cls.native = NativeHelper(cls._folder.name)

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def test_freestanding_build_appends_identically(self):
        prov = record(name='照片 LUT & Co'.encode(), grid=65, strength=192,
                      firmware=b'C3.11 1.4.a1')
        original = packet()
        result, after, written = self.native.append(original, prov)
        self.assertEqual(result, APPENDED)
        self.assertEqual(len(after), len(original))
        fields = parse_provenance(after)
        self.assertEqual(fields['LookName'], '照片 LUT & Co')
        self.assertEqual(fields['Grid'], '65')
        self.assertEqual(fields['Strength'], '192')
        self.assertEqual(fields['FirmwareIdentity'], 'C3.11 1.4.a1')
        self.assertGreater(written, 0)
        self.assertTrue(after.endswith(TRAILER))

    def test_native_and_host_builds_agree_byte_for_byte(self):
        folder = tempfile.TemporaryDirectory(prefix='z8-meta-host-')
        self.addCleanup(folder.cleanup)
        host = HostHelper(folder.name)
        for prov in (record(), record(name='中文 & "q"'.encode(), identity=1),
                     record(name=b'x' * 63, grid=17, strength=1,
                            firmware=b'fw')):
            with self.subTest(name=prov.name):
                original = packet(body=IPTC_BODY)
                a = self.native.append(original, prov)
                b = host.append(original, prov)
                self.assertEqual(a[0], b[0])
                self.assertEqual(a[2], b[2])
                self.assertEqual(a[1], b[1])

    def test_native_refusal_leaves_the_packet_unchanged(self):
        original = packet()
        result, after, written = self.native.append(original, record(grid=16))
        self.assertEqual(result, BAD_RECORD)
        self.assertEqual(after, original)
        self.assertEqual(written, 0xFFFFFFFF)


@unittest.skipUnless(shutil.which('exiftool'), 'exiftool is required')
class SyntheticReadbackTests(unittest.TestCase):
    """Splice helper output into an isolated COPY of a capture and read it back.

    SYNTHETIC HELPER READBACK, not a camera capture: only the XMP packet bytes
    are replaced; pixels and all other metadata come from the original file,
    which is never modified.
    """

    @classmethod
    def setUpClass(cls):
        sample = os.environ.get('Z8_PHOTO_LUT_SAMPLE_JPEG')
        if not sample or not Path(sample).is_file():
            raise unittest.SkipTest('set Z8_PHOTO_LUT_SAMPLE_JPEG to a baseline JPEG')
        cls.source = Path(sample)
        cls.original = cls.source.read_bytes()
        cls.original_sha = hashlib.sha256(cls.original).hexdigest()
        cls._folder = tempfile.TemporaryDirectory(prefix='z8-meta-readback-')
        cls.host = HostHelper(cls._folder.name)

    @classmethod
    def tearDownClass(cls):
        # The original sample must be bit-identical after the whole class.
        assert hashlib.sha256(cls.source.read_bytes()).hexdigest() == cls.original_sha, \
            'the original sample was modified'
        cls._folder.cleanup()

    def exiftool(self, path, *args):
        out = subprocess.check_output(['exiftool', '-j', '-G1', '-s', *args, str(path)],
                                      text=True)
        import json
        return json.loads(out)[0]

    def test_spliced_copy_reads_back_and_changes_nothing_else(self):
        data, offset, length = extract_packet(self.original)
        prov = record(name='星轨 & 夜景 "A"'.encode(), grid=33, strength=256,
                      digest=hashlib.sha256(b'look').digest(),
                      firmware=b'C3.11 z8photolut 1.4.a1')
        result, after, written = self.host.append(data, prov)
        self.assertEqual(result, APPENDED)
        self.assertEqual(len(after), length)

        copy = Path(self._folder.name) / 'spliced.JPG'
        spliced = self.original[:offset] + after + self.original[offset + length:]
        self.assertEqual(len(spliced), len(self.original), 'file size changed')
        copy.write_bytes(spliced)

        # Nothing outside the packet moved.
        self.assertEqual(spliced[:offset], self.original[:offset])
        self.assertEqual(spliced[offset + length:], self.original[offset + length:])
        sos = find_sos(self.original)
        self.assertGreater(sos, offset + length, 'SOS should follow the XMP segment')
        self.assertEqual(find_sos(spliced), sos, 'SOS offset moved')
        self.assertEqual(spliced[sos:], self.original[sos:],
                         'JPEG entropy-coded data changed')
        # Every marker segment keeps its offset, marker and size.
        self.assertEqual(list(walk_segments(spliced)), list(walk_segments(self.original)))

        # exiftool reads our generic fields from the spliced copy.
        tags = self.exiftool(copy)
        flat = {k.split(':', 1)[-1]: v for k, v in tags.items()}
        self.assertEqual(str(flat.get('Applied')), 'True')
        self.assertEqual(flat.get('LookName'), '星轨 & 夜景 "A"')
        self.assertEqual(flat.get('TransformSHA256'), hashlib.sha256(b'look').hexdigest())
        self.assertEqual(str(flat.get('Strength')), '256')
        self.assertEqual(str(flat.get('Grid')), '33')
        self.assertEqual(flat.get('Rendering'), 'baked-into-jpeg-pixels')

        # Original Nikon metadata is unchanged.
        before = self.exiftool(self.source)
        for key, value in before.items():
            if key.startswith(('Nikon', 'IFD0', 'ExifIFD', 'XMP-xmp')) \
                    and 'Directory' not in key and 'FileName' not in key:
                self.assertEqual(tags.get(key), value, f'{key} changed')

        # exiftool agrees the image payload is untouched.
        for tag in ('ImageWidth', 'ImageHeight', 'ImageSize'):
            key = next((k for k in before if k.endswith(':' + tag)), None)
            if key:
                self.assertEqual(tags.get(key), before[key], f'{tag} changed')
        self.assertEqual(
            subprocess.check_output(['exiftool', '-b', '-ThumbnailImage',
                                     str(self.source)], stderr=subprocess.DEVNULL),
            subprocess.check_output(['exiftool', '-b', '-ThumbnailImage',
                                     str(copy)], stderr=subprocess.DEVNULL),
            'the embedded thumbnail changed')

    def test_refusal_on_a_copy_leaves_the_file_identical(self):
        data, offset, length = extract_packet(self.original)
        result, after, _ = self.host.append(data, record(grid=16))
        self.assertEqual(result, BAD_RECORD)
        spliced = self.original[:offset] + after + self.original[offset + length:]
        self.assertEqual(hashlib.sha256(spliced).hexdigest(), self.original_sha,
                         'a refusal would have changed the file')


if __name__ == '__main__':
    unittest.main()
