"""Exchange Inbox rules (mail filters) — EWS's GetInboxRules/UpdateInboxRules
operations, which exchangelib 5.0.3 does not implement (verified: no
get_inbox_rules.py/update_inbox_rules.py under exchangelib/services/, and
no Rule type anywhere in the package). Built directly on exchangelib's
EWSAccountService/create_element/set_xml_value primitives, following
GetUserOofSettings/SetUserOofSettings (exchangelib/services/) as the
closest shipped precedent for an account-level get/set operation.

Schema verified against Microsoft's own EWS reference (GetInboxRules,
UpdateInboxRules, Conditions, Actions, FromAddresses pages) — this is a
CURATED SUBSET of the full RulePredicates (30 possible conditions) /
RuleActionsType (13 actions) schema: the common "move/forward/delete
based on sender/subject" cases, not a raw passthrough (DESIGN.md's
token-economy / curated-surface philosophy). ``unsupported_fields`` on a
parsed rule flags when a rule uses conditions/actions outside this
subset, so a complex existing rule is never silently misrepresented.

Element order within Conditions/Actions follows the EWS schema's own
sequence (verified against the MSDN Conditions/Actions pages) — EWS
validates element order, not just presence.
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

from exchangelib.services.common import EWSAccountService
from exchangelib.util import MNS, TNS, add_xml_child, create_element

# --- low-level EWS services --------------------------------------------------


class GetInboxRules(EWSAccountService):
    """MSDN: getinboxrules-operation

    GetInboxRulesResponse carries ResponseClass directly on itself (no
    nested ResponseMessages/ResponseMessage wrapper — verified against the
    MSDN example), so the base EWSService machinery's "not delivered in a
    list" fallback already does the right thing; no _get_element_container
    override needed here (contrast GetUserOofSettings, whose response DOES
    nest a ResponseMessage and needs one).
    """

    SERVICE_NAME = "GetInboxRules"
    element_container_name = f"{{{MNS}}}InboxRules"

    def call(self) -> List[Any]:
        return list(self._elems_to_objs(self._get_elements(payload=self.get_payload())))

    def _elem_to_obj(self, elem):
        return elem  # raw <t:Rule> element — parsed by parse_rule_element()

    @classmethod
    def _get_elements_in_container(cls, container):
        return container.findall(f"{{{TNS}}}Rule")

    def get_payload(self):
        return create_element(f"m:{self.SERVICE_NAME}")


class UpdateInboxRules(EWSAccountService):
    """MSDN: updateinboxrules-operation

    One operation per call (create XOR set XOR delete) — this codebase
    never batches rule mutations, so partial-failure (``RuleOperationErrors``,
    only relevant when a single request carries multiple operations) is
    out of scope: a single operation's response is either whole-success or
    a raised EWS error (handled generically by the base class, same as
    every other service).
    """

    SERVICE_NAME = "UpdateInboxRules"
    returns_elements = False

    def call(self, operation: Any) -> bool:
        list(self._get_elements(payload=self.get_payload(operation)))
        return True

    def get_payload(self, operation: Any):
        payload = create_element(f"m:{self.SERVICE_NAME}")
        operations = create_element("m:Operations")
        operations.append(operation)
        payload.append(operations)
        return payload


# --- curated condition/action schema (key, XML tag, kind), IN EWS ORDER -----

_IMPORTANCE_TO_XML = {"low": "Low", "normal": "Normal", "high": "High"}
_IMPORTANCE_FROM_XML = {v: k for k, v in _IMPORTANCE_TO_XML.items()}

# Order matches the Conditions page's schema block exactly.
_CONDITION_FIELDS: List[Tuple[str, str, str]] = [
    ("body_contains", "ContainsBodyStrings", "strings"),
    ("recipient_contains", "ContainsRecipientStrings", "strings"),
    ("sender_contains", "ContainsSenderStrings", "strings"),
    ("subject_contains", "ContainsSubjectStrings", "strings"),
    ("from_addresses", "FromAddresses", "addresses"),
    ("has_attachments", "HasAttachments", "flag"),
    ("importance", "Importance", "value"),
    ("sent_to_addresses", "SentToAddresses", "addresses"),
]

# Order matches the Actions page's schema block exactly.
_ACTION_FIELDS: List[Tuple[str, str, str]] = [
    ("copy_to_folder", "CopyToFolder", "folder"),
    ("delete", "Delete", "flag"),
    ("forward_to", "ForwardToRecipients", "addresses"),
    ("mark_importance", "MarkImportance", "value"),
    ("mark_as_read", "MarkAsRead", "flag"),
    ("move_to_folder", "MoveToFolder", "folder"),
    ("permanent_delete", "PermanentDelete", "flag"),
    ("redirect_to", "RedirectToRecipients", "addresses"),
    ("stop_processing", "StopProcessingRules", "flag"),
]

_KNOWN_CONDITION_TAGS = {tag for _, tag, _ in _CONDITION_FIELDS}
_KNOWN_ACTION_TAGS = {tag for _, tag, _ in _ACTION_FIELDS}


# --- building (dict -> XML) --------------------------------------------------


def _string_list_element(tag: str, values: List[str]):
    elem = create_element(f"t:{tag}")
    for v in values:
        add_xml_child(elem, "t:String", v)
    return elem


def _address_list_element(tag: str, emails: List[str]):
    elem = create_element(f"t:{tag}")
    for email in emails:
        addr = create_element("t:Address")
        add_xml_child(addr, "t:EmailAddress", email)
        elem.append(addr)
    return elem


def _folder_id_element(folder: Any):
    attrs = {"Id": str(folder.id)}
    changekey = getattr(folder, "changekey", None)
    if changekey:
        attrs["ChangeKey"] = str(changekey)
    return create_element("t:FolderId", attrs=attrs)


def _build_predicate_block(tag: str, fields: List[Tuple[str, str, str]],
                           values: Dict[str, Any],
                           resolve_folder: Optional[Callable[[str], Any]]):
    elem = create_element(f"t:{tag}")
    for key, xml_tag, kind in fields:
        value = values.get(key)
        if not value:
            continue
        if kind == "flag":
            elem.append(create_element(f"t:{xml_tag}"))
        elif kind == "value":
            sub = create_element(f"t:{xml_tag}")
            sub.text = _IMPORTANCE_TO_XML.get(value, str(value))
            elem.append(sub)
        elif kind == "strings":
            elem.append(_string_list_element(xml_tag, value))
        elif kind == "addresses":
            elem.append(_address_list_element(xml_tag, value))
        elif kind == "folder":
            if resolve_folder is None:
                raise ValueError(f"{key} given but no folder resolver supplied")
            wrapper = create_element(f"t:{xml_tag}")
            wrapper.append(_folder_id_element(resolve_folder(value)))
            elem.append(wrapper)
    return elem


def build_conditions_element(conditions: Dict[str, Any]):
    return _build_predicate_block("Conditions", _CONDITION_FIELDS, conditions or {}, None)


def build_actions_element(actions: Dict[str, Any], resolve_folder: Callable[[str], Any]):
    return _build_predicate_block("Actions", _ACTION_FIELDS, actions or {}, resolve_folder)


def build_rule_element(*, rule_id: Optional[str], display_name: str, priority: int,
                       is_enabled: bool, conditions: Dict[str, Any], actions: Dict[str, Any],
                       resolve_folder: Callable[[str], Any]):
    elem = create_element("t:Rule")
    if rule_id:
        add_xml_child(elem, "t:RuleId", rule_id)
    add_xml_child(elem, "t:DisplayName", display_name)
    add_xml_child(elem, "t:Priority", int(priority))
    add_xml_child(elem, "t:IsEnabled", bool(is_enabled))
    elem.append(build_conditions_element(conditions))
    elem.append(build_actions_element(actions, resolve_folder))
    return elem


def build_create_operation(rule_elem: Any):
    op = create_element("t:CreateRuleOperation")
    op.append(rule_elem)
    return op


def build_set_operation(rule_elem: Any):
    op = create_element("t:SetRuleOperation")
    op.append(rule_elem)
    return op


def build_delete_operation(rule_id: str):
    op = create_element("t:DeleteRuleOperation")
    add_xml_child(op, "t:RuleId", rule_id)
    return op


# --- parsing (XML -> dict) --------------------------------------------------


def _parse_predicate_block(block_elem: Any, fields: List[Tuple[str, str, str]],
                           known_tags: set) -> Tuple[Dict[str, Any], List[str]]:
    result: Dict[str, Any] = {}
    unsupported: List[str] = []
    if block_elem is None:
        return result, unsupported
    for key, xml_tag, kind in fields:
        child = block_elem.find(f"{{{TNS}}}{xml_tag}")
        if child is None:
            continue
        if kind == "flag":
            result[key] = True
        elif kind == "value":
            result[key] = _IMPORTANCE_FROM_XML.get(child.text, (child.text or "").lower())
        elif kind == "strings":
            result[key] = [s.text for s in child.findall(f"{{{TNS}}}String") if s.text]
        elif kind == "addresses":
            emails = [a.findtext(f"{{{TNS}}}EmailAddress") for a in child.findall(f"{{{TNS}}}Address")]
            result[key] = [e for e in emails if e]
        elif kind == "folder":
            fid = child.find(f"{{{TNS}}}FolderId")
            result[key] = fid.get("Id") if fid is not None else None
    for child in block_elem:
        local = child.tag.split("}", 1)[-1]
        if local not in known_tags:
            unsupported.append(local)
    return result, unsupported


def parse_rule_element(elem: Any) -> Dict[str, Any]:
    """``elem`` is a raw ``<t:Rule>`` element from GetInboxRules. Folder ids
    in the result (``move_to_folder``/``copy_to_folder``) are RAW EWS ids —
    the caller (tools/rules.py) aliases them, matching every other DTO in
    this codebase (DESIGN.md §Ids)."""

    def text(tag: str) -> Optional[str]:
        child = elem.find(f"{{{TNS}}}{tag}")
        return child.text if child is not None else None

    conditions, cond_unsupported = _parse_predicate_block(
        elem.find(f"{{{TNS}}}Conditions"), _CONDITION_FIELDS, _KNOWN_CONDITION_TAGS)
    actions, act_unsupported = _parse_predicate_block(
        elem.find(f"{{{TNS}}}Actions"), _ACTION_FIELDS, _KNOWN_ACTION_TAGS)
    unsupported = cond_unsupported + act_unsupported
    priority_text = text("Priority")

    return {
        "rule_id": text("RuleId"),
        "display_name": text("DisplayName") or "",
        "priority": int(priority_text) if priority_text and priority_text.isdigit() else None,
        "is_enabled": (text("IsEnabled") or "").strip().lower() in ("true", "1"),
        "conditions": conditions,
        "actions": actions,
        "unsupported_fields": unsupported or None,
    }
