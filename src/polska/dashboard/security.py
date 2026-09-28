"""Password hashing, login rate limiting, and CSRF tokens.

Three separate, narrow concerns, kept in one file because none of them is more
than a few lines and none of them belongs anywhere else. Nothing here talks to
the database: a login attempt either matches the one admin hash from settings
or it does not, and the rate limiter and CSRF store are both in-process memory,
which is correct for the single-process deployment this runs as (one uvicorn
worker; the scheduler is a separate container). If this ever runs with more
than one worker, both of these need to move to something shared (Redis, the
database) or a restart-lucky attacker gets a fresh rate-limit budget per
worker. Noted here so that move is a known consequence, not a surprise.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    """Argon2id hash of a plaintext password. Never store the plaintext itself."""
    return _hasher.hash(plaintext)


def verify_password(password_hash: str, plaintext: str) -> bool:
    """True if ``plaintext`` matches the stored hash. Never raises on a mismatch."""
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, plaintext)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001 - a malformed stored hash is a mismatch too
        return False


@dataclass
class LoginRateLimiter:
    """Locks out an IP after too many failed attempts in a sliding window.

    Deliberately per-IP, not global: one careless script hammering the login
    form should not also lock out the admin's own next attempt from a
    different address. Successful login clears that IP's history immediately,
    so a legitimate user who mistypes a few times is never punished for
    longer than the window once they get it right.
    """

    max_attempts: int = 5
    window_seconds: float = 900.0  # 15 minutes
    _failures: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def _prune(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        kept = [t for t in self._failures[key] if t > cutoff]
        self._failures[key] = kept
        return kept

    def is_locked_out(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.max_attempts

    def seconds_until_retry(self, key: str) -> int:
        """How long until the oldest failure in the window ages out. 0 if not locked."""
        now = time.monotonic()
        attempts = self._prune(key, now)
        if len(attempts) < self.max_attempts:
            return 0
        oldest = min(attempts)
        return max(0, int(oldest + self.window_seconds - now) + 1)

    def record_failure(self, key: str) -> None:
        self._failures[key].append(time.monotonic())

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)


def new_csrf_token() -> str:
    """A fresh token to store in the session and echo back on every form."""
    return secrets.token_urlsafe(32)


def csrf_token_matches(session_token: str | None, submitted_token: str | None) -> bool:
    """Constant-time comparison, so a mismatch cannot be timed to guess the token."""
    if not session_token or not submitted_token:
        return False
    return hmac.compare_digest(session_token, submitted_token)
