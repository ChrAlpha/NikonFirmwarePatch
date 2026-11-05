"""Compile a Log-referred .cube into Z8 still-develop ISP tables plus a near-identity residual grid.

Needs numpy, scipy and colour-science (requirements-photo-lut-log.txt); only Log
looks import this module. Everything else in the toolkit stays stdlib-only.

ISP model (upstream docs/data/log-controls.json `model` and
tools/replay_log_controls.py, which reproduce stock N-Log file codes):

    display RGB = Y2r . Cpxs(M2 . curve(M1 . x)) / 1023

x is the M1 input in curve units, `curve` a monotone curve on the 229 native
nodes giving 10-bit codes, M2 maps codes to the internal Y'UV where the 32-node
hue ring Cpxs acts, and Y2r is the BT.601 full-range Y'CbCr -> RGB matrix, so
the result is the RGB the photo LUT kernel recovers from the 8-bit JPEG input.

Target: T(s) = LUT(encode_basis(s)) for scene-linear s in the basis gamut,
where 18 % grey (s = 0.18) sits at `grey` curve units (default 400: where the
stock N-Log curve encodes 0.18, 400.16, and upstream S-Log3's 0.00045 per unit).
The fit works in basis coordinates x_b = s / 0.18 * grey.

Camera to basis: upstream calibrated S-Log3 on this camera as
m1_body_this_camera = round(m1_base x B) / 1024 (M1 input -> S-Gamut3.Cine
curve units), where B is the camera's composed native matrix. Hence
x_b = M(S-Gamut3.Cine -> basis) . m1_body . x and the firmware M1 *base*, which
the native Log branch composes with B and gain exactly as for upstream S-Log3, is

    base = M1_fit . M(S-Gamut3.Cine -> basis) . m1_base        (Q10)

i.e. target x B^-1 without needing B on the host or in the camera; other bodies
get their own B from the native composition, as upstream Log video does.

Compiler: bounded monotone curve (40 knots), M1/M2 condition bounds, Cpxs node
determinant floor and inter-node smoothness for an injective map; the integer
firmware tables are quantised first, then the residual grid is built by damped
Gauss-Newton inversion of the quantised model at every grid node:
R(y) = y + T(x*) - P(x*), x* = argmin |P(x) - y|, so R = T o P^-1 where P is
invertible and continues near identity elsewhere; a residual that does not
beat the identity grid on the training samples is replaced by it (the camera
then skips the CPU pass). The report measures, on held-out samples and both
through the camera's 8-bit Y'CbCr JPEG path, the quantised ISP alone and the
final result with the residual applied by the firmware's integer kernel.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import warnings

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial import cKDTree

with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    import colour

from . import library as lib

ROOT = Path(__file__).resolve().parents[1]
COMPILER = 'z8photolut.fit/1'
MODEL = json.loads((ROOT/'docs/data/log-controls.json').read_text())['model']
GRID = np.array(MODEL['grid'], float)                  # 229 curve nodes in M1-output units
Y2R = np.array(MODEL['y2r'])
PHI = MODEL['phi_deg']
SLOT_POSITION = np.array(MODEL['ring_position_of_slot'])   # ring position of each stored slot
SLOG3 = json.loads((ROOT/'z8log/data.json').read_text())['types']['1']
M1_BASE_SLOG3 = np.array(SLOG3['m1_base'], float).reshape(3, 3)/1024
M1_BODY_SLOG3 = np.array(SLOG3['m1_body_this_camera'], float).reshape(3, 3)/1024
GREY = 400.0
KNOTS = np.unique(np.round(np.linspace(0, 228, 40)).astype(int))
M2_FROM_RGB = np.linalg.inv(Y2R)
DET_FLOOR, CONDITION_LIMIT = 0.35, 40.0
DEFAULTS = dict(grey=GREY, smoothness=3.0, samples=12000, iterations=80, seed=1)
REPORT_SAMPLES, REPORT_SEED = 15000, 2


@dataclass(frozen=True)
class Compiled:
    isp: bytes
    samples: bytes
    fit: dict


# ---- colour helpers -------------------------------------------------------
_XYZ = np.array(colour.RGB_COLOURSPACES['ITU-R BT.709'].matrix_RGB_to_XYZ)
_WHITE = _XYZ.sum(1)


def to_lab(display):
    """Display RGB (BT.709 primaries, 2.4 gamma, D65) to CIE Lab."""
    xyz = (np.clip(display, 0, 1)**2.4) @ _XYZ.T / _WHITE
    f = np.where(xyz > (6/29)**3, np.cbrt(xyz), xyz/(3*(6/29)**2)+4/29)
    return np.stack([116*f[:, 1]-16, 500*(f[:, 0]-f[:, 1]), 200*(f[:, 1]-f[:, 2])], 1)


def delta_e(a, b):
    return colour.delta_E(to_lab(a), to_lab(b), method='CIE 2000')


def camera_to_basis(basis):
    """M1 input (curve units) -> basis-gamut linear (curve units), from upstream's S-Log3 calibration."""
    cine = colour.matrix_RGB_to_RGB(colour.RGB_COLOURSPACES['S-Gamut3.Cine'], colour.RGB_COLOURSPACES[basis.gamut])
    return np.asarray(cine) @ M1_BODY_SLOG3


def base_from_fit(m1, basis):
    """Firmware M1 base for a basis-space M1 (see module docstring)."""
    cine = colour.matrix_RGB_to_RGB(colour.RGB_COLOURSPACES['S-Gamut3.Cine'], colour.RGB_COLOURSPACES[basis.gamut])
    return m1 @ np.asarray(cine) @ M1_BASE_SLOG3


def fit_from_base(base, basis):
    cine = colour.matrix_RGB_to_RGB(colour.RGB_COLOURSPACES['S-Gamut3.Cine'], colour.RGB_COLOURSPACES[basis.gamut])
    return base @ np.linalg.inv(M1_BASE_SLOG3) @ np.linalg.inv(np.asarray(cine))


class Target:
    """The look's display output for basis-space inputs x_b (curve units, grey at `grey`)."""

    def __init__(self, cube, basis, grey):
        n = cube.size
        self.table = np.array(cube.values, float).reshape(n, n, n, 3)        # [b][g][r]
        self.lo, self.hi = np.array(cube.minimum), np.array(cube.maximum)
        self.encoding, self.grey = basis.encoding, grey

    def __call__(self, x):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            encoded = colour.log_encoding(np.maximum(x*0.18/self.grey, 0), self.encoding)
        return np.clip(trilinear(self.table, (encoded-self.lo)/(self.hi-self.lo)), 0, 1)


def trilinear(table, rgb):
    n = table.shape[0]
    p = np.clip(rgb, 0, 1)*(n-1)
    i = np.minimum(np.floor(p).astype(int), n-2)
    f = p-i
    out = np.zeros_like(p)
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = (f[:, 0] if dr else 1-f[:, 0])*(f[:, 1] if dg else 1-f[:, 1])*(f[:, 2] if db else 1-f[:, 2])
                out += w[:, None]*table[i[:, 2]+db, i[:, 1]+dg, i[:, 0]+dr]
    return out


def scene_samples(n, seed, basis, grey):
    """Scene colours in basis curve units: log-uniform exposure over -7..+3.5 stops around grey,
    natural saturation, hues inside BT.2020."""
    rng = np.random.default_rng(seed)
    lum = 0.18*2.0**rng.uniform(-7, 3.5, n)
    hue = rng.uniform(0, 2*np.pi, n)
    sat = rng.beta(1.3, 3.0, n)
    lab = np.stack([np.full(n, 60.0), 90*sat*np.cos(hue), 90*sat*np.sin(hue)], 1)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        rgb2020 = colour.XYZ_to_RGB(colour.Lab_to_XYZ(lab), 'ITU-R BT.2020', apply_cctf_encoding=False)
        ok = (rgb2020 > 0).all(1)
        rgb = colour.RGB_to_RGB(rgb2020[ok], 'ITU-R BT.2020', basis.gamut)
        space = colour.RGB_COLOURSPACES[basis.gamut]
        y = colour.RGB_luminance(rgb, space.primaries, space.whitepoint)
    return rgb/y[:, None]*lum[ok][:, None]/0.18*grey


# ---- ISP model ------------------------------------------------------------
def ring(values):
    """Cpxs ring position and fraction of each internal Y'UV value."""
    u, v = values[:, 1], values[:, 2]
    angle = np.arctan2(v, u) % (2*np.pi)
    quadrant = np.floor(angle/(np.pi/2))
    sector = angle-quadrant*np.pi/2
    diamond = 90*(quadrant+np.sin(sector)/(np.cos(sector)+np.sin(sector)))
    position = ((diamond-PHI)/11.25) % 32
    index = np.floor(position).astype(int) % 32
    return index, position-np.floor(position)


def cpxs(values, rows):
    """rows: 32x6 Pa..Pf by ring position, unit gain = 1."""
    index, frac = ring(values)
    a, b, c, d, e, f = ((1-frac)[:, None]*rows[index]+frac[:, None]*rows[(index+1) % 32]).T
    y, u, v = values.T
    return np.column_stack([y+e*u+f*v, a*u+b*v, c*u+d*v])


def curve_at(lin, curve):
    return np.interp(lin.ravel(), GRID, curve).reshape(lin.shape)


def model(tables, x):
    m1, curve, m2, rows = tables
    code = curve_at(np.clip(x @ m1.T, 0, GRID[-1]), curve)
    return cpxs(code @ m2.T, rows) @ Y2R.T/1023.0


def curve_knots(params):
    """Bounded monotone knots: start = 1023 sigmoid(a); positive increments with a unit tail keep the top < 1023."""
    start = 1023/(1+np.exp(-params[0]))
    inc = np.exp(np.clip(params[1:], -30, 30))
    return np.concatenate([[start], start+(1023-start)*np.cumsum(inc)/(inc.sum()+1.0)])


def curve_params(code):
    span = 1023-code[0]
    d = np.maximum(np.diff(code), 1e-4)
    total = d.sum()/(span-d.sum())
    return np.concatenate([[np.log(code[0]/span)], np.log(d*(total+1)/span)])


DENSE = 9+len(KNOTS)+9          # M1, curve and M2 parameters; the 192 Cpxs values follow


def unpack(theta):
    k = len(KNOTS)
    m1 = theta[:9].reshape(3, 3)
    curve = np.interp(np.arange(229), KNOTS, curve_knots(theta[9:9+k]))
    m2 = theta[9+k:DENSE].reshape(3, 3)
    rows = theta[DENSE:].reshape(32, 6)
    return m1, curve, m2, rows


def initial(target):
    """Start from the basis transfer: curve(lin) = 1023 encode(lin / grey * 0.18), identity M1 and Cpxs."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        code = 1023*colour.log_encoding(GRID[KNOTS]*0.18/target.grey, target.encoding)
    code = np.maximum.accumulate(np.clip(code, 1.0, 1000.0)+np.arange(len(KNOTS))*1e-3)
    rows = np.tile([1.0, 0, 0, 1.0, 0, 0], (32, 1))
    return np.concatenate([np.eye(3).ravel(), curve_params(code), M2_FROM_RGB.ravel(), rows.ravel()])


def condition(m):
    s = np.linalg.svd(m, compute_uv=False)
    return s[0]/max(s[-1], 1e-12)


def penalties(theta, smoothness):
    m1, _, m2, rows = unpack(theta)
    det = rows[:, 0]*rows[:, 3]-rows[:, 1]*rows[:, 2]
    conditioning = [max(0.0, np.log(condition(m)/CONDITION_LIMIT))*20 for m in (m1, m2)]
    # Firmware ranges: M2 s16/512 (|m| < 64), Cpxs s16 Q8 (|p| < 128); keep well inside.
    ranges = np.concatenate([np.maximum(0, np.abs(m2.ravel())-48), np.maximum(0, np.abs(rows.ravel())-96)])
    return np.concatenate([np.maximum(0, DET_FLOOR-det)*50, (rows-np.roll(rows, -1, axis=0)).ravel()*smoothness,
                           conditioning, ranges*10])


def fit_tables(target, x, smoothness, iterations):
    """Least squares in Lab (dE76/10) plus constraints. Dense parameters use finite differences;
    the model is linear in the Cpxs rows given the hue ring position, so those columns are analytic."""
    lab_t = to_lab(target(x))
    n = len(x)

    def residuals(theta):
        return np.concatenate([(to_lab(model(unpack(theta), x))-lab_t).ravel()/10, penalties(theta, smoothness)])

    def jacobian(theta):
        base = residuals(theta)
        jac = np.zeros((len(base), len(theta)))
        for j in range(DENSE):
            step = 1e-6*max(1.0, abs(theta[j]))
            probe = theta.copy()
            probe[j] += step
            jac[:, j] = (residuals(probe)-base)/step
        m1, curve, m2, rows = unpack(theta)
        values = curve_at(np.clip(x @ m1.T, 0, GRID[-1]), curve) @ m2.T
        rgb = cpxs(values, rows) @ Y2R.T/1023.0
        lab0 = to_lab(rgb)
        dlab = np.stack([(to_lab(rgb+np.eye(3)[k]*1e-5)-lab0)/1e-5 for k in range(3)], 2)/10     # n x 3 x 3
        index, frac = ring(values)
        u, v = values[:, 1], values[:, 2]
        # d yuv / d (a,b,c,d,e,f) = (0,u,0) (0,v,0) (0,0,u) (0,0,v) (u,0,0) (v,0,0)
        component, variable = (1, 1, 2, 2, 0, 0), (u, v, u, v, u, v)
        for k in range(6):
            drgb = Y2R[:, component[k]][None, :]*variable[k][:, None]/1023.0          # n x 3
            dl = np.einsum('nij,nj->ni', dlab, drgb)                                  # n x 3
            for slot, weight in ((index, 1-frac), ((index+1) % 32, frac)):
                columns = DENSE+slot*6+k
                for channel in range(3):
                    jac[np.arange(n)*3+channel, columns] += dl[:, channel]*weight
        # Analytic Cpxs penalties: determinant floor, smoothness, ranges.
        at = 3*n
        det = rows[:, 0]*rows[:, 3]-rows[:, 1]*rows[:, 2]
        active = DET_FLOOR-det > 0
        for r in range(32):
            if active[r]:
                a, b, c, d = rows[r, :4]
                jac[at+r, DENSE+r*6:DENSE+r*6+4] = np.array([-d, c, b, -a])*50
        at += 32
        for r in range(32):
            for k in range(6):
                jac[at+r*6+k, DENSE+r*6+k] = smoothness
                jac[at+r*6+k, DENSE+((r+1) % 32)*6+k] = -smoothness
        at += 192+2+9
        flat = rows.ravel()
        jac[at+np.arange(192), DENSE+np.arange(192)] = np.where(np.abs(flat) > 96, np.sign(flat)*10, 0)
        return jac

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        result = least_squares(residuals, initial(target), jac=jacobian, method='trf', x_scale='jac',
                               max_nfev=iterations)
    return unpack(result.x)


# ---- firmware integers ----------------------------------------------------
def quantise(tables, basis):
    """Integer firmware tables (library.pack_isp) and the exact float model they implement in basis space."""
    m1, curve, m2, rows = tables
    base = np.round(1024*base_from_fit(m1, basis)).astype(int)
    rgb = np.maximum.accumulate(np.clip(np.round(32*curve), 0, lib.CURVE_LIMIT).astype(int))
    m2q = np.round(512*m2).astype(int)
    rowsq = np.round(256*rows).astype(int)
    for name, values in (('M1 base', base), ('M2', m2q), ('Cpxs', rowsq)):
        if np.abs(values).max() > 32767:
            raise ValueError(f'{name} does not fit the firmware s16 format')
    isp = lib.pack_isp(dict(rgb=[*rgb, *rgb, *rgb], nlg=(rgb+4) >> 3, m2=m2q.ravel(), m1=base.ravel(),
                            cpxs=rowsq[SLOT_POSITION].ravel()))
    return isp, tables_from_isp(isp, basis)


def tables_from_isp(isp, basis):
    t = lib.unpack_isp(isp)
    rows = np.zeros((32, 6))
    rows[SLOT_POSITION] = np.array(t['cpxs'], float).reshape(32, 6)
    green = np.array(t['rgb'][229:458], float)/32
    return (fit_from_base(np.array(t['m1'], float).reshape(3, 3)/1024, basis), green,
            np.array(t['m2'], float).reshape(3, 3)/512, rows/256)


# ---- residual -------------------------------------------------------------
def node_rgb(n):
    """Grid nodes in sample order (red outer, blue fastest), RGB 0..1."""
    g = np.linspace(0, 1, n)
    r, gg, b = np.meshgrid(g, g, g, indexing='ij')
    return np.stack([r.ravel(), gg.ravel(), b.ravel()], 1)


def invert(tables, ys, seed_x, iterations=30):
    """x* = argmin |P(x) - y| per y by damped Gauss-Newton in log2(x), seeded from the nearest sample."""
    seed_y = model(tables, seed_x)
    lx = np.log2(np.maximum(seed_x[cKDTree(seed_y).query(ys)[1]], 1e-3))
    eye = 1e-6*np.eye(3)
    for _ in range(iterations):
        f = model(tables, 2.0**lx)-ys
        jac = np.empty((len(ys), 3, 3))
        for k in range(3):
            probe = lx.copy()
            probe[:, k] += 1e-3
            jac[:, :, k] = (model(tables, 2.0**probe)-ys-f)/1e-3
        jt = np.transpose(jac, (0, 2, 1))
        step = np.linalg.solve(jt @ jac+eye, -(jt @ f[:, :, None]))[:, :, 0]
        lx = np.clip(lx+np.clip(step, -1.5, 1.5), -10, 17)
    return 2.0**lx


def residual_grid(tables, target, grid, seeds):
    ys = node_rgb(grid)
    xs = invert(tables, ys, seeds)
    values = np.clip(ys+target(xs)-model(tables, xs), 0, 1)
    samples = np.round(values*65535).astype('<u2')
    return samples.tobytes(), float((np.abs(model(tables, xs)-ys).max(1) < 0.5/255).mean())


# ---- the camera's JPEG path (firmware lut.c integer kernel) -------------------
def jpeg_input(display):
    """8-bit full-range BT.601 Y'CbCr the ISP hands the JPEG path, from model display RGB."""
    rgb = np.clip(display, 0, 1)*255
    y = rgb @ np.array([0.299, 0.587, 0.114])
    cb = (rgb[:, 2]-y)/1.772+128
    cr = (rgb[:, 0]-y)/1.402+128
    return np.clip(np.round(np.stack([y, cb, cr], 1)), 0, 255).astype(np.int64)


def kernel(samples, grid, ycc):
    """Exactly lut.c transform(): integer Y'CbCr -> RGB, tetrahedral uint16 lookup, RGB -> Y'CbCr."""
    table = np.frombuffer(samples, '<u2').reshape(grid, grid, grid, 3).astype(np.int64)     # [r][g][b]
    y, cb, cr = ycc[:, 0], ycc[:, 1]-128, ycc[:, 2]-128
    rgb = np.clip(np.stack([y+((359*cr+128) >> 8), y-((88*cb+183*cr+128) >> 8), y+((454*cb+128) >> 8)], 1), 0, 255)
    scaled = rgb*(grid-1)
    low, f = scaled//255, scaled % 255
    top = low == grid-1
    low[top] -= 1
    f[top] = 255
    order = np.argsort(-f, axis=1, kind='stable')
    fs = np.take_along_axis(f, order, 1)
    weights = np.stack([255-fs[:, 0], fs[:, 0]-fs[:, 1], fs[:, 1]-fs[:, 2], fs[:, 2]], 1)
    corner = low.copy()
    total = np.zeros((len(ycc), 3), np.int64)
    rows = np.arange(len(ycc))
    for step in range(4):
        if step:
            corner[rows, order[:, step-1]] += 1
        total += weights[:, step:step+1]*table[corner[:, 0], corner[:, 1], corner[:, 2]]
    mapped = (total+32767)//65535
    r, g, b = mapped.T
    return np.stack([(77*r+150*g+29*b+128) >> 8, np.clip(((-43*r-85*g+128*b+128) >> 8)+128, 0, 255),
                     np.clip(((128*r-107*g-21*b+128) >> 8)+128, 0, 255)], 1)


def jpeg_path(samples, grid, ycc):
    """What the camera encodes: an identity grid is skipped entirely, any other grid runs the kernel."""
    return ycc if samples == lib.identity_samples(grid) else kernel(samples, grid, ycc)


def jpeg_display(ycc):
    y, cb, cr = ycc[:, 0].astype(float), ycc[:, 1]-128.0, ycc[:, 2]-128.0
    return np.clip(np.stack([y+1.402*cr, y-0.344136*cb-0.714136*cr, y+1.772*cb], 1)/255, 0, 1)


def slope(samples, grid, ys):
    """Largest residual step per 8-bit input step in the cells the ISP output populates."""
    table = np.frombuffer(samples, '<u2').reshape(grid, grid, grid, 3).astype(float)/65535
    cell = np.unique(np.minimum(np.floor(np.clip(ys, 0, 1)*(grid-1)).astype(int), grid-2), axis=0)
    r, g, b = cell.T
    steps = [np.abs(table[r+1, g, b]-table[r, g, b]), np.abs(table[r, g+1, b]-table[r, g, b]),
             np.abs(table[r, g, b+1]-table[r, g, b])]
    return np.max(np.stack(steps), axis=(0, 2))*(grid-1)


def measure(tables, isp, samples, grid, target, basis, grey):
    held = scene_samples(REPORT_SAMPLES, REPORT_SEED, basis, grey)
    wanted = target(held)
    isp_out = model(tables, held)
    final = jpeg_display(jpeg_path(samples, grid, jpeg_input(isp_out)))
    pct = lambda d, ks: [round(float(np.percentile(d, k)), 3) for k in ks]     # noqa: E731
    t = lib.unpack_isp(isp)
    rows = np.array(t['cpxs'], float).reshape(32, 6)/256
    m1_target = np.round(1024*np.array(t['m1'], float).reshape(3, 3)/1024 @ np.linalg.inv(M1_BASE_SLOG3)
                         @ M1_BODY_SLOG3).astype(int)
    return dict(isp_de00=pct(delta_e(jpeg_display(jpeg_input(isp_out)), wanted), (50, 95, 99)),
                final_de00=pct(delta_e(final, wanted), (50, 95, 99)),
                slope=pct(slope(samples, grid, isp_out), (50, 99)),
                cpxs_min_det=round(float((rows[:, 0]*rows[:, 3]-rows[:, 1]*rows[:, 2]).min()), 3),
                m1_condition=round(float(condition(np.array(t['m1'], float).reshape(3, 3))), 3),
                m2_condition=round(float(condition(np.array(t['m2'], float).reshape(3, 3))), 3),
                m1_target_q10=[int(v) for v in m1_target.ravel()])


def compile_look(cube, basis, *, grid=33, grey=GREY, smoothness=3.0, samples=12000, iterations=80, seed=1):
    """Fit, quantise to firmware integers, then build the residual from the quantised model."""
    if not basis.log:
        raise ValueError('Only Log bases are compiled into ISP tables')
    if not 25 <= grey <= 6400:
        raise ValueError('Grey point must be 25..6400 curve units (default 400)')
    if not 0 <= smoothness <= 100 or not 500 <= samples <= 200000 or not 1 <= iterations <= 1000:
        raise ValueError('Fit options out of range')
    target = Target(cube, basis, float(grey))
    x = scene_samples(samples, seed, basis, target.grey)
    tables = fit_tables(target, x, float(smoothness), iterations)
    isp, quantised = quantise(tables, basis)
    seeds = scene_samples(max(4*samples, 40000), seed+6, basis, target.grey)
    residual, reachable = residual_grid(quantised, target, grid, seeds)
    # Keep the residual only where it helps: on the training samples through the camera's JPEG path, a
    # coarse grid near the edge of the ISP output range can be worse than the ISP fit alone.
    produced, wanted = jpeg_input(model(quantised, x)), target(x)
    identity = lib.identity_samples(grid)
    if delta_e(jpeg_display(jpeg_path(identity, grid, produced)), wanted).mean() <= \
            delta_e(jpeg_display(jpeg_path(residual, grid, produced)), wanted).mean():
        residual = identity
    report = measure(quantised, isp, residual, grid, target, basis, target.grey)
    report['reachable'] = round(reachable, 4)
    fit = dict(compiler=COMPILER, grey=float(grey), smoothness=float(smoothness), samples=int(samples),
               iterations=int(iterations), seed=int(seed), report=report)
    return Compiled(isp, residual, fit)


def verify_look(look):
    """Re-measure a stored Log look from its tables and source: the report must hold within 0.01."""
    from .basis import BY_KEY
    from .cube import parse
    basis = BY_KEY[look.basis]
    fit = look.fit
    target = Target(parse(look.source), basis, fit['grey'])
    quantised = tables_from_isp(look.isp, basis)
    report = measure(quantised, look.isp, look.samples, look.grid, target, basis, fit['grey'])
    stored = fit['report']
    for key in ('isp_de00', 'final_de00', 'slope'):
        if any(abs(a-b) > 0.01 for a, b in zip(report[key], stored[key])):
            raise ValueError(f'Look {look.name!r}: {key} re-measured as {report[key]}, stored {stored[key]}')
    if report['m1_target_q10'] != stored['m1_target_q10']:
        raise ValueError(f'Look {look.name!r}: stored M1 does not match its report')
    return report


def describe(fit):
    """Human-readable fit report lines."""
    r = fit['report']
    p = lambda v: '/'.join(f'{x:.2f}' for x in v)      # noqa: E731
    return [f'fit: grey {fit["grey"]:g} curve units, smoothness {fit["smoothness"]:g}, {fit["samples"]} samples',
            f'  dE00 p50/p95/p99: ISP only {p(r["isp_de00"])}; ISP + residual (8-bit JPEG path) {p(r["final_de00"])}',
            f'  residual slope p50/p99 {p(r["slope"])} codes per code (1 = identity grid, skipped by the camera); '
            f'reachable nodes '
            f'{100*r["reachable"]:.1f} %',
            f'  min Cpxs determinant {r["cpxs_min_det"]:.2f}; M1 condition {r["m1_condition"]:.1f}, '
            f'M2 condition {r["m2_condition"]:.1f}']

