"""Two-step sign-in: a 6-digit code from an authenticator app (TOTP, RFC 6238) or a one-time recovery code.

The phone and the server share a secret (shown once as a QR code); each holds the time and makes the same
code every 30 seconds. Nothing is sent to the phone, so it works without signal and costs nothing.
Any standard app works: Google Authenticator, Microsoft Authenticator, 1Password, ...

* a code is accepted 30 seconds early or late (phones' clocks drift) and only once (``last_step``);
* recovery codes are for a lost phone: 10, each works once, only their hashes are kept.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from urllib.parse import quote

STEP_S = 30
DIGITS = 6
DRIFT = 1                 # steps accepted either side of now
ISSUER = "ORchestraAI"
RECOVERY_COUNT = 10
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to read from paper
SECRET = re.compile(r"^[A-Z2-7]{16,64}$")


def new_secret() -> str:
    """160 random bits, base32 (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def valid_secret(secret: str | None) -> bool:
    return bool(secret) and bool(SECRET.match(secret.strip().upper().replace(" ", "")))


def _key(secret: str) -> bytes:
    secret = secret.strip().upper().replace(" ", "")
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def step_now(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_S)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def clean(code: str | None) -> str:
    return re.sub(r"[\s-]", "", str(code or "")).lower()[:40]


def is_app_code(code: str) -> bool:
    return bool(re.fullmatch(r"\d{%d}" % DIGITS, code))


def match(secret: str, code: str, *, last_step: int | None = None, now: float | None = None) -> int | None:
    """The step the code belongs to, or None (wrong, too old, or already used)."""
    code = clean(code)
    if not is_app_code(code) or not valid_secret(secret):
        return None
    current = step_now(now)
    for step in range(current - DRIFT, current + DRIFT + 1):
        if last_step is not None and step <= last_step:
            continue  # this code (or an earlier one) was used already
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def uri(secret: str, account: str) -> str:
    """What the QR code holds, in the form Google's own documentation shows (SHA-1, 6 digits and 30 seconds
    are what every app assumes, so they are left out: a shorter text makes a QR code with bigger dots)."""
    label = quote(f"{ISSUER}:{account}", safe=":@")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"


def qr_svg(text: str) -> str:
    """The QR code as inline SVG (drawn on the server: no script from elsewhere, nothing sent out).
    It scales to whatever size the page gives it (``viewBox``, no fixed size: a fixed size squeezed by CSS
    cut the code off) and keeps the 4-dot white margin scanners need."""
    import segno

    return segno.make_qr(text, error="m").svg_inline(scale=5, border=4, dark="#000000", light="#ffffff",
                                                     omitsize=True)


def new_recovery_codes() -> list[str]:
    """10 codes like ``k7mq2-x9rtp`` (50 random bits each)."""
    def one() -> str:
        chars = "".join(secrets.choice(_ALPHABET) for _ in range(10))
        return f"{chars[:5]}-{chars[5:]}"
    return [one() for _ in range(RECOVERY_COUNT)]


def hash_recovery(code: str) -> str:
    return hashlib.sha256(("orchestra-recovery:" + clean(code)).encode()).hexdigest()
