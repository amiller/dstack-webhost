"""Minimal pure-Python secp256k1 — just enough to derive a compressed public key
from a private scalar, so the daemon never has to expose private key material.
No external deps (the image only ships aiohttp). Not constant-time; only used on
the already-public derived public key, never as a signing oracle."""

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _inv(x):
    return pow(x, _P - 2, _P)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    (x1, y1), (x2, y2) = p, q
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p == q:
        m = (3 * x1 * x1) * _inv(2 * y1) % _P
    else:
        m = (y2 - y1) * _inv(x2 - x1) % _P
    x3 = (m * m - x1 - x2) % _P
    y3 = (m * (x1 - x3) - y1) % _P
    return (x3, y3)


def _mul(k, point):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def compressed_pubkey(priv_bytes: bytes) -> bytes:
    """Return the 33-byte SEC1 compressed public key for a 32-byte private key."""
    k = int.from_bytes(priv_bytes, "big") % _N
    if k == 0:
        raise ValueError("invalid private key")
    x, y = _mul(k, (_GX, _GY))
    return bytes([0x02 | (y & 1)]) + x.to_bytes(32, "big")
