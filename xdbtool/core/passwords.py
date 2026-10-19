# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Create password hashes for *new* accounts in the format XUI.one stores.

XUI.one keeps admin/reseller passwords as SHA-512 crypt (``$6$rounds=20000$salt$…``,
the glibc / PHP ``crypt()`` scheme). Python 3.13 removed the ``crypt`` module, so
the published algorithm (Ulrich Drepper, "Unix crypt using SHA-256 and SHA-512")
is implemented here. This only *creates* hashes for passwords the tool generates —
existing hashes are always copied verbatim and never reversed.
"""

from __future__ import annotations

import hashlib
import secrets
import string

_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# Byte order of the final transposed base64 encoding (from the specification).
_ORDER = (
    (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26),
    (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32),
    (12, 33, 54), (34, 55, 13), (56, 14, 35), (15, 36, 57), (37, 58, 16), (59, 17, 38),
    (18, 39, 60), (40, 61, 19), (62, 20, 41),
)


def _b64(b2: int, b1: int, b0: int, n: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_B64[w & 0x3F])
        w >>= 6
    return "".join(out)


def _repeat(digest: bytes, length: int) -> bytes:
    return (digest * (length // 64 + 1))[:length]


def sha512_crypt(password: str, salt: str | None = None, rounds: int = 20000) -> str:
    """Return the SHA-512 crypt hash of ``password`` (random 16-char hex salt by default)."""
    pw = password.encode("utf-8")
    salt = (salt if salt is not None else secrets.token_hex(8))[:16]
    s = salt.encode("utf-8")
    rounds = max(1000, min(999_999_999, rounds))

    b = hashlib.sha512(pw + s + pw).digest()
    a = hashlib.sha512(pw + s + _repeat(b, len(pw)))
    n = len(pw)
    while n:
        a.update(b if n & 1 else pw)
        n >>= 1
    a = a.digest()

    p = _repeat(hashlib.sha512(pw * len(pw)).digest(), len(pw))
    ds = _repeat(hashlib.sha512(s * (16 + a[0])).digest(), len(s))

    c = a
    for i in range(rounds):
        h = hashlib.sha512(p if i & 1 else c)
        if i % 3:
            h.update(ds)
        if i % 7:
            h.update(p)
        h.update(c if i & 1 else p)
        c = h.digest()

    enc = "".join(_b64(c[x], c[y], c[z], 4) for x, y, z in _ORDER) + _b64(0, 0, c[63], 2)
    return f"$6$rounds={rounds}${salt}${enc}"


def random_secret(length: int = 14, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))
