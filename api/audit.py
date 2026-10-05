"""Audit log + idempotent outbox, shared by n8n workflows.
Standalone on purpose: a JSON-lines file, independent of whichever case database (SQLite/Postgres) is in use.
   POST /admin/audit   {type, case_id?, key?, detail?}  -> {duplicate: bool, entry}
   GET  /admin/audit   ?type=...                        -> newest first
   DELETE /admin/audit                                  -> clear (demo reset)
If `key` is given (or derived for supplier emails), a second POST with the same key is NOT recorded again
and returns duplicate=true, so n8n can stop before sending twice (replay / retry protection)."""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

PATH = Path(os.getenv("AUDIT_PATH", Path(__file__).resolve().parent.parent / "audit.jsonl"))
_lock = threading.Lock()
router = APIRouter()


class AuditIn(BaseModel):
    type: str
    case_id: Optional[str] = None
    key: Optional[str] = None
    detail: Optional[dict] = None


def _read():
    if not PATH.exists():
        return []
    return [json.loads(line) for line in PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def derive_key(a: AuditIn) -> Optional[str]:
    if a.key:
        return a.key
    if a.type in ("supplier_email", "supplier_email_sent"):   # same case + same text = same email
        d = a.detail or {}
        raw = f"{a.case_id}|{d.get('subject', '')}|{d.get('body', '')}"
        return "send:" + hashlib.sha1(raw.encode()).hexdigest()[:16]
    return None


@router.post("/admin/audit")
def add(a: AuditIn):
    key = derive_key(a)
    with _lock:
        rows = _read()
        if key and any(r.get("key") == key and not r.get("duplicate") for r in rows):
            entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "type": a.type + "_duplicate_blocked",
                     "case_id": a.case_id, "key": key, "duplicate": True, "detail": {}}
            with open(PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return {"duplicate": True, "entry": entry}
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "type": a.type,
                 "case_id": a.case_id, "key": key, "duplicate": False, "detail": a.detail or {}}
        with open(PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"duplicate": False, "entry": entry}


@router.get("/admin/audit")
def list_(type: Optional[str] = None, case_id: Optional[str] = None):
    rows = [r for r in _read() if (not type or r["type"].startswith(type)) and (not case_id or r.get("case_id") == case_id)]
    return rows[::-1]


@router.delete("/admin/audit")
def clear():
    with _lock:
        if PATH.exists():
            PATH.unlink()
    return {"ok": True}
