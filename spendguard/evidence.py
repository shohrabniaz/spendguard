"""Append-only, hash-chained JSONL evidence log (tamper-evident)."""
from __future__ import annotations
import hashlib, json, os
from datetime import datetime, timezone

class EvidenceLog:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    def entries(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            return [json.loads(l) for l in f if l.strip()]
    def append(self, record: dict) -> dict:
        prev = self.entries()
        rec = dict(record)
        rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
        rec["prev_hash"] = prev[-1]["hash"] if prev else "0" * 64
        rec["hash"] = hashlib.sha256(json.dumps({k: v for k, v in rec.items() if k != "hash"},
                                                sort_keys=True).encode()).hexdigest()
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
    def verify(self) -> bool:
        prev = "0" * 64
        for r in self.entries():
            body = {k: v for k, v in r.items() if k != "hash"}
            if r["prev_hash"] != prev or hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != r["hash"]:
                return False
            prev = r["hash"]
        return True
