import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (RFC 9562) application-side, for tests and fixtures.

    Real rows rely on Postgres 18's native uuidv7() server default instead —
    this exists so tests and factories can generate matching IDs without a
    database round-trip.
    """
    unix_ts_ms = int(time.time() * 1000)
    rand = os.urandom(10)

    rand_a = int.from_bytes(rand[0:2], "big") & 0x0FFF
    rand_b = int.from_bytes(rand[2:10], "big") & 0x3FFFFFFFFFFFFFFF

    version_and_rand_a = (0x7 << 12) | rand_a
    variant_and_rand_b = (0b10 << 62) | rand_b

    value = (unix_ts_ms << 80) | (version_and_rand_a << 64) | variant_and_rand_b

    return uuid.UUID(int=value)
