#!/usr/bin/env python3
"""Make a password hash for the Worker secret APP_PASSWORD_HASH.

    python3 scripts/password.py                 # generate a random 24-character password
    python3 scripts/password.py --password 'x'  # hash the password you choose

The hash goes to Cloudflare, never the password:

    python3 scripts/password.py | tee /dev/stderr | sed -n 's/^hash: //p' | npx wrangler secret put APP_PASSWORD_HASH
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import secrets
import string

ITERATIONS = 50_000


def pbkdf2_sha256(password: bytes, salt: bytes, iterations: int, length: int = 32) -> bytes:
    """Pure Python, identical to src/main.py: Pyodide has no hashlib.pbkdf2_hmac."""
    mac = hmac.new(password, b"", hashlib.sha256)
    blocks = -(-length // 32)
    out = b""
    for index in range(1, blocks + 1):
        seed = mac.copy()
        seed.update(salt + index.to_bytes(4, "big"))
        u = seed.digest()
        acc = int.from_bytes(u, "big")
        for _ in range(iterations - 1):
            step = mac.copy()
            step.update(u)
            u = step.digest()
            acc ^= int.from_bytes(u, "big")
        out += acc.to_bytes(32, "big")
    return out[:length]


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = pbkdf2_sha256(password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", help="password to hash; omit to generate one")
    parser.add_argument("--length", type=int, default=24)
    parser.add_argument("--iterations", type=int, default=ITERATIONS)
    options = parser.parse_args()

    password = options.password
    if not password:
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(options.length))

    print(f"password: {password}")
    print(f"hash: {hash_password(password, options.iterations)}")


if __name__ == "__main__":
    main()
