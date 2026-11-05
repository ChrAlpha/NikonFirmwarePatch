"""Shared owner of the five recording colour-chain sites, used by log-colour and photo-lut."""
import struct
from z8prores.package import require
from z8patch.image import BASE, branch

CODE, CONFIG, CONFIG_END = 0x40981000, 0x40981F00, 0x40981F60
LOG_TYPE = 0xA03D5C0DD
# (address, stock word, hook entry)
SITES = [(0x4111A508, '0800e0f2', 'colour_rgb_hook'), (0x4111A6D4, '2129029b', 'colour_nlg_hook'),
         (0x41116CB4, '0b250b9b', 'colour_m2_hook'), (0x41116010, '0e00e0f2', 'colour_m1_hook'),
         (0x4111A198, '75a24039', 'colour_cpxs_hook')]


def emit(img, code, entries, log_tables=None, provider=0):
    """Install the colour unit. log_tables: ten table addresses from z8log.patcher.colour_tables(), or
    None without log-colour. provider: photo-lut still-develop provider block address, or 0."""
    require(log_tables is not None or provider, 'Colour chain without a provider')
    require(log_tables is None or len(log_tables)==10, 'Log colour table shape')
    require(CODE+len(code)<=CONFIG, 'Colour code reaches its configuration')
    img.put(CODE-BASE, code, 'shared colour-chain code', expect_zero=True)
    config = struct.pack('<Q10QQ', LOG_TYPE if log_tables else 0, *(log_tables or [0]*10), provider)
    require(CONFIG+len(config)==CONFIG_END, 'Colour configuration shape')
    img.put(CONFIG-BASE, config, 'shared colour-chain providers: Log type tables and still-develop block', expect_zero=True)
    for at, word, entry in SITES:
        img.put(at-BASE, branch(at, entries[entry]), f'{at:08X}: {word} -> b {entry} (shared colour chain)', expect=bytes.fromhex(word))
    return dict(code=hex(CODE), config=hex(CONFIG), log_type=hex(LOG_TYPE) if log_tables else None,
                log_tables=[hex(t) for t in log_tables] if log_tables else None, provider=hex(provider) if provider else None,
                sites={hex(at): entry for at, _, entry in SITES})
