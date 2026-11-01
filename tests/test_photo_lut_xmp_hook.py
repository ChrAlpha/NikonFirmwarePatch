"""Execute the compiled JPEG XMP provenance hook on graphs from the real C3.11 builder.

Opt in with Z8_PHOTO_LUT_ROM. The per-file graph comes from the original graph
builder 0x40eb17e0 and the capture lease from the compiled allocator. Only the
native packet composer 0x406e3698 is replaced, by a stub that writes a packet
with the layout of a C3.11 main-JPEG packet (33759 bytes). This establishes
the hook's gating and in-place insertion, not physical camera output.
"""
import hashlib
import struct
import unittest
import xml.etree.ElementTree as ET

from photo_lut_fixtures import LEASE_BYTES, runtime_bundle
from test_photo_lut_native_graph import (CAPTURE, CompiledGraphMachine, JPEG_GRAPH, NEF_GRAPH, OWNER, STATE,
                                         PinnedRomTestCase, Uc, compile_code)
from z8photolut.library import look_id

try:
    from unicorn import UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    pass

COMPOSER = 0x406e3698
PACKET = 0xa12000000                  # General heap
TRANSLATE = 0x41186718                 # raw address -> CPU (cached) alias, via the boot region table
GATES = ('not ready', 'not JPEG', 'header', 'branch', 'lease', 'off', 'packet', 'not applied')
SIZE = 33759
NS = 'https://github.com/ChrAlpha/NikonFirmwarePatch/ns/photo-lut/1.0/'
RESULT = 0x5a5a


def native_packet():
    head = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
            'xmlns:xmp="http://ns.adobe.com/xap/1.0/"><xmp:CreatorTool>NIKON Z 8 Ver.03.92   </xmp:CreatorTool>'
            '<xmp:CreateDate>2026-11-01T10:54:02.24</xmp:CreateDate><xmp:Rating>0</xmp:Rating> </rdf:Description>'
            '</rdf:RDF></x:xmpmeta>').encode()
    tail = b'<?xpacket end="w"?>'
    return head+b' '*(SIZE-len(head)-len(tail))+tail


def cube(title, cycle):
    quoted = title.replace('\\', '\\\\').replace('"', '\\"')
    rows = [f'TITLE "{quoted}"', 'LUT_3D_SIZE 2']
    for b in range(2):
        for g in range(2):
            for r in range(2):
                rows.append(' '.join(map(str, (g, b, r) if cycle else (r, g, b))))
    return ('\n'.join(rows)+'\n').encode()


NAME = 'Cycle & <Co> "冬"'


@unittest.skipIf(Uc is None, 'Unicorn is required for native XMP hook checks')
class PhotoLutXmpHookTests(PinnedRomTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.code, cls.symbols, cls.metadata = compile_code()
        cls.looks = runtime_bundle([cube('Neutral', False), cube(NAME, True)])

    def machine(self, choice=2):
        machine = CompiledGraphMachine(self.rom, self.code, self.symbols, self.metadata, self.looks, choice=choice)
        machine.m.mem_map(PACKET, 0x10000)
        machine.composed = []
        def compose(uc, address, size, user):
            arguments = [uc.reg_read(getattr(regs, f'UC_ARM64_REG_X{i}')) for i in range(5)]
            machine.composed.append(arguments)
            uc.mem_write(arguments[4], native_packet()[:arguments[3] & 0xffffffff])
            uc.reg_write(regs.UC_ARM64_REG_X0, RESULT)
            uc.reg_write(regs.UC_ARM64_REG_PC, uc.reg_read(regs.UC_ARM64_REG_X30))
        machine.m.hook_add(UC_HOOK_CODE, compose, begin=COMPOSER, end=COMPOSER)
        return machine

    def composer_kind(self, machine, file_id):
        """Execute 0x406c328c..0x406c32a4 of header builder 0x406c31bc (w21 = dispatch index)."""
        m, sp = machine.m, 0x500f0000
        m.reg_write(regs.UC_ARM64_REG_SP, sp)
        m.reg_write(regs.UC_ARM64_REG_X21, file_id)
        m.reg_write(regs.UC_ARM64_REG_X27, PACKET+0x9000)
        m.mem_write(sp+0x88, b'\xff'*4)
        m.emu_start(0x406c328c, 0x406c32a8, count=16)
        return struct.unpack('<I', m.mem_read(sp+0x88, 4))[0]

    def hook(self, machine, *arguments):
        # The helper scans a 33 KB packet; allow more than the fixture's cap.
        m = machine.m
        m.reg_write(regs.UC_ARM64_REG_SP, 0x500f0000)
        m.reg_write(regs.UC_ARM64_REG_X30, 0x60000000)
        for index, value in enumerate(arguments):
            m.reg_write(getattr(regs, f'UC_ARM64_REG_X{index}'), value)
        m.emu_start(self.symbols['z8_photo_lut_xmp'], 0x60000000, count=50_000_000)
        self.assertEqual(m.reg_read(regs.UC_ARM64_REG_PC), 0x60000000)
        return m.reg_read(regs.UC_ARM64_REG_X0)

    def translate(self, machine, raw):
        """Run the original 0x41186718 the header builder uses for its packet buffer."""
        m = machine.m
        m.reg_write(regs.UC_ARM64_REG_X0, raw)
        m.reg_write(regs.UC_ARM64_REG_X30, 0x60000000)
        m.emu_start(TRANSLATE, 0x60000000, count=1000)
        return m.reg_read(regs.UC_ARM64_REG_X0)

    def telemetry(self, machine):
        layout = self.metadata['runtime_layout']
        calls = struct.unpack('<I', machine.m.mem_read(STATE+layout['xmp_offset']+20, 4))[0]
        skipped = struct.unpack('<8I', machine.m.mem_read(STATE+layout['xmp_skipped_offset'], 32))
        header, packet = struct.unpack('<2Q', machine.m.mem_read(STATE+layout['xmp_skipped_offset']+32, 16))
        appended, refused = struct.unpack('<2Q', machine.m.mem_read(STATE+layout['xmp_offset'], 16))
        return dict(calls=calls, skipped={g: n for g, n in zip(GATES, skipped) if n}, header=header, packet=packet,
                    appended=appended, refused=refused)

    def compose(self, machine, file_id=0, graph=JPEG_GRAPH, *, applied=None, kind=None, header=None, packet=PACKET):
        native_kind = machine.graph(file_id, graph)
        if applied is not None:
            machine.write32(machine.footer+244, applied)
        # The JPEG header arm derives the composer kind from its dispatch index
        # (the file id): run that original prefix. NEF never reaches this arm.
        if kind is None:
            kind = self.composer_kind(machine, file_id) if file_id in (0, 4) else native_kind
        if packet != PACKET:
            machine.m.mem_map(packet & ~0xfff, 0x10000)
        result = self.hook(machine, kind, file_id, 0xa03000000, SIZE, packet, graph+0x48 if header is None else header)
        self.assertEqual(result, RESULT)
        self.assertEqual(len(machine.composed), 1)
        return bytes(machine.m.mem_read(packet, SIZE))

    def provenance(self, packet):
        self.assertEqual(len(packet), SIZE)
        self.assertTrue(packet.endswith(b'<?xpacket end="w"?>'))
        root = ET.fromstring(packet.decode('utf-8'))
        fields = {}
        for element in root.iter():
            if element.tag.startswith('{'+NS+'}'):
                fields[element.tag[len(NS)+2:]] = element.text
        return fields

    def test_applied_main_and_secondary_jpeg_get_latched_provenance(self):
        for file_id in (0, 4):
            with self.subTest(file_id=file_id):
                packet = self.compose(self.machine(), file_id, applied=1)
                fields = self.provenance(packet)
                entry = struct.unpack_from('<6I', self.looks, 64+128)
                samples = self.looks[entry[0]:entry[0]+entry[1]]
                self.assertEqual(fields['LookName'], NAME)
                self.assertEqual(fields['TransformSHA256'], look_id(17, samples).hex())
                self.assertEqual((fields['Applied'], fields['Grid'], fields['Strength']), ('True', '17', '256'))
                self.assertEqual(fields['Rendering'], 'baked-into-jpeg-pixels')
                self.assertNotIn('NoOp', fields)
                # Only padding was consumed: the native prefix is untouched.
                original = native_packet()
                at = original.index(b'</rdf:RDF>')
                self.assertEqual(packet[:at], original[:at])

    def test_original_header_prefix_gives_composer_kind_equal_to_file_id(self):
        machine = self.machine()
        self.assertEqual([self.composer_kind(machine, f) for f in (0, 4)], [0, 4])

    def test_image_ram_packets_from_the_native_translator_are_annotated(self):
        # The header buffer is image RAM translated by 0x41186718 (0x406c32bc..c8):
        # its whole cached alias window 0xc00000000..0xe00000000 is accepted.
        for raw in (0x800020000, 0x87ff00000, 0x9fff00000):
            with self.subTest(raw=hex(raw)):
                machine = self.machine()
                packet = self.translate(machine, raw)
                self.assertEqual(packet, raw+0x400000000)
                fields = self.provenance(self.compose(machine, 4, applied=1, packet=packet))
                self.assertEqual(fields['LookName'], NAME)
                self.assertEqual(self.telemetry(machine), dict(calls=1, skipped={}, header=JPEG_GRAPH+0x48,
                                                                 packet=packet, appended=1, refused=0))

    def test_failed_off_and_unsupported_outputs_are_not_claimed(self):
        cases = {
            'main encode not applied': (dict(), dict(applied=0), 'not applied'),
            'only a secondary stage applied': (dict(), dict(applied=0b110), 'not applied'),
            'look Off': (dict(choice=0), dict(applied=1), 'off'),
            'NEF': (dict(), dict(file_id=2, graph=NEF_GRAPH, applied=1), 'not JPEG'),
            'non-JPEG kind': (dict(), dict(applied=1, kind=1), 'not JPEG'),
            'kind unlike its file id': (dict(), dict(file_id=4, applied=1, kind=0), 'not JPEG'),
            'header outside its graph': (dict(), dict(applied=1, header=JPEG_GRAPH+0x50), 'header'),
            'packet past the image alias window': (dict(), dict(applied=1, packet=0xe00000000), 'packet'),
            'packet in raw image RAM': (dict(), dict(applied=1, packet=0x900000000), 'packet'),
        }
        for label, (machine_options, compose_options, gate) in cases.items():
            with self.subTest(label=label):
                machine = self.machine(**machine_options)
                self.assertEqual(self.compose(machine, **compose_options), native_packet())
                observed = self.telemetry(machine)
                self.assertEqual((observed['calls'], observed['skipped'], observed['appended'], observed['refused']),
                                 (1, {gate: 1}, 0, 0))

    def test_unready_state_is_counted_and_uninitialized_state_is_untouched(self):
        machine = self.machine()
        machine.write32(STATE+24, 0)                                    # ready = 0 (invalid built-in library)
        self.assertEqual(self.compose(machine, applied=1), native_packet())
        self.assertEqual(self.telemetry(machine)['skipped'], {'not ready': 1})
        machine = self.machine()
        machine.m.mem_write(STATE, bytes(8))                            # no state magic
        self.assertEqual(self.compose(machine, applied=1), native_packet())
        observed = self.telemetry(machine)
        self.assertEqual((observed['calls'], observed['skipped'], observed['packet']), (0, {}, 0))

    def test_alternate_kind0_branch_and_foreign_owner_are_not_claimed(self):
        machine = self.machine()
        machine.graph(0, JPEG_GRAPH)
        machine.write32(machine.footer+244, 1)
        expected = {}
        for label, gate, mutate in [('alternate branch', 'branch', lambda: machine.m.mem_write(JPEG_GRAPH+0x4c, b'\x01')),
                                    ('owner+0x80 descriptor', 'lease', lambda: machine.write64(JPEG_GRAPH, OWNER+0x80))]:
            with self.subTest(label=label):
                saved = bytes(machine.m.mem_read(JPEG_GRAPH, 0x60))
                mutate()
                machine.composed.clear()
                self.hook(machine, 0, 0, 0xa03000000, SIZE, PACKET, JPEG_GRAPH+0x48)
                self.assertEqual(bytes(machine.m.mem_read(PACKET, SIZE)), native_packet())
                expected[gate] = 1
                self.assertEqual(self.telemetry(machine)['skipped'], expected)
                machine.m.mem_write(JPEG_GRAPH, saved)

    def test_no_op_look_is_recorded_as_not_applied(self):
        fields = self.provenance(self.compose(self.machine(choice=1), applied=0))
        self.assertEqual((fields['LookName'], fields['Applied'], fields['NoOp']), ('Neutral', 'False', 'True'))

    def test_existing_provenance_is_never_duplicated(self):
        machine = self.machine()
        first = self.compose(machine, applied=1)
        machine.composed.clear()
        machine.m.mem_write(PACKET, first)
        # A second hook pass over an already annotated packet refuses and leaves it intact.
        def keep(uc, address, size, user):
            uc.reg_write(regs.UC_ARM64_REG_X0, RESULT)
            uc.reg_write(regs.UC_ARM64_REG_PC, uc.reg_read(regs.UC_ARM64_REG_X30))
        machine.m.hook_add(UC_HOOK_CODE, keep, begin=COMPOSER, end=COMPOSER)
        self.hook(machine, 0, 0, 0xa03000000, SIZE, PACKET, JPEG_GRAPH+0x48)
        self.assertEqual(bytes(machine.m.mem_read(PACKET, SIZE)), first)


if __name__ == '__main__':
    unittest.main()
