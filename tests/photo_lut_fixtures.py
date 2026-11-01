"""Shared photo LUT test fixtures: runtime bundles from literal cubes."""
from z8photolut.library import Library, look_from_cube

LEASE_BYTES = 256
JPEG_STORAGE = 8256*5504*2+1920*1080*2+640*480*2+192*120*2+0x3600


def runtime_bundle(cubes, grid=17, default=0):
    """Runtime bytes for (raw .cube bytes) in order, named by their TITLE."""
    looks = tuple(look_from_cube(raw, source_name=f'look{i}.cube', grid=grid) for i, raw in enumerate(cubes))
    return Library(looks, default).runtime()


def snapshot_bytes(grid):
    """Bytes a transform reservation adds for the latched private look grid."""
    return (grid**3*6+63)&~63
