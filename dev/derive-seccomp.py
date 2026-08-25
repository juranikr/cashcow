#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: derive-seccomp.py SOURCE TARGET UPSTREAM_SHA256 DERIVED_SHA256"
        )

    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    expected_upstream = sys.argv[3].lower()
    expected_derived = sys.argv[4].lower()

    source_payload = source.read_bytes()
    actual_upstream = sha256(source_payload)
    if actual_upstream != expected_upstream:
        raise SystemExit(
            f"unexpected upstream seccomp SHA-256: {actual_upstream}"
        )

    profile = json.loads(source_payload)
    matches = [
        rule
        for rule in profile.get("syscalls", [])
        if rule.get("names") == ["chroot"]
        and rule.get("action") == "SCMP_ACT_ALLOW"
        and rule.get("args") == []
        and rule.get("includes") == {"caps": ["CAP_SYS_CHROOT"]}
    ]
    if len(matches) != 1:
        raise SystemExit("expected exactly one capability-gated chroot rule")

    # The container drops every host capability. Chromium gains CAP_SYS_CHROOT
    # only inside its fresh user namespace, so Docker must retain this syscall
    # rule even though the outer container has no CAP_SYS_CHROOT.
    matches[0]["includes"] = {}
    derived_payload = (
        json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    actual_derived = sha256(derived_payload)
    if actual_derived != expected_derived:
        raise SystemExit(f"unexpected derived seccomp SHA-256: {actual_derived}")

    target.write_bytes(derived_payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
