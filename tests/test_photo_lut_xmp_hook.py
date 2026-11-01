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
from test_photo_lut_native_graph import (CAPTURE, CompiledGraphMachine, JPEG_GRAPH, NEF_GRAPH, OWNER,
                                         PinnedRomTestCase, Uc, compile_code)
from z8photolut.library import look_id

try:
    from unicorn import UC_HOOK_CODE
    from unicorn import arm64_const as regs
except ImportError:
    pass

COMPOSER = 0x406e3698
PACKET = 0xa12000000
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

    def compose(self, machine, file_id=0, graph=JPEG_GRAPH, *, applied=None, kind=None, header=None):
        native_kind = machine.graph(file_id, graph)
        if applied is not None:
            machine.write32(machine.footer+244, applied)
        # The JPEG header arm derives the composer kind from its dispatch index
        # (the file id): run that original prefix. NEF never reaches this arm.
        if kind is None:
            kind = self.composer_kind(machine, file_id) if file_id in (0, 4) else native_kind
        result = self.hook(machine, kind, file_id, 0xa03000000, SIZE, PACKET, graph+0x48 if header is None else header)
        self.assertEqual(result, RESULT)
        self.assertEqual(len(machine.composed), 1)
        return bytes(machine.m.mem_read(PACKET, SIZE))

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

    def test_failed_off_and_unsupported_outputs_are_not_claimed(self):
        cases = {
            'main encode not applied': dict(machine=dict(), compose=dict(applied=0)),
            'only a secondary stage applied': dict(machine=dict(), compose=dict(applied=0b110)),
            'look Off': dict(machine=dict(choice=0), compose=dict(applied=1)),
            'NEF': dict(machine=dict(), compose=dict(file_id=2, graph=NEF_GRAPH, applied=1)),
            'non-JPEG kind': dict(machine=dict(), compose=dict(applied=1, kind=1)),
            'kind unlike its file id': dict(machine=dict(), compose=dict(file_id=4, applied=1, kind=0)),
            'header outside its graph': dict(machine=dict(), compose=dict(applied=1, header=JPEG_GRAPH+0x50)),
        }
        for label, case in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.compose(self.machine(**case['machine']), **case['compose']), native_packet())

    def test_alternate_kind0_branch_and_foreign_owner_are_not_claimed(self):
        machine = self.machine()
        machine.graph(0, JPEG_GRAPH)
        machine.write32(machine.footer+244, 1)
        for label, mutate in [('alternate branch', lambda: machine.m.mem_write(JPEG_GRAPH+0x4c, b'\x01')),
                              ('owner+0x80 descriptor', lambda: machine.write64(JPEG_GRAPH, OWNER+0x80))]:
            with self.subTest(label=label):
                saved = bytes(machine.m.mem_read(JPEG_GRAPH, 0x60))
                mutate()
                machine.composed.clear()
                self.hook(machine, 0, 0, 0xa03000000, SIZE, PACKET, JPEG_GRAPH+0x48)
                self.assertEqual(bytes(machine.m.mem_read(PACKET, SIZE)), native_packet())
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
