"""Tool pack: tasks + waiting_on — small data-plane additions (Phase 3).

Deliberately minimal per the MCP-vs-Skill litmus test: planning,
prioritization and commitments live in the calling LLM's skills; the
server only moves task DATA (list, complete, re-date) and answers the
deterministic mirror query "which sent threads never got a reply".
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..dates import parse_when
from ..dto import envelope
from ..errors import ToolError
from .base import Context, ToolSpec

logger = logging.getLogger(__name__)

_TASK_PROJECTION = ("id", "changekey", "subject", "due_date", "is_complete",
                    "status")


def _task_row_dto(ctx: Context, row: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": ctx.aliaser.alias_for(row["ews_id"], "k"),
        "subject": row["subject"] or "",
        "complete": bool(row["is_complete"]),
    }
    if row["due_iso"]:
        out["due"] = row["due_iso"]
    if row["status"]:
        out["status"] = row["status"]
    return out


def _task_item_dto(ctx: Context, item: Any, tz: str) -> Dict[str, Any]:
    from ..dto import fmt_dt
    due = getattr(item, "due_date", None)
    out: Dict[str, Any] = {
        "id": ctx.aliaser.alias_for(str(item.id), "k"),
        "subject": getattr(item, "subject", "") or "",
        "complete": bool(getattr(item, "is_complete", False)),
    }
    if due is not None:
        out["due"] = fmt_dt(due, tz) or (due.isoformat()
                                         if hasattr(due, "isoformat") else str(due))
    status = getattr(item, "status", None)
    if status:
        out["status"] = str(status)
    return out


async def _list_tasks(ctx: Context, include_completed: bool = False,
                      offset: int = 0, limit: int = 25,
                      fresh: bool = False) -> Dict[str, Any]:
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 100))
    if not fresh and ctx.cache is not None and ctx.cache.watermark("item:tasks"):
        try:
            rows, total = await asyncio.to_thread(
                ctx.cache.task_rows, include_completed, offset, limit)
            items = [_task_row_dto(ctx, r) for r in rows]
            out = envelope(items, total, offset)
            out["source"] = "cache"
            return out
        except Exception as exc:
            logger.warning("cache list_tasks failed (%s) — live", exc)

    tz = ctx.settings.ews_tz

    def work(account: Any) -> List[Any]:
        qs = account.tasks.all()
        try:
            qs = qs.only(*_TASK_PROJECTION)
        except Exception:
            pass
        found = []
        for item in qs:
            if not include_completed and getattr(item, "is_complete", False):
                continue
            found.append(item)
        return found

    items = await ctx.gateway.call(work)
    total = len(items)
    page = items[offset:offset + limit]
    out = envelope([_task_item_dto(ctx, it, tz) for it in page], total, offset)
    out["source"] = "live"
    return out


async def _update_task(ctx: Context, id: str, complete: Optional[bool] = None,
                       due: Optional[str] = None) -> Dict[str, Any]:
    # DATA-plane only by design: completion + due date. Renaming,
    # prioritizing and planning live in skills, not here.
    if complete is None and due is None:
        raise ToolError("validation", "pass complete=true and/or due=<date>")
    due_dt = parse_when(due, "due", ctx.settings.ews_tz) if due else None

    def work(account: Any) -> Dict[str, Any]:
        fetched = list(account.fetch(ids=[(id, None)]))
        if not fetched:
            raise ToolError("not_found", "task not found — the id may be stale",
                            hint="Re-run list_tasks for fresh ids.")
        item = fetched[0]
        if isinstance(item, Exception):
            raise item
        changed: List[str] = []
        if due_dt is not None:
            item.due_date = due_dt.date()
            changed.append("due_date")
        if changed:
            item.save(update_fields=changed)
        if complete:
            item.complete()  # 5.0.3: sets status/percent and saves
        return {
            "complete": bool(complete) or bool(getattr(item, "is_complete", False)),
            "due": due_dt.date().isoformat() if due_dt else None,
        }

    result = await ctx.gateway.call(work)
    if ctx.cache is not None:
        try:  # write-through; the slow sync lane repairs anything missed
            row = {"ews_id": id, "changekey": None, "subject": "",
                   "due_ts": int(due_dt.timestamp()) if due_dt else None,
                   "due_iso": due_dt.date().isoformat() if due_dt else None,
                   "is_complete": 1 if result["complete"] else 0,
                   "status": None}
            existing = None
            try:
                with ctx.cache._read() as conn:  # keep subject if we have it
                    existing = conn.execute(
                        "SELECT subject, status, due_ts, due_iso FROM tasks "
                        "WHERE ews_id=?", (id,)).fetchone()
            except Exception:
                existing = None
            if existing is not None:
                row["subject"] = existing["subject"]
                row["status"] = existing["status"]
                if due_dt is None:
                    row["due_ts"] = existing["due_ts"]
                    row["due_iso"] = existing["due_iso"]
                ctx.cache.upsert_tasks([row])
        except Exception as exc:
            logger.warning("task write-through failed (sync repairs): %s", exc)
    return {"ok": True, "task_id": ctx.aliaser.alias_for(id, "k"), **result}


async def _waiting_on(ctx: Context, days: int = 5,
                      limit: int = 25) -> Dict[str, Any]:
    days = max(1, min(int(days), 90))
    limit = max(1, min(int(limit), 50))
    if ctx.cache is None or not ctx.cache.watermark("item:sent"):
        raise ToolError(
            "upstream_unavailable",
            "waiting_on needs the local mirror (sent folder not synced yet)",
            hint="The cache warms up shortly after boot; check "
                 "get_server_status.cache, or enable EWS_CACHE_ENABLED.",
        )
    rows = await asyncio.to_thread(ctx.cache.sent_without_reply, days, limit)

    def build(r: Any) -> Dict[str, Any]:
        import json as _json
        try:
            to = _json.loads(r["to_json"] or "[]")
        except ValueError:
            to = []
        return {
            "id": ctx.aliaser.alias_for(r["ews_id"], "m",
                                        internet_message_id=r["internet_message_id"]),
            "thread": (ctx.aliaser.alias_for(r["conversation_id"], "t")
                       if r["conversation_id"] else None),
            "to": to,
            "subject": r["subject"] or "",
            "sent": r["date_iso"],
            "snippet": (r["body_clean"] or "")[:200],
        }

    items = await asyncio.to_thread(lambda: [build(r) for r in rows])
    out = envelope(items, len(items), 0)
    out["source"] = "cache"
    out["note"] = (f"sent threads with no inbound reply after {days} day(s); "
                   "use get_thread on `thread` for context")
    return out


TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="list_tasks",
        description=(
            "List Exchange tasks (to-dos), incomplete ones by default. "
            "Returns {id, subject, due, complete, status}; ids are short "
            "aliases (k1, k2, …) for update_task. Served from the local "
            "mirror when synced (source=cache), live otherwise."
        ),
        side_effect_class="read",
        input_schema={
            "type": "object",
            "properties": {
                "include_completed": {"type": "boolean", "default": False},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                          "default": 25},
                "fresh": {"type": "boolean", "default": False,
                          "description": "true forces a live Exchange read."},
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=_list_tasks,
    ),
    ToolSpec(
        name="update_task",
        description=(
            "Update ONE task: mark it complete and/or set its due date "
            "('today', '+Nd', YYYY-MM-DD). Nothing else is editable here — "
            "task planning belongs to the calling assistant, not the server."
        ),
        side_effect_class="write",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string",
                       "description": "Task id (k-alias from list_tasks or raw)."},
                "complete": {"type": "boolean"},
                "due": {"type": "string",
                        "description": "New due date: 'today', '+Nd', or "
                                       "YYYY-MM-DD."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        handler=_update_task,
    ),
    ToolSpec(
        name="waiting_on",
        description=(
            "Deterministic mirror query: messages YOU sent whose thread has "
            "received no inbound reply after `days` days — the 'what am I "
            "waiting on?' list. Each row carries the sent message id, the "
            "thread alias, recipients and a snippet. Requires the local "
            "mirror (sent folder synced)."
        ),
        side_effect_class="read",
        requires_ews=False,  # answers purely from the mirror
        input_schema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 90,
                         "default": 5},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                          "default": 25},
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=_waiting_on,
    ),
]
