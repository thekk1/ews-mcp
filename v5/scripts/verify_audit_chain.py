#!/usr/bin/env python3
"""Verify the v5 audit log's hash chain (ported from the v3.5 verifier).

v5 records are JSONL: one object per tool call carrying ``seq``, ``prev``
(first 12 chars of the previous full hash) and ``h`` where
``h = sha256(prev_full_hash | canonical)`` and ``canonical`` is the record
JSON-serialized with sorted keys and WITHOUT the ``h`` field. The chain
head persists in ``chain.state`` so it continues across restarts; a run
that starts at ``seq=1`` re-anchors at GENESIS (fresh data dir).

Any edited or deleted record breaks every hash after it within its run,
and a truncated tail is caught by the ``chain.state`` cross-check.

Usage:
    python scripts/verify_audit_chain.py [DATA_DIR/audit]

Exit codes: 0 = chain intact, 1 = break detected, 2 = usage/IO error.
"""
import hashlib
import json
import sys
from pathlib import Path


def verify(audit_dir: Path) -> int:
    files = sorted(audit_dir.glob("audit-*.jsonl"))
    if not files:
        print(f"no audit-*.jsonl files under {audit_dir}")
        return 2
    prev = "GENESIS"
    checked = 0
    breaks = 0
    last_rec = None
    for path in files:
        for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                print(f"BREAK {path.name}:{lineno}: not valid JSON")
                breaks += 1
                continue
            if rec.get("seq") == 1:
                prev = "GENESIS"  # a fresh chain (pre-persistence data dir)
            h = rec.pop("h", None)
            canonical = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            expected = hashlib.sha256(f"{prev}|{canonical}".encode()).hexdigest()
            if h != expected:
                print(f"BREAK {path.name}:{lineno}: hash mismatch (seq={rec.get('seq')})")
                breaks += 1
            if rec.get("prev") != prev[:12]:
                print(f"BREAK {path.name}:{lineno}: prev pointer mismatch "
                      f"(seq={rec.get('seq')})")
                breaks += 1
            # Re-anchor on the recorded hash so one break doesn't cascade.
            prev = h if isinstance(h, str) else prev
            checked += 1
            last_rec = rec
    state_path = audit_dir / "chain.state"
    if state_path.exists() and last_rec is not None:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"BREAK chain.state unreadable: {exc}")
            breaks += 1
        else:
            if state.get("prev") != prev:
                print("BREAK chain.state head does not match the last record "
                      "(truncated tail?)")
                breaks += 1
            if state.get("seq") != last_rec.get("seq"):
                print(f"BREAK chain.state seq={state.get('seq')} but last "
                      f"record seq={last_rec.get('seq')}")
                breaks += 1
    print(f"checked {checked} chained record(s); {breaks} break(s)")
    return 1 if breaks else 0


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/audit")
    if not target.is_dir():
        print(f"no such directory: {target}")
        return 2
    return verify(target)


if __name__ == "__main__":
    sys.exit(main())
