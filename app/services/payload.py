"""Canonical hashing of an approval payload.

SPEC-v2 D20: the approval is bound to a hash of the exact payload the human was
shown. That only works if the same payload always hashes the same way, so the
encoding is pinned here rather than left to whatever json.dumps defaults to at
the call site: keys sorted, no incidental whitespace, non-ASCII preserved
rather than escaped.
"""

import hashlib
import json
from typing import Any


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
