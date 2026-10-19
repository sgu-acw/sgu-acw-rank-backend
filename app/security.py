"""Password hashing and session tokens.

Both primitives come from the standard library on purpose: the target host has
a tight memory budget and no compiler, so pulling in bcrypt or argon2 would cost
more than it buys. ``hashlib.scrypt`` is a memory-hard KDF and is a reasonable
stand-in.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .config import get_settings

#: scrypt produces 64-byte blocks per (r, p) pair; 32 bytes of salt is the
#: conventional choice and leaves the hash at 64 bytes.
SALT_BYTES = 32
KEY_LEN = 64

#: Session tokens are 32 random bytes, base64url-encoded by ``token_urlsafe``.
SESSION_TOKEN_BYTES = 32


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return ``scrypt$<salt-hex>$<key-hex>``.

    The parameters are embedded in the stored string so that raising the work
    factor later does not invalidate existing hashes.
    """
    settings = get_settings()
    salt = salt or secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=settings.scrypt_n,
        r=settings.scrypt_r,
        p=settings.scrypt_p,
        dklen=KEY_LEN,
    )
    return (
        f"scrypt${settings.scrypt_n}${settings.scrypt_r}${settings.scrypt_p}"
        f"${salt.hex()}${key.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    """Check ``password`` against a string produced by :func:`hash_password`.

    Returns False rather than raising on malformed input, so a corrupted row
    reads as a failed login instead of a 500.
    """
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        key = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(key_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key.hex(), key_hex)


def new_session_token() -> str:
    """A fresh opaque session token. Only its hash is ever stored."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Hash a session token for storage.

    A plain SHA-256 is right here: the token is already 256 bits of entropy, so
    there is nothing to brute force, and this keeps lookup a single indexed
    comparison.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_invite_code() -> str:
    """A short, human-readable invite code.

    Eight characters from an alphabet with the look-alikes removed, which leaves
    about 40 bits. Codes are single-use by default and expire quickly, so that
    is ample, and it survives being read aloud in a group chat.
    """
    return _random_code(8)


#: Alphabet without 0/O, 1/I/L — the characters people mistype when copying a
#: code or password out of a chat message. Uppercase only: invite codes are
#: matched case-insensitively against what the member types, and mixing case
#: would turn a transcription slip into a confusing "invalid code".
UNAMBIGUOUS_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _random_code(length: int) -> str:
    return "".join(secrets.choice(UNAMBIGUOUS_ALPHABET) for _ in range(length))


def new_temporary_password() -> str:
    """A password an administrator reads out to a member who lost theirs.

    Sixteen characters from the unambiguous alphabet, giving about 95 bits.
    Avoids base64url punctuation because this gets typed by hand.
    """
    return _random_code(16)


def new_verify_token() -> str:
    """The ``ACW-xxxx`` string a member pastes into a platform profile."""
    return "ACW-" + _random_code(4)


# --------------------------------------------------------------------------- #
# 两步验证登录票据
#
# 密码对了但还没过 TOTP 的中间态——不能发 session，用一张带签名的短票记录
# 「这个 uid 已经过了密码关」，等 TOTP 验完才发真 session。
# --------------------------------------------------------------------------- #

TICKET_TTL_SECONDS = 300


def new_login_ticket(user_id: int) -> str:
    """签发一张带随机 nonce 的签名票据，有效期 5 分钟。"""
    exp = int(time.time()) + TICKET_TTL_SECONDS
    body = f"{user_id}.{exp}.{secrets.token_urlsafe(24)}"
    sig = hmac.new(
        get_settings().ticket_secret.encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    return f"{body}.{sig}"


def verify_login_ticket(ticket: str) -> int | None:
    """校验票据，返回 user_id；过期或签名不对返回 None。"""
    try:
        uid_s, exp_s, nonce, sig = ticket.split(".")
        uid, exp = int(uid_s), int(exp_s)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(
        get_settings().ticket_secret.encode(), f"{uid_s}.{exp_s}.{nonce}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if exp < time.time():
        return None
    return uid
