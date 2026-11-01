/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef Z8_PHOTO_LUT_METADATA_H
#define Z8_PHOTO_LUT_METADATA_H
#include <stdint.h>

#include "lut.h"

/* Project namespace for photo LUT provenance. Every property this helper
 * writes lives here; no Nikon, Adobe, Panasonic or other vendor schema is
 * touched, and nothing implies a host tool should re-apply the transform. */
#define Z8_META_NS "https://github.com/ChrAlpha/NikonFirmwarePatch/ns/photo-lut/1.0/"
#define Z8_META_PREFIX "photolut"
#define Z8_META_SCHEMA "1.0"
#define Z8_META_INPUT_BASIS "nikon-rendered-sdr"
#define Z8_META_RENDERING "baked-into-jpeg-pixels"

#define Z8_META_XMPMETA_NS "adobe:ns:meta/"
#define Z8_META_RDF_NS "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

#define Z8_META_FIRMWARE_BYTES 32u
#define Z8_META_MIN_PACKET 64u
#define Z8_META_MAX_PACKET 0x100000u
/* `</rdf:RDF>` plus optional whitespace plus `</x:xmpmeta>` is 22 bytes in
 * C3.11 output; refuse anything implausibly large rather than relocating it. */
#define Z8_META_MAX_CLOSING 64u
/* Window for the opening-envelope scan. C3.11's envelope is 154 bytes; a
 * prebuilt packet carrying x:xmptk and newlines still fits far inside this. */
#define Z8_META_MAX_ENVELOPE 1024u

typedef struct {
    char name[64];                      /* UTF-8, NUL-padded, 1..63 bytes */
    uint8_t id[32];                     /* transform SHA-256 */
    uint32_t grid, strength, identity;  /* strength 0..256, 256 = full */
    char firmware[Z8_META_FIRMWARE_BYTES]; /* optional, all-NUL to omit */
} Z8LutProvenance;

enum {
    Z8_META_APPENDED, Z8_META_BAD_ARGUMENT, Z8_META_BAD_RECORD,
    Z8_META_BAD_PACKET, Z8_META_NO_SPACE, Z8_META_DUPLICATE
};

/* Insert one rdf:Description carrying `record` immediately before the packet's
 * final `</rdf:RDF>`, consuming exactly as many trailing padding spaces as the
 * inserted bytes. The packet's byte count, its trailer offset and every byte
 * before the insertion point are preserved. Returns Z8_META_APPENDED and (when
 * `appended` is non-NULL) the inserted length; on any other result the packet
 * is byte-for-byte unchanged and `appended` is untouched.
 *
 * `packet`, `*record` and `*appended` must not overlap: `packet` is mutated
 * during emission, so an aliased record would stop being the immutable input
 * the emission assumes. `const` cannot express that, so it is checked here and
 * reported as Z8_META_BAD_ARGUMENT.
 *
 * Validation is structural, not a full XML parse: the opening envelope
 * (`<?xpacket begin=`, `x:xmpmeta` with xmlns:x, `rdf:RDF` with xmlns:rdf), the
 * closing tags, the trailer and the padding run are all checked, and comments
 * and quoted attribute values are skipped so a namespace cannot be faked
 * inside them. Well-formedness of the packet's INTERIOR -- the properties
 * between `<rdf:RDF ...>` and `</rdf:RDF>` -- is a precondition owned by the
 * native composer and the caller; this helper neither verifies nor repairs
 * it. */
unsigned z8_meta_append(char *packet, uint32_t capacity,
                        const Z8LutProvenance *record, uint32_t *appended);

#endif
