#!/usr/bin/env python3
"""Make a modified copy of a FIT activity file.

Changes made:
  * power (record.power): each sample changed by up to +/-4% (--power-tol)
  * cadence (record.cadence + record.fractional_cadence): up to +/-5%
    (--cadence-tol)
  * by default each channel gets one random offset for the whole ride (up to
    60% of the margin) plus per-sample noise (the rest), so averages shift too;
    --no-offset gives per-sample noise only
  * zero runs (coasting/stops) in power and cadence get their start and end
    moved by up to 2 samples each (--zero-shift), so zeros no longer line up.
    The samples this touches go outside the margins above, since any change
    to or from zero is a 100% change.
  * heart rate: unchanged
  * speed: removed (record speed/enhanced_speed, lap/session avg/max speed)
  * product name in file_id and device_info set to "claude code", and
    manufacturer set to 52 ("seiko_epson"; FIT has no Anthropic ID)
  * lap/session avg/max power and cadence recomputed from the new samples

The file is rewritten at the byte level. Everything else is copied as-is,
then the header and file CRCs are recalculated. Standard library only.

Usage:
  python3 make_variant_fit.py input.fit output.fit [--seed N]
      [--power-tol PCT] [--cadence-tol PCT] [--no-offset] [--zero-shift N]
"""
import argparse
import math
import random
import struct

POWER_TOL = 0.04     # default, override with --power-tol
CADENCE_TOL = 0.05   # default, override with --cadence-tol
OFFSET_SHARE = 0.6   # share of the margin used by the whole-ride offset
ZERO_SHIFT = 2       # max samples each zero-run edge moves; 0 disables
DEVICE_NAME = b"claude code"

# Global message numbers
FILE_ID, DEVICE_INFO, RECORD, LAP, SESSION = 0, 23, 20, 19, 18

# Fields to drop per message: speed data
DROP_FIELDS = {
    RECORD: {6, 73, 17},       # speed, enhanced_speed, speed_1s
    LAP: {13, 14, 110, 111},   # avg/max speed, enhanced avg/max speed
    SESSION: {14, 15, 124, 125},
}
PRODUCT_NAME_FIELD = {FILE_ID: 8, DEVICE_INFO: 27}
MANUFACTURER_FIELD = {FILE_ID: 1, DEVICE_INFO: 2}
MANUFACTURER = 52   # "seiko_epson" in the FIT manufacturer list

# Record fields
R_TIMESTAMP, R_CADENCE, R_POWER, R_FRAC_CAD = 253, 4, 7, 53
# Summary fields: (start_time, timestamp, avg_cad, max_cad, avg_pow, max_pow)
SUMMARY = {
    LAP: (2, 253, 17, 18, 19, 20),
    SESSION: (2, 253, 18, 19, 20, 21),
}

CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def fit_crc(data, crc=0):
    for b in data:
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[b & 0xF]
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[(b >> 4) & 0xF]
    return crc


class Definition:
    def __init__(self, arch, gnum, fields, dev_fields):
        self.arch = arch              # 0 = little endian, 1 = big endian
        self.gnum = gnum
        self.fields = fields          # list of (num, size, base_type)
        self.dev_fields = dev_fields  # list of (num, size, dev_index)

    @property
    def endian(self):
        return "<" if self.arch == 0 else ">"


class Message:
    """One data message: field values kept as raw bytes, keyed by field number."""

    def __init__(self, header, defn, values, dev_bytes):
        self.header = header
        self.defn = defn
        self.values = values      # dict num -> bytes
        self.dev_bytes = dev_bytes

    def get_uint(self, num):
        raw = self.values.get(num)
        if raw is None:
            return None
        if len(raw) not in (1, 2, 4):
            return None
        fmt = {1: "B", 2: "H", 4: "I"}[len(raw)]
        val = struct.unpack(self.defn.endian + fmt, raw)[0]
        if val == (1 << (8 * len(raw))) - 1:  # invalid marker
            return None
        return val

    def set_uint(self, num, val):
        raw = self.values[num]
        fmt = {1: "B", 2: "H", 4: "I"}[len(raw)]
        self.values[num] = struct.pack(self.defn.endian + fmt, val)


def parse(data):
    header_size = data[0]
    data_size = struct.unpack("<I", data[4:8])[0]
    if data[8:12] != b".FIT":
        raise ValueError("not a FIT file")
    header = bytearray(data[:header_size])
    items = []   # ('def', header_byte, local, Definition) or ('msg', Message)
    defs = {}
    p, end = header_size, header_size + data_size
    while p < end:
        h = data[p]
        p += 1
        if h & 0x80:          # compressed timestamp header
            local, is_def, has_dev = (h >> 5) & 0x3, False, False
        else:
            local, is_def, has_dev = h & 0x0F, bool(h & 0x40), bool(h & 0x20)
        if is_def:
            arch = data[p + 1]
            gnum = struct.unpack("<H" if arch == 0 else ">H", data[p + 2:p + 4])[0]
            n = data[p + 4]
            p += 5
            fields = [tuple(data[p + 3 * i:p + 3 * i + 3]) for i in range(n)]
            p += 3 * n
            dev_fields = []
            if has_dev:
                nd = data[p]
                p += 1
                dev_fields = [tuple(data[p + 3 * i:p + 3 * i + 3]) for i in range(nd)]
                p += 3 * nd
            d = Definition(arch, gnum, fields, dev_fields)
            defs[local] = d
            items.append(("def", h, local, d))
        else:
            d = defs[local]
            values = {}
            for num, size, _ in d.fields:
                values[num] = data[p:p + size]
                p += size
            dev_len = sum(f[1] for f in d.dev_fields)
            dev_bytes = data[p:p + dev_len]
            p += dev_len
            items.append(("msg", Message(h, d, values, dev_bytes)))
    if end + 2 != len(data):
        raise ValueError("chained FIT files or trailing data are not supported")
    return header, items


def serialize(header, items):
    body = bytearray()
    for item in items:
        if item[0] == "def":
            _, h, local, d = item
            fields = [f for f in d.fields if f[0] not in DROP_FIELDS.get(d.gnum, ())]
            body.append(h)
            body += bytes([0, d.arch])
            body += struct.pack(d.endian + "H", d.gnum)
            body.append(len(fields))
            for f in fields:
                body += bytes(f)
            if h & 0x20:
                body.append(len(d.dev_fields))
                for f in d.dev_fields:
                    body += bytes(f)
        else:
            m = item[1]
            body.append(m.header)
            drop = DROP_FIELDS.get(m.defn.gnum, ())
            for num, _, _ in m.defn.fields:
                if num not in drop:
                    body += m.values[num]
            body += m.dev_bytes
    header = bytearray(header)
    header[4:8] = struct.pack("<I", len(body))
    if len(header) >= 14:
        header[12:14] = struct.pack("<H", fit_crc(header[:12]))
    out = bytes(header) + bytes(body)
    return out + struct.pack("<H", fit_crc(out))


def jitter_int(value, tol, rng, offset=0.0):
    """Random integer within +/-tol of value (the bound holds after rounding).

    offset is a fixed relative shift; noise fills the rest of the margin.
    """
    lo, hi = math.ceil(value * (1 - tol)), math.floor(value * (1 + tol))
    noise = tol - abs(offset)
    target = round(value * (1 + offset + rng.uniform(-noise, noise)))
    return min(max(target, lo), hi)


def set_product_name(msg):
    num = PRODUCT_NAME_FIELD[msg.defn.gnum]
    if num not in msg.values:
        raise ValueError(f"message {msg.defn.gnum} has no product_name field")
    size = len(msg.values[num])
    if size < len(DEVICE_NAME) + 1:
        raise ValueError("product_name field too small for the new name")
    msg.values[num] = DEVICE_NAME + b"\x00" * (size - len(DEVICE_NAME))


def full_timestamps(items):
    """Timestamp of each record message, resolving compressed headers."""
    last, out = None, {}
    for item in items:
        if item[0] != "msg":
            continue
        m = item[1]
        if m.header & 0x80:
            offset = m.header & 0x1F
            ts = (last & ~0x1F) + offset
            if offset < (last & 0x1F):
                ts += 0x20
        else:
            ts = m.get_uint(R_TIMESTAMP)
        if ts is not None:
            last = ts
        if m.defn.gnum == RECORD:
            out[id(m)] = ts
    return out


def shift_zero_edges(old, new, max_shift, rng):
    """Move the start and end of each zero run in `old` by up to max_shift samples.

    Edits `new` in place. A run that grows zeroes out neighbouring samples; a
    run that shrinks gets a fading value (half the neighbour, then a quarter...),
    like a meter easing to zero. Each run moves at least one edge, and keeps at
    least one zero. Returns the indices that were changed.
    """
    n = len(old)
    runs, i = [], 0
    while i < n:
        if old[i] == 0:
            j = i
            while j < n and old[j] == 0:
                j += 1
            runs.append((i, j))  # end is exclusive
            i = j
        else:
            i += 1
    changed = set()
    if max_shift <= 0:
        return changed
    for s, e in runs:
        ds = de = 0
        while ds == 0 and de == 0:
            ds = rng.randint(-max_shift, max_shift)
            de = rng.randint(-max_shift, max_shift)
        ns, ne = max(0, s + ds), min(n, e + de)
        if ne - ns < 1:  # keep at least one zero
            ns, ne = s, s + 1
        for i in list(range(ns, s)) + list(range(e, ne)):  # run grows
            if new[i] is not None:
                new[i] = 0
                changed.add(i)
        before = new[s - 1] if s > 0 and new[s - 1] else 0
        for k, i in enumerate(range(s, min(ns, e))):       # start moves later
            new[i] = max(1, round(before * 0.5 ** (k + 1))) if before else 0
            changed.add(i)
        after = new[e] if e < n and new[e] else 0
        for i in range(max(ne, s), e):                      # end moves earlier
            new[i] = max(1, round(after * 0.5 ** (e - i))) if after else 0
            changed.add(i)
    return changed


def transform(items, rng, power_tol=POWER_TOL, cadence_tol=CADENCE_TOL,
              use_offset=True, zero_shift=ZERO_SHIFT):
    share = OFFSET_SHARE if use_offset else 0.0
    p_off = rng.uniform(-share, share) * power_tol
    c_off = rng.uniform(-share, share) * cadence_tol
    print(f"whole-ride offset: power {p_off:+.2%}, cadence {c_off:+.2%}")
    records = [it[1] for it in items if it[0] == "msg" and it[1].defn.gnum == RECORD]
    stamps = full_timestamps(items)
    has_frac = [R_FRAC_CAD in m.values for m in records]

    # Pass 1: per-sample changes within the margins
    old_p = [m.get_uint(R_POWER) for m in records]
    new_p = [None if v is None else jitter_int(v, power_tol, rng, p_off) for v in old_p]
    old_c = []  # cadence in 1/128 rpm
    for m, frac_ok in zip(records, has_frac):
        cad = m.get_uint(R_CADENCE)
        frac = (m.get_uint(R_FRAC_CAD) or 0) if frac_ok else 0
        old_c.append(None if cad is None else cad * 128 + frac)
    new_c = []
    for v, frac_ok in zip(old_c, has_frac):
        if v is None or not frac_ok:  # whole rpm only: keep the original
            new_c.append(v)
        else:
            new_c.append(jitter_int(v, cadence_tol, rng, c_off))

    # Pass 2: move the edges of zero runs so they no longer line up
    p_zero = shift_zero_edges(old_p, new_p, zero_shift, rng)
    c_zero = shift_zero_edges(old_c, new_c, zero_shift, rng)

    samples = []  # (ts, old_power, new_power, old_cad, new_cad)
    for i, m in enumerate(records):
        if new_p[i] is not None:
            m.set_uint(R_POWER, new_p[i])
        if new_c[i] is not None:
            q = min(new_c[i], 254 * 128 + 127)
            m.set_uint(R_CADENCE, q // 128)
            if has_frac[i]:
                m.set_uint(R_FRAC_CAD, q % 128)
            new_c[i] = q
        samples.append((stamps[id(m)], old_p[i], new_p[i],
                        None if old_c[i] is None else old_c[i] / 128,
                        None if new_c[i] is None else new_c[i] / 128))
    for item in items:
        if item[0] != "msg":
            continue
        m = item[1]
        g = m.defn.gnum
        if g in PRODUCT_NAME_FIELD:
            set_product_name(m)
            if MANUFACTURER_FIELD[g] in m.values:
                m.set_uint(MANUFACTURER_FIELD[g], MANUFACTURER)
        if g in SUMMARY:
            update_summary(m, SUMMARY[g], samples)
    return samples, p_zero, c_zero


def update_summary(m, fields, samples):
    """Rescale avg power/cadence by the sample ratio; take max from samples.

    Scaling (rather than recomputing from scratch) keeps whatever averaging
    rule the recording app used, e.g. whether zeros are included.
    """
    start_f, end_f, avg_c, max_c, avg_p, max_p = fields
    start, end = m.get_uint(start_f), m.get_uint(end_f)
    win = [s for s in samples
           if s[0] is not None and (start is None or s[0] >= start)
           and (end is None or s[0] <= end)]

    def rescale(avg_field, max_field, old_i, new_i):
        old = [s[old_i] for s in win if s[old_i] is not None]
        new = [s[new_i] for s in win if s[new_i] is not None]
        if not old or sum(old) == 0:
            return
        avg = m.get_uint(avg_field) if avg_field in m.values else None
        if avg is not None:
            m.set_uint(avg_field, round(avg * sum(new) / sum(old)))
        mx = m.get_uint(max_field) if max_field in m.values else None
        if mx is not None:
            if mx == round(max(old)):
                m.set_uint(max_field, round(max(new)))
            else:
                m.set_uint(max_field, round(mx * max(new) / max(old)))

    rescale(avg_p, max_p, 1, 2)
    rescale(avg_c, max_c, 3, 4)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--seed", type=int, help="random seed for repeatable output")
    ap.add_argument("--power-tol", type=float, default=POWER_TOL * 100,
                    help="max power change per sample, in %% (default %(default)s)")
    ap.add_argument("--cadence-tol", type=float, default=CADENCE_TOL * 100,
                    help="max cadence change per sample, in %% (default %(default)s)")
    ap.add_argument("--no-offset", action="store_true",
                    help="per-sample noise only, no whole-ride offset")
    ap.add_argument("--zero-shift", type=int, default=ZERO_SHIFT,
                    help="max samples to move each edge of a zero run "
                         "(default %(default)s, 0 keeps zeros aligned)")
    args = ap.parse_args()

    data = open(args.input, "rb").read()
    if fit_crc(data) != 0:
        print("warning: input file CRC does not check out")
    header, items = parse(data)
    samples, p_zero, c_zero = transform(items, random.Random(args.seed),
                                         args.power_tol / 100, args.cadence_tol / 100,
                                         not args.no_offset, args.zero_shift)
    out = serialize(header, items)
    with open(args.output, "wb") as f:
        f.write(out)

    p_dev = [abs(s[2] - s[1]) / s[1] for i, s in enumerate(samples)
             if s[1] and i not in p_zero]
    c_dev = [abs(s[4] - s[3]) / s[3] for i, s in enumerate(samples)
             if s[3] and i not in c_zero]
    print(f"wrote {args.output} ({len(out)} bytes, {len(samples)} records)")
    if p_dev:
        print(f"power:   max change {max(p_dev):.3%}, mean {sum(p_dev)/len(p_dev):.3%}")
    if c_dev:
        print(f"cadence: max change {max(c_dev):.3%}, mean {sum(c_dev)/len(c_dev):.3%}")
    print(f"zero-run edges moved: {len(p_zero)} power and {len(c_zero)} cadence "
          "samples changed (not counted above)")


if __name__ == "__main__":
    main()
