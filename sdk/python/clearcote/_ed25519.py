"""Ed25519 signature verification (RFC 8032, section 5.1.7), standard library only.

Verification only: the SDK holds public keys and never signs. Everything here works on public data, so
nothing needs to run in constant time. Points are kept in extended coordinates (X, Y, Z, T) with
x = X/Z, y = Y/Z and x*y = T/Z, as in the RFC's reference code.
"""

import hashlib

_P = 2**255 - 19  # the field prime
_L = 2**252 + 27742317777372353535851937790883648493  # the order of the base point
_D = -121665 * pow(121666, _P - 2, _P) % _P  # the curve constant d
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)  # a square root of -1


def _add(a, b):
    """The sum of two points."""
    e = (a[1] - a[0]) * (b[1] - b[0]) % _P
    f = (a[1] + a[0]) * (b[1] + b[0]) % _P
    g = 2 * a[3] * b[3] * _D % _P
    h = 2 * a[2] * b[2] % _P
    return ((f - e) * (h - g) % _P, (h + g) * (f + e) % _P, (h - g) * (h + g) % _P, (f - e) * (f + e) % _P)


def _mul(n, point):
    """``n`` times a point (double and add)."""
    out = (0, 1, 1, 0)  # the neutral element
    while n > 0:
        if n & 1:
            out = _add(out, point)
        point = _add(point, point)
        n >>= 1
    return out


def _same(a, b):
    """True when two points are the same point (x1/z1 == x2/z2 and y1/z1 == y2/z2)."""
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _decode(raw):
    """The point 32 bytes stand for, or None when they are not the encoding of a curve point.

    The bytes hold y (little-endian, 255 bits) and the lowest bit of x in the top bit. x is recovered from
    the curve equation x^2 = (y^2 - 1) / (d*y^2 + 1); a y that is not below the prime, or one with no
    matching x, is refused."""
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        if sign:
            return None
        x = 0
    else:
        x = pow(x2, (_P + 3) // 8, _P)
        if (x * x - x2) % _P != 0:
            x = x * _SQRT_M1 % _P
        if (x * x - x2) % _P != 0:
            return None  # x2 has no square root: no such point
        if x & 1 != sign:
            x = _P - x
    return (x, y, 1, x * y % _P)


# The base point: y = 4/5, x even.
_BASE = _decode((4 * pow(5, _P - 2, _P) % _P).to_bytes(32, "little"))


def verify(public_key, message, signature):
    """True when ``signature`` (64 bytes) is the Ed25519 signature of ``message`` under ``public_key``
    (32 raw bytes). False for anything else: a wrong length, a public key or an R that is not a curve
    point, an S that is not below the group order, or a signature that does not match. Never raises on
    bytes input."""
    if not isinstance(public_key, (bytes, bytearray)) or not isinstance(signature, (bytes, bytearray)):
        return False
    if not isinstance(message, (bytes, bytearray)) or len(public_key) != 32 or len(signature) != 64:
        return False
    public_key, message, signature = bytes(public_key), bytes(message), bytes(signature)
    a = _decode(public_key)
    r = _decode(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
    # [S]B == R + [k]A
    return _same(_mul(s, _BASE), _add(r, _mul(k, a)))
