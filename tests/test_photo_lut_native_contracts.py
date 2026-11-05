"""Pin the C3.11 kernel and pool contracts the photo LUT JPEG worker relies on.

Opt in with Z8_PHOTO_LUT_ROM=/path/to/z8-main-payload.bi. Each entry names an
exact instruction in the pinned ROM and the property it establishes, so the
worker's start, failure and lifetime reasoning is checked against the actual
code rather than restated. These are static contracts, not camera timing.
"""
import hashlib
import os
from pathlib import Path
import unittest

try:
    import capstone
except ImportError:
    capstone=None

ROM_SHA256='2b43a31c9de838f84b12416de4348a7004e65be07f853ec56ec2b10f3a8e3853'
BASE=0x40160000

CONTRACTS={
    'task start wrapper 40d2d5cc starts only ids >= 1 and discards the OS result':[
        (0x40d2d5d4,'ldr w8, [x19]'),(0x40d2d5d8,'cmp w8, #1'),(0x40d2d5dc,'b.lt #0x40d2d600'),
        (0x40d2d5e4,'bl #0x40183490'),(0x40d2d5f0,'cbz w8, #0x40d2d5fc'),
        (0x40d2d5f4,'bl #0x4018576c'),(0x40d2d5fc,'bl #0x401855e0'),(0x40d2d604,'b #0x40183490')],
    'context check 40183490 reports interrupt nesting only':[
        (0x401834a4,'ldr w9, [x9, #0x50]'),(0x401834b0,'cset w0, ne')],
    'OS start 401855e0 fails only for ISR/masked context or an invalid id; a dormant task starts':[
        (0x40185604,'cbnz w9, #0x40185664'),(0x4018560c,'tbnz w8, #7, #0x40185664'),
        (0x40185664,'mov w19, #-0x19'),(0x40185610,'subs w8, w0, #1'),(0x40185614,'b.lt #0x40185678'),
        (0x4018562c,'cmp w9, w0'),(0x40185630,'b.lt #0x4018567c'),(0x4018567c,'mov w19, #-0x12'),
        (0x401856e4,'ldrb w8, [x0, #0x18]'),(0x401856e8,'cbz w8, #0x40185710')],
    'OS create 40185064 takes a free TCB before allocating a stack; failures return E_NOID/E_NOMEM':[
        (0x401851b8,'ldr x20, [x21]'),(0x401851bc,'cmp x20, x21'),(0x401851c0,'b.eq #0x4018527c'),
        (0x40185350,'mov w0, #-0x22'),(0x401851c4,'cbz x8, #0x40185324'),
        (0x40185330,'bl #0x4016f738'),(0x40185338,'cbz x8, #0x401853d4'),(0x401853d4,'mov w0, #-0x21')],
    'task create wrapper registers only ids 1..0x300':[
        (0x40d2cab0,'sub w8, w1, #1'),(0x40d2cab4,'cmp w8, #0x2ff'),(0x40d2cab8,'b.hi #0x40d2cb14')],
    'event wait 40d2bdac uses OR wait with clear':[
        (0x40d2bdcc,'mov w2, #0x21'),(0x40d2bdd4,'bl #0x4017c040')],
    'mode transition destroys a pool only when it is entirely free, else keeps it busy':[
        (0x41046554,'cbz x9, #0x4104658c'),(0x4104655c,'bl #0x40cdafa0'),(0x41046560,'tbz w0, #0, #0x41046578'),
        (0x41046568,'str xzr, [x28, x22, lsl #3]'),(0x4104656c,'bl #0x40cda990'),
        (0x41046588,'strb w23, [x8, #2]')],
    'pool predicate 40cdafa0 compares live free units with the configured total':[
        (0x40cdafe4,'bl #0x40cdb020'),(0x40cdb05c,'bl #0x40cdadb8'),
        (0x40cdb088,'cmp w0, w8'),(0x40cdb08c,'cset w0, eq')],
    'file class 4086cd48 is the packed id low nibble with F mapped to E (photo LUT XMP gate calls it)':[
        (0x4086cd48,'and w8, w0, #0xf'),(0x4086cd4c,'cmp w8, #0xf'),(0x4086cd50,'mov w9, #0xe'),
        (0x4086cd54,'csel w0, w9, w8, eq'),(0x4086cd58,'ret ')],
    'graph builder keeps the full file id at graph+0x48 and dispatches on its class':[
        (0x40eb17fc,'mov w22, w0'),(0x40eb180c,'str w22, [x19, #0x48]'),(0x40eb1814,'bl #0x4086cd48')],
    'both callers pass the graph return as header dispatch and graph+0x48 as header ctx':[
        (0x40eb1784,'bl #0x40eb17e0'),(0x40eb1788,'mov w21, w0'),(0x40eb17a8,'mov w1, w21'),
        (0x40eb17ac,'add x2, x8, #0x48'),(0x40eb17b0,'bl #0x406cc130'),
        (0x40eb1dc0,'bl #0x40eb17e0'),(0x40eb1dc4,'mov w22, w0'),(0x40eb1de4,'mov w1, w22'),
        (0x40eb1de8,'add x2, x8, #0x48'),(0x40eb1dec,'bl #0x406cc130'),(0x406cc130,'b #0x406c31bc')],
    'JPEG graph returns settings+0x130 ? 4 : 0':[
        (0x40eb1b0c,'ldrb w8, [x8, #0x130]'),(0x40eb1b1c,'cmp w8, #0'),(0x40eb1b20,'mov w8, #4'),
        (0x40eb1b24,'csel w0, wzr, w8, eq')],
    'smaller task priority numbers are higher (mutex ceiling E_ILUSE)':[
        (0x40180108,'ldrb w11, [x11, #0x19]'),(0x4018010c,'cmp w10, w11'),(0x40180110,'b.ls #0x40180130'),
        (0x40180114,'mov w0, #-0x1c')],
}


@unittest.skipIf(capstone is None,'Capstone is required for native contract checks')
class PhotoLutNativeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        supplied=os.environ.get('Z8_PHOTO_LUT_ROM')
        if not supplied:
            raise unittest.SkipTest('Set Z8_PHOTO_LUT_ROM to the original C3.11 z8-main-payload.bi')
        path=Path(supplied)
        if not path.is_file():
            raise unittest.SkipTest(f'Z8_PHOTO_LUT_ROM is unavailable: {path}')
        cls.rom=path.read_bytes()
        observed=hashlib.sha256(cls.rom).hexdigest()
        if observed!=ROM_SHA256:
            raise AssertionError(f'C3.11 payload SHA256 differs: {observed}')
        cls.md=capstone.Cs(capstone.CS_ARCH_ARM64,capstone.CS_MODE_ARM)

    def instruction(self,address):
        offset=address-BASE
        decoded=list(self.md.disasm(self.rom[offset:offset+4],address))
        self.assertEqual(len(decoded),1,hex(address))
        return f'{decoded[0].mnemonic} {decoded[0].op_str}'

    def test_worker_kernel_and_pool_contracts(self):
        for claim,rows in CONTRACTS.items():
            for address,expected in rows:
                with self.subTest(claim=claim,address=hex(address)):
                    self.assertEqual(self.instruction(address),expected)


if __name__=='__main__':
    unittest.main()
