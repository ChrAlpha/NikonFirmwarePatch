/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Bounded, length-preserving photo LUT provenance for an existing XMP packet.
 *
 * The native composer (C3.11 0x406e4f48, reached from the JPEG header builder
 * 0x406c31bc) always emits a packet of exactly the reserved size for the file
 * kind: real content, then a run of spaces, then `<?xpacket end="w"?>`. The
 * padding is dead weight, so provenance can be inserted into it without moving
 * a single byte of the APP1 segment, the Exif offsets or the entropy data.
 *
 * This file is pure: parameters and locals only, no globals with mutable
 * state, no libc, no heap, no atomics, no floating point. Every read and write
 * is bounded by the caller-supplied packet capacity, and every validation runs
 * before the first byte is written, so a refusal always leaves the packet
 * untouched. */
#include "metadata.h"

/* Pass a string literal as (pointer, length) without a runtime strlen. */
#define LIT(s) (s), (uint32_t)(sizeof(s) - 1u)
#define TAG_OPEN "<" Z8_META_PREFIX ":"
#define TAG_CLOSE "</" Z8_META_PREFIX ":"

/* A single emission routine serves both passes: with `out == 0` it only
 * measures, with `out != 0` it writes into a span hard-bounded by `limit`. */
typedef struct {
    char *out;
    uint32_t limit, len;
    unsigned overflow;
} Sink;

static void emit(Sink *s, const char *text, uint32_t bytes)
{
    if (s->overflow) return;
    if (s->out) {
        if (s->len > s->limit || bytes > s->limit - s->len) { s->overflow = 1; return; }
        for (uint32_t i = 0; i < bytes; i++) s->out[s->len + i] = text[i];
    }
    s->len += bytes;
}

/* XML 1.0 character data. `&` and `<` are mandatory, `>` avoids `]]>`, and the
 * quotes keep the text safe if a consumer ever lifts it into an attribute.
 * Non-ASCII UTF-8 passes through: z8_lut_name_valid has already excluded C0/C1
 * controls, surrogates and U+FFFE/U+FFFF, so every remaining byte is legal. */
static void emit_escaped(Sink *s, const char *text, uint32_t bytes)
{
    for (uint32_t i = 0; i < bytes; i++) {
        switch (text[i]) {
        case '&': emit(s, LIT("&amp;")); break;
        case '<': emit(s, LIT("&lt;")); break;
        case '>': emit(s, LIT("&gt;")); break;
        case '"': emit(s, LIT("&quot;")); break;
        case '\'': emit(s, LIT("&apos;")); break;
        default: emit(s, &text[i], 1); break;
        }
    }
}

static void emit_u32(Sink *s, uint32_t value)
{
    char digits[10];
    unsigned n = 0;
    do { digits[n++] = (char)('0' + value % 10u); value /= 10u; } while (value);
    while (n) emit(s, &digits[--n], 1);
}

static void emit_hex(Sink *s, const uint8_t *bytes, uint32_t count)
{
    static const char hex[] = "0123456789abcdef";
    for (uint32_t i = 0; i < count; i++) {
        char pair[2] = { hex[bytes[i] >> 4], hex[bytes[i] & 15u] };
        emit(s, pair, 2);
    }
}

static void emit_property(Sink *s, const char *tag, uint32_t tag_bytes,
                          const char *value, uint32_t value_bytes, unsigned escape)
{
    emit(s, LIT(TAG_OPEN));
    emit(s, tag, tag_bytes);
    emit(s, LIT(">"));
    if (escape) emit_escaped(s, value, value_bytes);
    else emit(s, value, value_bytes);
    emit(s, LIT(TAG_CLOSE));
    emit(s, tag, tag_bytes);
    emit(s, LIT(">\n"));
}

static void emit_number(Sink *s, const char *tag, uint32_t tag_bytes, uint32_t value)
{
    emit(s, LIT(TAG_OPEN));
    emit(s, tag, tag_bytes);
    emit(s, LIT(">"));
    emit_u32(s, value);
    emit(s, LIT(TAG_CLOSE));
    emit(s, tag, tag_bytes);
    emit(s, LIT(">\n"));
}

/* One self-contained rdf:Description in this project's namespace only. No
 * Nikon, Adobe, Panasonic, photoshop, tiff, exif or dc property is written,
 * and `Rendering` states that the pixels already carry the transform (baked
 * into the JPEG, or developed through fitted ISP tables plus a residual), so
 * no host tool should treat this as a recipe to re-apply. */
static void emit_record(Sink *s, const Z8LutProvenance *r,
                        uint32_t name_bytes, uint32_t firmware_bytes)
{
    const unsigned isp = r->rendering == Z8_RENDER_ISP;
    const unsigned noop = !isp && (r->identity || !r->strength);
    const char *basis = z8_lut_basis_key(r->basis);
    uint32_t basis_bytes = 0;
    while (basis[basis_bytes]) basis_bytes++;

    emit(s, LIT("<rdf:Description rdf:about=\"\" xmlns:"
               Z8_META_PREFIX "=\"" Z8_META_NS "\">\n"));
    if (noop) {
        emit_property(s, LIT("Applied"), LIT("False"), 0);
        emit_property(s, LIT("NoOp"), LIT("True"), 0);
    } else {
        emit_property(s, LIT("Applied"), LIT("True"), 0);
    }
    if (isp) emit_property(s, LIT("Rendering"), LIT(Z8_META_RENDERING_ISP), 0);
    else emit_property(s, LIT("Rendering"), LIT(Z8_META_RENDERING), 0);
    emit_property(s, LIT("LookName"), r->name, name_bytes, 1);

    emit(s, LIT(TAG_OPEN "TransformSHA256>"));
    emit_hex(s, r->id, 32);
    emit(s, LIT(TAG_CLOSE "TransformSHA256>\n"));
    if (isp) {
        emit(s, LIT(TAG_OPEN "FitSHA256>"));
        emit_hex(s, r->fit, 32);
        emit(s, LIT(TAG_CLOSE "FitSHA256>\n"));
    }

    emit_property(s, LIT("InputBasis"), basis, basis_bytes, 0);
    emit_number(s, LIT("Strength"), r->strength);
    emit_property(s, LIT("StrengthScale"), LIT("256"), 0);
    emit_number(s, LIT("Grid"), r->grid);
    emit_property(s, LIT("Schema"), LIT(Z8_META_SCHEMA), 0);
    if (isp) emit_property(s, LIT("Algorithm"), LIT(Z8_LUT_ISP_SEMANTICS), 1);
    else emit_property(s, LIT("Algorithm"), LIT(Z8_LUT_SEMANTICS), 1);
    if (firmware_bytes) emit_property(s, LIT("FirmwareIdentity"), r->firmware, firmware_bytes, 1);
    emit(s, LIT("</rdf:Description>"));
}

static unsigned starts_with(const char *data, uint32_t bytes,
                            const char *needle, uint32_t needle_bytes)
{
    if (needle_bytes > bytes) return 0;
    for (uint32_t i = 0; i < needle_bytes; i++) if (data[i] != needle[i]) return 0;
    return 1;
}

static unsigned contains(const char *data, uint32_t bytes,
                         const char *needle, uint32_t needle_bytes)
{
    if (!needle_bytes || needle_bytes > bytes) return 0;
    for (uint32_t at = 0; at + needle_bytes <= bytes; at++)
        if (starts_with(data + at, bytes - at, needle, needle_bytes)) return 1;
    return 0;
}

/* The real structural close is the LAST one: a deceptive `</rdf:RDF>` inside a
 * property value cannot be mistaken for it, and anything masquerading after it
 * would have to sit inside the padding, which is verified to be spaces. */
static uint32_t find_last(const char *data, uint32_t bytes,
                          const char *needle, uint32_t needle_bytes)
{
    if (!needle_bytes || needle_bytes > bytes) return UINT32_MAX;
    for (uint32_t at = bytes - needle_bytes + 1u; at-- > 0;)
        if (starts_with(data + at, bytes - at, needle, needle_bytes)) return at;
    return UINT32_MAX;
}

static unsigned space(char c)
{
    return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

/* ---- bounded structural scan of the opening envelope -------------------
 * Not an XML parser: it recognises just enough syntax -- processing
 * instructions, comments and quoted attribute values -- that the required
 * namespaces cannot be satisfied by text hidden inside a comment or an
 * attribute value. Interior well-formedness is the composer's precondition. */

/* Consume whitespace, comments and processing instructions. Fails on an
 * unterminated construct or an exhausted window. */
static unsigned skip_trivia(const char *p, uint32_t n, uint32_t *at)
{
    for (;;) {
        while (*at < n && space(p[*at])) (*at)++;
        if (*at >= n) return 0;
        if (starts_with(p + *at, n - *at, LIT("<!--"))) {
            uint32_t i = *at + 4u;
            while (i + 3u <= n && !starts_with(p + i, n - i, LIT("-->"))) i++;
            if (i + 3u > n) return 0;
            *at = i + 3u;
            continue;
        }
        if (starts_with(p + *at, n - *at, LIT("<?"))) {
            uint32_t i = *at + 2u;
            while (i + 2u <= n && !starts_with(p + i, n - i, LIT("?>"))) i++;
            if (i + 2u > n) return 0;
            *at = i + 2u;
            continue;
        }
        return 1;
    }
}

/* `at` is the '<'. The name must be followed by a name-terminating byte so
 * `x:xmpmetaX` cannot pass as `x:xmpmeta`. */
static unsigned element_is(const char *p, uint32_t n, uint32_t at,
                           const char *name, uint32_t name_bytes)
{
    if (at >= n || p[at] != '<') return 0;
    if (!starts_with(p + at + 1u, n - at - 1u, name, name_bytes)) return 0;
    uint32_t after = at + 1u + name_bytes;
    if (after >= n) return 0;
    return space(p[after]) || p[after] == '>' || p[after] == '/';
}

/* End of a start tag, skipping quoted regions so a '>' inside an attribute
 * value does not terminate it early. */
static unsigned tag_end(const char *p, uint32_t n, uint32_t at, uint32_t *end)
{
    while (at < n) {
        char c = p[at];
        if (c == '"' || c == '\'') {
            char quote = c;
            at++;
            while (at < n && p[at] != quote) at++;
            if (at >= n) return 0;
            at++;
            continue;
        }
        if (c == '>') { *end = at; return 1; }
        at++;
    }
    return 0;
}

/* Exact `attr="value"` inside [from, to). Whole quoted values are stepped
 * over, so an attribute name appearing inside another value is never matched. */
static unsigned attribute_is(const char *p, uint32_t from, uint32_t to,
                             const char *attr, uint32_t attr_bytes,
                             const char *value, uint32_t value_bytes)
{
    uint32_t at = from;
    while (at < to) {
        if (p[at] == '"' || p[at] == '\'') {
            char quote = p[at++];
            while (at < to && p[at] != quote) at++;
            if (at < to) at++;
            continue;
        }
        if (!space(p[at])) { at++; continue; }
        uint32_t i = at;
        while (i < to && space(p[i])) i++;          /* i > at, so this advances */
        if (starts_with(p + i, to - i, attr, attr_bytes)) {
            uint32_t j = i + attr_bytes;
            while (j < to && space(p[j])) j++;
            if (j < to && p[j] == '=') {
                j++;
                while (j < to && space(p[j])) j++;
                if (j < to && (p[j] == '"' || p[j] == '\'')) {
                    char quote = p[j++];
                    if (starts_with(p + j, to - j, value, value_bytes) &&
                        j + value_bytes < to && p[j + value_bytes] == quote) return 1;
                }
            }
        }
        at = i;
    }
    return 0;
}

/* The packet must open as an XMP packet whose single element is x:xmpmeta
 * carrying the Adobe meta namespace, containing rdf:RDF carrying the RDF
 * namespace. Closing-tag checks alone would accept arbitrary non-XMP content,
 * so this runs before anything else looks at the tail. */
static unsigned envelope_valid(const char *p, uint32_t n)
{
    const uint32_t window = n < Z8_META_MAX_ENVELOPE ? n : Z8_META_MAX_ENVELOPE;
    uint32_t at = 0, end = 0;

    if (!starts_with(p, window, LIT("<?xpacket begin="))) return 0;
    if (!skip_trivia(p, window, &at)) return 0;     /* consumes that PI */

    if (!element_is(p, window, at, LIT("x:xmpmeta"))) return 0;
    if (!tag_end(p, window, at + 1u, &end)) return 0;
    if (!attribute_is(p, at + 1u + (uint32_t)(sizeof("x:xmpmeta") - 1u), end,
                      LIT("xmlns:x"), LIT(Z8_META_XMPMETA_NS))) return 0;
    at = end + 1u;

    if (!skip_trivia(p, window, &at)) return 0;
    if (!element_is(p, window, at, LIT("rdf:RDF"))) return 0;
    if (!tag_end(p, window, at + 1u, &end)) return 0;
    if (!attribute_is(p, at + 1u + (uint32_t)(sizeof("rdf:RDF") - 1u), end,
                      LIT("xmlns:rdf"), LIT(Z8_META_RDF_NS))) return 0;
    return 1;
}

/* `packet` is mutated while `record` is read, so they must be distinct
 * objects; `const` cannot rule out physical aliasing. Treat address wrap as
 * invalid rather than reasoning about it. */
static unsigned disjoint(uintptr_t a, uint32_t a_bytes, uintptr_t b, uint32_t b_bytes)
{
    if (a + a_bytes < a || b + b_bytes < b) return 0;
    return a >= b + b_bytes || b >= a + a_bytes;
}

/* Re-validate everything rather than trusting the caller: a corrupted record
 * must be refused, never emitted as a half-truth. */
static unsigned provenance_valid(const Z8LutProvenance *r,
                             uint32_t *name_bytes, uint32_t *firmware_bytes)
{
    if (!z8_lut_name_valid(r->name, sizeof(r->name))) return 0;
    uint32_t n = 0;
    while (n < sizeof(r->name) && r->name[n]) n++;

    /* Exactly the grids and kinds the bundle contract admits (lut.c z8_lut_validate). */
    if (r->identity > 1u || r->strength > 256u) return 0;
    if (r->grid != 17u && r->grid != 33u && r->grid != 65u) return 0;
    if (r->rendering > Z8_RENDER_ISP || r->basis >= Z8_BASES ||
        (r->basis != 0u) != (r->rendering == Z8_RENDER_ISP)) return 0;
    if (r->rendering != Z8_RENDER_ISP)
        for (uint32_t i = 0; i < sizeof(r->fit); i++) if (r->fit[i]) return 0;

    uint32_t f = 0;
    while (f < Z8_META_FIRMWARE_BYTES && r->firmware[f]) f++;
    if (f == Z8_META_FIRMWARE_BYTES) return 0;          /* must be terminated */
    for (uint32_t i = 0; i < f; i++) {
        unsigned char c = (unsigned char)r->firmware[i];
        if (c < 0x20u || c > 0x7eu) return 0;           /* printable ASCII only */
    }
    for (uint32_t i = f; i < Z8_META_FIRMWARE_BYTES; i++) if (r->firmware[i]) return 0;

    *name_bytes = n;
    *firmware_bytes = f;
    return 1;
}

unsigned z8_meta_append(char *packet, uint32_t capacity,
                        const Z8LutProvenance *record, uint32_t *appended)
{
    if (!packet || !record || capacity < Z8_META_MIN_PACKET || capacity > Z8_META_MAX_PACKET)
        return Z8_META_BAD_ARGUMENT;

    /* The packet is written while the record is read, so they must be
     * separate objects; likewise the out-parameter. */
    const uintptr_t p = (uintptr_t)packet, r = (uintptr_t)record;
    if (!disjoint(p, capacity, r, (uint32_t)sizeof(*record))) return Z8_META_BAD_ARGUMENT;
    if (appended) {
        const uintptr_t a = (uintptr_t)appended;
        if (!disjoint(p, capacity, a, (uint32_t)sizeof(*appended)) ||
            !disjoint(r, (uint32_t)sizeof(*record), a, (uint32_t)sizeof(*appended)))
            return Z8_META_BAD_ARGUMENT;
    }

    uint32_t name_bytes = 0, firmware_bytes = 0;
    if (!provenance_valid(record, &name_bytes, &firmware_bytes)) return Z8_META_BAD_RECORD;

    /* Opening envelope first: the closing tags and trailer say nothing about
     * whether this is actually an XMP/RDF packet. */
    if (!envelope_valid(packet, capacity)) return Z8_META_BAD_PACKET;
    /* Writing twice would duplicate the provenance, so refuse instead. */
    if (contains(packet, capacity, LIT(Z8_META_NS))) return Z8_META_DUPLICATE;

    const uint32_t rdf = find_last(packet, capacity, LIT("</rdf:RDF>"));
    if (rdf == UINT32_MAX) return Z8_META_BAD_PACKET;

    uint32_t at = rdf + (uint32_t)(sizeof("</rdf:RDF>") - 1u);
    while (at < capacity && space(packet[at])) at++;
    if (!starts_with(packet + at, capacity - at, LIT("</x:xmpmeta>"))) return Z8_META_BAD_PACKET;
    at += (uint32_t)(sizeof("</x:xmpmeta>") - 1u);

    /* The block relocated by the insertion: `</rdf:RDF>` .. `</x:xmpmeta>`. */
    const uint32_t closing = at - rdf;
    if (closing > Z8_META_MAX_CLOSING) return Z8_META_BAD_PACKET;

    /* Only a verified run of plain spaces may be consumed. Any other byte
     * between the closing tags and the trailer means this is not padding we
     * understand, so refuse rather than overwrite real content. */
    uint32_t pad = at;
    while (pad < capacity && packet[pad] == ' ') pad++;
    const uint32_t available = pad - at;

    if (!starts_with(packet + pad, capacity - pad, LIT("<?xpacket end="))) return Z8_META_BAD_PACKET;
    const uint32_t tail = pad + (uint32_t)(sizeof("<?xpacket end=") - 1u);
    if (tail + 5u > capacity) return Z8_META_BAD_PACKET;
    if (packet[tail] != '"' || (packet[tail + 1] != 'w' && packet[tail + 1] != 'r') ||
        packet[tail + 2] != '"' || packet[tail + 3] != '?' || packet[tail + 4] != '>')
        return Z8_META_BAD_PACKET;
    for (uint32_t i = tail + 5u; i < capacity; i++)
        if (packet[i] && !space(packet[i])) return Z8_META_BAD_PACKET;

    /* Measure first so the write pass can be bounded exactly. */
    Sink measure = { 0, 0, 0, 0 };
    emit_record(&measure, record, name_bytes, firmware_bytes);
    const uint32_t needed = measure.len;
    if (needed > available) return Z8_META_NO_SPACE;

    /* Relocate the closing block right by `needed`, consuming that many
     * padding spaces. Copy high-to-low because the spans overlap. The trailer
     * offset, the packet length and every byte before `rdf` all stay put. */
    for (uint32_t i = closing; i-- > 0;) packet[rdf + needed + i] = packet[rdf + i];

    /* Both passes are the same deterministic emission over the same immutable
     * record, so this produces exactly `needed` bytes; the sink stays bounded
     * by that span so it can never write past the reserved room. */
    Sink write = { packet + rdf, needed, 0, 0 };
    emit_record(&write, record, name_bytes, firmware_bytes);
    while (write.len < needed) packet[rdf + write.len++] = ' ';

    if (appended) *appended = needed;
    return Z8_META_APPENDED;
}
