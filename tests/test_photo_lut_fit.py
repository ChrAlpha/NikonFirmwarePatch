"""Log look compiler (z8photolut/fit.py): firmware formats, quantise-then-invert, kernel parity, reports.

Needs numpy, scipy and colour-science (requirements-photo-lut-log.txt); skipped without them.
Set Z8_PHOTO_LUT_SLOW=1 to also compile a real cube from /workspaces/LumaForge/lumaforge-profiles
(not part of the repository; skipped when absent) at the production settings.
"""
import ctypes as C
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

try:
    import numpy as np
    import colour  # noqa: F401
    from z8photolut import fit
except ImportError:
    fit = None

from z8photolut import library as lib
from z8photolut.basis import BY_KEY
from z8photolut.cube import parse

ROOT = Path(__file__).resolve().parents[1]
SLOW = os.environ.get('Z8_PHOTO_LUT_SLOW') == '1'
PROVIA = Path('/workspaces/LumaForge/lumaforge-profiles/profiles/lut.lumaforge.flog2c-to-provia-65grid-v-1-00.v1/'
              'assets/flog2c-to-provia-65grid-v-1-00.cube')


def film_cube(encoding, size=17):
    """A synthetic Log -> display look: decode, warm/saturated grade, filmic tone curve, BT.709 2.4 encode, then a
    luminance-dependent split tone (teal shadows, warm highlights) that per-hue ISP stages cannot represent."""
    import colour
    g = np.linspace(0, 1, size)
    b, gg, r = np.meshgrid(g, g, g, indexing='ij')
    code = np.stack([r.ravel(), gg.ravel(), b.ravel()], 1)
    linear = np.maximum(colour.log_decoding(code, encoding), 0)
    grey = linear.mean(1, keepdims=True)
    graded = np.maximum(grey+1.25*(linear-grey), 0)*[1.06, 1.0, 0.92]
    display = np.clip((graded/(graded+0.6))*1.6, 0, 1)**(1/2.4)
    display = np.clip(display+(display.mean(1, keepdims=True)-0.45)*[0.16, 0.02, -0.16], 0, 1)
    rows = [f'TITLE "{encoding} film"', f'LUT_3D_SIZE {size}'] + [' '.join(f'{v:.6f}' for v in row) for row in display]
    return ('\n'.join(rows)+'\n').encode()


@unittest.skipIf(fit is None, 'numpy, scipy and colour-science are needed to compile Log looks')
class CompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.basis = BY_KEY['s-log3/s-gamut3.cine']
        cls.raw = film_cube('S-Log3')
        cls.options = dict(samples=2500, iterations=25)
        cls.look = lib.look_from_cube(cls.raw, source_name='slog3-film.cube', basis=cls.basis.key, grid=33,
                                      fit_options=cls.options)

    def test_tables_are_firmware_integers_with_identical_curves(self):
        tables = lib.unpack_isp(self.look.isp)
        rgb = tables['rgb']
        self.assertEqual(rgb[:229], rgb[229:458])
        self.assertEqual(rgb[:229], rgb[458:])
        self.assertLessEqual(max(rgb), lib.CURVE_LIMIT)
        self.assertEqual(tables['nlg'], tuple((v+4) >> 3 for v in rgb[229:458]))
        rows = np.array(tables['cpxs']).reshape(32, 6)/256
        det = rows[:, 0]*rows[:, 3]-rows[:, 1]*rows[:, 2]
        self.assertGreater(det.min(), 0.3)
        self.assertLess(fit.condition(np.array(tables['m2']).reshape(3, 3)), fit.CONDITION_LIMIT)

    def test_compiling_is_deterministic_and_the_report_holds(self):
        again = lib.look_from_cube(self.raw, source_name='slog3-film.cube', basis=self.basis.key, grid=33,
                                   fit_options=self.options)
        self.assertEqual((again.isp, again.samples, again.fit), (self.look.isp, self.look.samples, self.look.fit))
        self.assertEqual(fit.verify_look(self.look)['final_de00'], self.look.fit['report']['final_de00'])
        report = self.look.fit['report']
        self.assertLess(report['isp_de00'][0], 2.0)
        self.assertFalse(self.look.identity)
        self.assertLess(report['final_de00'][0], report['isp_de00'][0])
        self.assertLess(report['final_de00'][1], report['isp_de00'][1])
        self.assertLess(report['slope'][0], 1.5)
        self.assertLess(report['slope'][1], 8.0)
        library = lib.Library((self.look,))
        self.assertEqual(lib.decode(library.encode(), deep=True).looks[0].fit, self.look.fit)

    def test_residual_inverts_the_quantised_model(self):
        quantised = fit.tables_from_isp(self.look.isp, self.basis)
        target = fit.Target(parse(self.raw), self.basis, 400.0)
        nodes = fit.node_rgb(9)
        xs = fit.invert(quantised, nodes, fit.scene_samples(20000, 7, self.basis, 400.0))
        reached = np.abs(fit.model(quantised, xs)-nodes).max(1) < 0.5/255
        self.assertGreater(reached.mean(), 0.3)
        samples, _ = fit.residual_grid(quantised, target, 9, fit.scene_samples(20000, 7, self.basis, 400.0))
        grid = np.frombuffer(samples, '<u2').reshape(-1, 3)/65535
        # R(y) = y + T(x*) - P(x*): T o P^-1 where y is reached, a continuous near-identity elsewhere.
        np.testing.assert_allclose(grid, np.clip(nodes+target(xs)-fit.model(quantised, xs), 0, 1), atol=1/65535)
        np.testing.assert_allclose(grid[reached], np.clip(target(xs[reached]), 0, 1), atol=0.5/255+1/65535)

    def test_m1_base_composes_with_the_upstream_calibration(self):
        # For S-Log3 the basis is S-Gamut3.Cine: an identity basis-space M1 is upstream's own base, and the
        # camera-to-basis matrix is its calibrated product for this camera.
        np.testing.assert_allclose(fit.base_from_fit(np.eye(3), self.basis), fit.M1_BASE_SLOG3, atol=1e-12)
        np.testing.assert_allclose(fit.camera_to_basis(self.basis), fit.M1_BODY_SLOG3, atol=1e-12)
        logc = BY_KEY['arri-logc3/awg3']
        m = np.array([[0.9, 0.1, 0.0], [0.05, 1.1, -0.15], [0.0, -0.1, 1.1]])
        np.testing.assert_allclose(fit.fit_from_base(fit.base_from_fit(m, logc), logc), m, atol=1e-12)
        report = self.look.fit['report']
        base = np.array(lib.unpack_isp(self.look.isp)['m1']).reshape(3, 3)
        expected = np.round(base @ np.linalg.inv(fit.M1_BASE_SLOG3) @ fit.M1_BODY_SLOG3).astype(int)
        self.assertEqual(report['m1_target_q10'], [int(v) for v in expected.ravel()])

    def test_grey_point_moves_exposure(self):
        brighter = fit.Target(parse(self.raw), self.basis, 200.0)
        x = np.array([[400.0, 400.0, 400.0]])
        self.assertGreater(brighter(x)[0, 1], fit.Target(parse(self.raw), self.basis, 400.0)(x)[0, 1])
        with self.assertRaisesRegex(ValueError, 'Grey point'):
            fit.compile_look(parse(self.raw), self.basis, grey=10)

    def test_kernel_matches_the_firmware_c(self):
        with tempfile.TemporaryDirectory() as folder:
            so = Path(folder)/'lut.so'
            subprocess.run(['cc', '-std=c11', '-O2', '-shared', '-fPIC', str(ROOT/'z8photolut/src/lut.c'), '-o', str(so)],
                           check=True)
            native = C.CDLL(str(so))

            class Surface(C.Structure):
                _fields_ = [('y', C.c_void_p), ('uv', C.c_void_p), ('y_bytes', C.c_uint64), ('uv_bytes', C.c_uint64),
                            ('width', C.c_uint32), ('height', C.c_uint32), ('y_stride', C.c_uint32),
                            ('uv_stride', C.c_uint32), ('uv_divisor', C.c_uint32)]

            class Look(C.Structure):
                _fields_ = [('samples', C.c_void_p), ('grid', C.c_uint32), ('identity', C.c_uint32), ('strength', C.c_uint32)]
            table = C.create_string_buffer(self.look.samples)
            look = Look(C.cast(table, C.c_void_p), 33, 0, 256)
            ycc = np.random.default_rng(3).integers(0, 256, (300, 3))
            mine = fit.kernel(self.look.samples, 33, ycc)
            for value, expected in zip(ycc, mine):
                y = C.create_string_buffer(bytes([value[0]])*4)
                uv = C.create_string_buffer(bytes([value[1], value[2]]))
                surface = Surface(C.cast(y, C.c_void_p), C.cast(uv, C.c_void_p), 4, 2, 2, 2, 2, 2, 2)
                self.assertEqual(native.z8_lut_surface(C.byref(surface), C.byref(look)), 0)
                self.assertEqual([y.raw[0], uv.raw[0], uv.raw[1]], list(expected))

    def test_coarse_residual_that_hurts_becomes_identity(self):
        flat = lib.look_from_cube(film_cube('S-Log3', 5), source_name='slog3-film.cube', basis=self.basis.key, grid=17,
                                  fit_options=dict(samples=800, iterations=3))
        if flat.identity:
            self.assertEqual(flat.fit['report']['slope'], [1.0, 1.0])
        produced = fit.jpeg_input(np.array([[0.2, 0.4, 0.6]]))
        self.assertEqual(fit.jpeg_path(lib.identity_samples(17), 17, produced).tolist(), produced.tolist())


@unittest.skipUnless(fit is not None and SLOW and PROVIA.exists(), 'set Z8_PHOTO_LUT_SLOW=1 with the LumaForge cubes')
class RealCubeTests(unittest.TestCase):
    def test_provia_at_production_settings(self):
        look = lib.look_from_cube(PROVIA.read_bytes(), source_name=PROVIA.name, basis='f-log2/f-gamut-c', grid=33)
        report = look.fit['report']
        self.assertLess(report['isp_de00'][0], 1.3)
        self.assertLess(report['final_de00'][0], 0.6)
        self.assertLess(report['final_de00'][1], report['isp_de00'][1])
        self.assertLess(report['slope'][1], 2.5)
        raw = lib.Library((look,)).encode()
        self.assertEqual(struct.unpack_from('<2H', raw, 64+20), (1, 4))
        lib.decode(raw, deep=True)


if __name__ == '__main__':
    unittest.main()
