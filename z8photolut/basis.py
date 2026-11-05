"""Input bases a photo LUT look declares, and the cube text that hints at one.

`nikon-sdr` looks act on the photo the camera has already rendered (8-bit
full-range Y'CbCr after the active Picture Control) and are applied by the
CPU as a full 3D LUT. Every other basis is a documented camera Log encoding
with its gamut; such looks are compiled into the native ISP Log still develop
plus a near-identity residual grid (z8photolut/fit.py). Encodings and gamuts
use the names of colour-science (`colour.LOG_ENCODINGS`,
`colour.RGB_COLOURSPACES`).

`key` is stored in host source records and written to XMP InputBasis; `code`
is the runtime enum in the camera bundle (firmware lut.h Z8_BASIS_*). Both
are part of a look's identity and must never be renumbered.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Basis:
    key: str
    code: int
    label: str
    encoding: str = ''
    gamut: str = ''
    words: str = ''         # regular expression of text naming this encoding family

    @property
    def log(self):
        return bool(self.encoding)


NIKON_SDR = Basis('nikon-sdr', 0, "Nikon-rendered SDR (JPEG Y'CbCr after Picture Control)")
BASES = (
    NIKON_SDR,
    Basis('n-log/bt2020', 1, 'N-Log / ITU-R BT.2020', 'N-Log', 'ITU-R BT.2020', r'n-?log'),
    Basis('s-log3/s-gamut3.cine', 2, 'S-Log3 / S-Gamut3.Cine', 'S-Log3', 'S-Gamut3.Cine', r's-?log-?3|s-?gamut-?3(?:\.?cine)?'),
    Basis('log3g10/redwidegamutrgb', 3, 'Log3G10 / REDWideGamutRGB', 'Log3G10', 'REDWideGamutRGB',
          r'log3g1[02]|red-?wide-?gamut(?:-?rgb)?|rwg'),
    Basis('f-log2/f-gamut-c', 4, 'F-Log2 / F-Gamut C', 'F-Log2', 'F-Gamut C', r'f-?log-?2c?|f-?gamut-?c?'),
    Basis('arri-logc3/awg3', 5, 'ARRI LogC3 / ARRI Wide Gamut 3', 'ARRI LogC3', 'ARRI Wide Gamut 3',
          r'log-?c-?3?|awg-?3?|alexa-?wide-?gamut'),
    Basis('arri-logc4/awg4', 6, 'ARRI LogC4 / ARRI Wide Gamut 4', 'ARRI LogC4', 'ARRI Wide Gamut 4', r'log-?c-?4|awg-?4'),
    Basis('v-log/v-gamut', 7, 'V-Log / V-Gamut', 'V-Log', 'V-Gamut', r'v-?log|v-?gamut'),
)
LOG_BASES = tuple(b for b in BASES if b.log)
BY_KEY = {b.key: b for b in BASES}
BY_CODE = {b.code: b for b in BASES}

# Log, HDR and scene-referred encodings this camera cannot take as a declared basis.
OTHER_ENCODINGS = r'[cdl]-?log[0-9]?|s-?log-?[12]?|f-?log|hlg|pq|st[ ._-]?2084|(?:rec|bt|itu)[ ._-]?2100|acescc|acescct|cineon'
# Any wording that suggests a cube expects something other than Nikon-rendered SDR.
FOREIGN = re.compile(r'(?<![a-z0-9])(?:' + '|'.join([*(b.words for b in LOG_BASES), OTHER_ENCODINGS,
                                                       r'(?:rec|bt|itu)[ ._-]?2020']) + r')(?![a-z0-9])', re.IGNORECASE)
_FAMILY = [(b, re.compile(r'(?<![a-z0-9])(?:' + b.words + r')(?![a-z0-9])', re.IGNORECASE)) for b in LOG_BASES]
_OTHER = re.compile(r'(?<![a-z0-9])(?:' + OTHER_ENCODINGS + r')(?![a-z0-9])', re.IGNORECASE)


def _token(text):
    return re.sub(r'[^a-z0-9.]+', '', text.casefold())


def parse(text):
    """A basis from its key, its encoding name, or 'encoding/gamut' in colour-science spelling."""
    wanted = _token(text)
    for b in BASES:
        names = {b.key, b.encoding, f'{b.encoding}/{b.gamut}'} if b.log else {b.key, 'sdr', 'nikon-rendered-sdr'}
        if wanted in {_token(n) for n in names if n}:
            return b
    raise ValueError(f'Unknown input basis {text!r}; use one of: ' + ', '.join(b.key for b in BASES))


def families(text):
    """Supported Log bases whose encoding or gamut the text names, and unsupported encodings it names."""
    found = [b for b, pattern in _FAMILY if pattern.search(text)]
    # 'LogC' alone means LogC3; a text naming LogC4 is not also a LogC3 hint.
    if BY_KEY['arri-logc4/awg4'] in found and BY_KEY['arri-logc3/awg3'] in found and not re.search(
            r'(?<![a-z0-9])(?:log-?c-?3|awg-?3)(?![a-z0-9])', text, re.IGNORECASE):
        found.remove(BY_KEY['arri-logc3/awg3'])
    other = [m.group(0) for m in _OTHER.finditer(text)]
    return found, other
