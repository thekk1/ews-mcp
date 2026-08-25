"""Tool pack: rules — Inbox rules (mail filters): list/create/update/delete.

Built on EWS's GetInboxRules/UpdateInboxRules operations, which
exchangelib does not implement — see gateway/rules.py for the low-level
services and the curated condition/action subset they support.

Safety follows the create_event/update_event precedent in writes.py
exactly: these tools are class "write" (draft tier) so a plain
move-to-folder rule stays usable without the full tier, but forward_to/
redirect_to leave the mailbox on every future match — a standing
auto-send order, not a one-off. The dispatcher's kill-switch only covers
class "send", so _check_rule_send_kill_switch re-checks SEND_ENABLED
itself when (and only when) forward_to/redirect_to is set, and confirm
is gated the same way (plus delete/permanent_delete, which are
irreversible). update_rule replaces the rule WHOLESALE (matching EWS's
own SetRuleOperation semantics) — it is deliberately not a partial patch.
"""

from typing import Any, Dict, List, Tuple

from ..errors import ToolError
from ..dto import envelope
from ..gateway.rules import (
    GetInboxRules,
    UpdateInboxRules,
    build_create_operation,
    build_delete_operation,
    build_rule_element,
    build_set_operation,
    parse_rule_element,
)
from .base import Context, ToolSpec

_CONDITION_KEYS = ("subject_contains", "body_contains", "from_addresses",
                   "sent_to_addresses", "has_attachments", "importance")
_ACTION_KEYS = ("move_to_folder", "copy_to_folder", "forward_to", "redirect_to",
                "delete", "permanent_delete", "mark_as_read", "mark_importance",
                "stop_processing")


def _split_rule_kwargs(kw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    conditions = {k: kw[k] for k in _CONDITION_KEYS if kw.get(k) is not None}
    actions = {k: kw[k] for k in _ACTION_KEYS if kw.get(k) is not None}
    return conditions, actions


def _check_rule_send_kill_switch(ctx: Context, actions: Dict[str, Any], tool_name: str) -> None:
    if (actions.get("forward_to") or actions.get("redirect_to")) and not ctx.settings.send_enabled:
        raise ToolError(
            "kill_switch",
            f"{tool_name} with forward_to/redirect_to is blocked: "
            "SEND_ENABLED=false on this server.",
            hint="Use a rule without forward_to/redirect_to, or have the "
                 "operator flip SEND_ENABLED.",
        )


def _rule_confirm_needed(kw: Dict[str, Any]) -> bool:
    return bool(kw.get("forward_to") or kw.get("redirect_to")
                or kw.get("delete") or kw.get("permanent_delete"))


def _alias_rule(ctx: Context, parsed: Dict[str, Any]) -> Dict[str, Any]:
    raw_id = parsed.get("rule_id")
    actions = dict(parsed.get("actions") or {})
    for key in ("move_to_folder", "copy_to_folder"):
        if actions.get(key):
            actions[key] = ctx.aliaser.alias_for(actions[key], "f")
    row: Dict[str, Any] = {
        "id": ctx.aliaser.alias_for(raw_id, "r") if raw_id else None,
        "display_name": parsed.get("display_name"),
        "priority": parsed.get("priority"),
        "is_enabled": parsed.get("is_enabled"),
        "conditions": parsed.get("conditions") or {},
        "actions": actions,
    }
    if parsed.get("unsupported_fields"):
        row["unsupported_fields"] = parsed["unsupported_fields"]
    return row


async def _list_rules(ctx: Context) -> Dict[str, Any]:
    def work(account: Any) -> List[Any]:
        return GetInboxRules(account=account).call()

    raw_rules = await ctx.gateway.call(work)
    rows = [_alias_rule(ctx, parse_rule_element(elem)) for elem in raw_rules]
    return envelope(rows, total_available=len(rows), offset=0)


async def _create_rule(ctx: Context, *, display_name: str, priority: int = 1,
                       is_enabled: bool = True, **kw: Any) -> Dict[str, Any]:
    conditions, actions = _split_rule_kwargs(kw)
    _check_rule_send_kill_switch(ctx, actions, "create_rule")
    if not actions:
        raise ToolError("validation", "at least one action is required",
                        hint="Set e.g. move_to_folder, delete, or forward_to.")

    def work(account: Any) -> Dict[str, Any]:
        def resolve(ref: str) -> Any:
            return ctx.gateway.resolve_folder(account, ref, ctx.aliaser)

        rule_elem = build_rule_element(
            rule_id=None, display_name=display_name, priority=priority,
            is_enabled=is_enabled, conditions=conditions, actions=actions,
            resolve_folder=resolve,
        )
        UpdateInboxRules(account=account).call(build_create_operation(rule_elem))
        # UpdateInboxRules never hands back the new RuleId (verified against
        # MSDN's own response example — ResponseCode only). One GetInboxRules
        # round trip to find it, matched on display_name+priority: unique
        # enough in practice, and a collision merely costs the caller a
        # re-list to disambiguate, nothing worse.
        for elem in GetInboxRules(account=account).call():
            parsed = parse_rule_element(elem)
            if parsed["display_name"] == display_name and parsed["priority"] == priority:
                return parsed
        return {"rule_id": None, "display_name": display_name, "priority": priority,
                "is_enabled": is_enabled, "conditions": conditions, "actions": actions}

    parsed = await ctx.gateway.call(work)
    return _alias_rule(ctx, parsed)


async def _update_rule(ctx: Context, *, rule_id: str, display_name: str,
                       priority: int = 1, is_enabled: bool = True,
                       **kw: Any) -> Dict[str, Any]:
    conditions, actions = _split_rule_kwargs(kw)
    _check_rule_send_kill_switch(ctx, actions, "update_rule")
    if not actions:
        raise ToolError("validation", "at least one action is required",
                        hint="Set e.g. move_to_folder, delete, or forward_to.")

    def work(account: Any) -> Dict[str, Any]:
        def resolve(ref: str) -> Any:
            return ctx.gateway.resolve_folder(account, ref, ctx.aliaser)

        rule_elem = build_rule_element(
            rule_id=rule_id, display_name=display_name, priority=priority,
            is_enabled=is_enabled, conditions=conditions, actions=actions,
            resolve_folder=resolve,
        )
        UpdateInboxRules(account=account).call(build_set_operation(rule_elem))
        # SetRuleOperation echoes nothing back either -- we already know the
        # full state, since update_rule replaced it wholesale.
        return {"rule_id": rule_id, "display_name": display_name, "priority": priority,
                "is_enabled": is_enabled, "conditions": conditions, "actions": actions}

    parsed = await ctx.gateway.call(work)
    return _alias_rule(ctx, parsed)


async def _delete_rule(ctx: Context, *, rule_id: str) -> Dict[str, Any]:
    def work(account: Any) -> bool:
        return UpdateInboxRules(account=account).call(build_delete_operation(rule_id))

    await ctx.gateway.call(work)
    return {"deleted": True}


# --- schemas ------------------------------------------------------------------


def _obj(props: Dict[str, Any], required: List[str] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": False}


_STR = {"type": "string"}
_EMAILS = {"type": "array", "items": {"type": "string"}}
_STRINGS = {"type": "array", "items": {"type": "string"}}
_IMPORTANCE_ENUM = {"type": "string", "enum": ["low", "normal", "high"]}


def _condition_props() -> Dict[str, Any]:
    return {
        "subject_contains": {**_STRINGS, "description": "Subject must contain ANY of these substrings."},
        "body_contains": {**_STRINGS, "description": "Body must contain ANY of these substrings."},
        "from_addresses": {**_EMAILS, "description": "Sender must be one of these addresses."},
        "sent_to_addresses": {**_EMAILS, "description": "A To/Cc recipient must be one of these addresses."},
        "has_attachments": {"type": "boolean", "description": "Message must have an attachment."},
        "importance": {**_IMPORTANCE_ENUM, "description": "Message must be stamped with this importance."},
    }


def _action_props() -> Dict[str, Any]:
    return {
        "move_to_folder": {**_STR, "description": "Move the message here (folder id, f:alias, or path)."},
        "copy_to_folder": {**_STR, "description": "ALSO copy the message here."},
        "forward_to": {**_EMAILS, "description": ("Forward to these addresses. Leaves the mailbox on "
                                                   "every future match — needs SEND_ENABLED=true and "
                                                   "two-phase confirm.")},
        "redirect_to": {**_EMAILS, "description": ("Redirect to these addresses (no copy kept locally, "
                                                    "sender sees no change). Same SEND_ENABLED + confirm "
                                                    "requirement as forward_to.")},
        "delete": {"type": "boolean", "description": "Move to Deleted Items. Needs confirm."},
        "permanent_delete": {"type": "boolean",
                             "description": "Delete with no Deleted Items copy — unrecoverable. Needs confirm."},
        "mark_as_read": {"type": "boolean"},
        "mark_importance": {**_IMPORTANCE_ENUM, "description": "Stamp this importance on the message."},
        "stop_processing": {"type": "boolean", "description": "Don't evaluate rules after this one."},
    }


def _rule_schema(with_rule_id: bool) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    required: List[str] = []
    if with_rule_id:
        props["rule_id"] = _STR
        required.append("rule_id")
    props["display_name"] = _STR
    required.append("display_name")
    props["priority"] = {"type": "integer", "minimum": 1, "default": 1,
                         "description": "Lower runs first. Rules share one priority space per mailbox."}
    props["is_enabled"] = {"type": "boolean", "default": True}
    props.update(_condition_props())
    props.update(_action_props())
    return _obj(props, required=required)


TOOLS: List[ToolSpec] = [
    ToolSpec(
        name="list_rules",
        description=(
            "List Inbox rules (mail filters) with their conditions/actions. "
            "Each row's `id` is a short rule alias (r3), reusable as `rule_id` "
            "in update_rule/delete_rule. `unsupported_fields`, when present, "
            "means this rule also uses conditions/actions outside this tool's "
            "curated subset (30+ possible EWS predicates exist; this surface "
            "covers the common ones) — such a rule is safe to list but "
            "editing it via update_rule would silently drop those fields, "
            "since update_rule replaces the whole rule."
        ),
        side_effect_class="read",
        input_schema=_obj({}),
        handler=_list_rules,
    ),
    ToolSpec(
        name="create_rule",
        description=(
            "Create an Inbox rule (mail filter). Omitting every condition "
            "matches EVERY incoming message — only do that deliberately. At "
            "least one action is required. forward_to/redirect_to leave the "
            "mailbox on every future match (a standing auto-send order, not "
            "a one-off send) — SEND_ENABLED must be true, and confirmation "
            "is required, same as delete/permanent_delete."
        ),
        side_effect_class="write",
        input_schema=_rule_schema(with_rule_id=False),
        handler=_create_rule,
        confirm=_rule_confirm_needed,
    ),
    ToolSpec(
        name="update_rule",
        description=(
            "Replace an EXISTING Inbox rule wholesale (like create_rule, "
            "targeting rule_id from list_rules) — this is NOT a partial "
            "patch: supply the full desired display_name/priority/"
            "is_enabled/conditions/actions, not just the fields you want "
            "changed, or the omitted ones are dropped."
        ),
        side_effect_class="write",
        input_schema=_rule_schema(with_rule_id=True),
        handler=_update_rule,
        confirm=_rule_confirm_needed,
    ),
    ToolSpec(
        name="delete_rule",
        description="Permanently delete an Inbox rule (no undo — recreate with create_rule if needed).",
        side_effect_class="write",
        input_schema=_obj({"rule_id": _STR}, required=["rule_id"]),
        handler=_delete_rule,
        confirm=True,
    ),
]
