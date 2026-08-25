"""gateway/rules.py: the hand-built GetInboxRules/UpdateInboxRules XML —
exchangelib implements neither operation, so this is the only place the
wire format is exercised. Pure XML build/parse, no network/account needed.
"""

from lxml import etree

from ewsmcp.gateway.rules import (
    build_actions_element,
    build_conditions_element,
    build_create_operation,
    build_delete_operation,
    build_rule_element,
    build_set_operation,
    parse_rule_element,
)

TNS = "http://schemas.microsoft.com/exchange/services/2006/types"


class FakeFolder:
    def __init__(self, id_, changekey=None):
        self.id = id_
        self.changekey = changekey


def _resolve(_ref):
    return FakeFolder("AAMkAGYzZjZm", "AQAAAA==")


# --- building -----------------------------------------------------------------


def test_build_rule_element_matches_msdn_create_example():
    """Cross-checked against learn.microsoft.com's own CreateRule example."""
    elem = build_rule_element(
        rule_id=None, display_name="MoveInterestingToJunk", priority=1,
        is_enabled=True, conditions={"subject_contains": ["Interesting"]},
        actions={"move_to_folder": "junkemail"}, resolve_folder=_resolve,
    )
    assert elem.find(f"{{{TNS}}}RuleId") is None  # never sent on create
    assert elem.find(f"{{{TNS}}}DisplayName").text == "MoveInterestingToJunk"
    assert elem.find(f"{{{TNS}}}Priority").text == "1"
    assert elem.find(f"{{{TNS}}}IsEnabled").text in ("1", "true")
    subj = elem.find(f"{{{TNS}}}Conditions/{{{TNS}}}ContainsSubjectStrings/{{{TNS}}}String")
    assert subj.text == "Interesting"
    folder_id = elem.find(f"{{{TNS}}}Actions/{{{TNS}}}MoveToFolder/{{{TNS}}}FolderId")
    assert folder_id.get("Id") == "AAMkAGYzZjZm"
    assert folder_id.get("ChangeKey") == "AQAAAA=="


def test_build_rule_element_includes_rule_id_when_updating():
    elem = build_rule_element(
        rule_id="Nh8AAAAwW/w=", display_name="x", priority=1, is_enabled=True,
        conditions={}, actions={"stop_processing": True}, resolve_folder=_resolve,
    )
    assert elem[0].tag == f"{{{TNS}}}RuleId"
    assert elem[0].text == "Nh8AAAAwW/w="


def test_condition_element_order_matches_ews_schema():
    """EWS validates element ORDER inside Conditions, not just presence —
    the fields must come out in the schema's own sequence regardless of
    the order given in the input dict."""
    conditions = {
        "sent_to_addresses": ["b@x.com"],
        "importance": "high",
        "has_attachments": True,
        "from_addresses": ["a@x.com"],
        "subject_contains": ["hi"],
        "body_contains": ["hello"],
        "sender_contains": ["bmw."],
        "recipient_contains": ["bmw."],
    }
    elem = build_conditions_element(conditions)
    tags = [child.tag.split("}", 1)[-1] for child in elem]
    assert tags == [
        "ContainsBodyStrings", "ContainsRecipientStrings", "ContainsSenderStrings",
        "ContainsSubjectStrings", "FromAddresses",
        "HasAttachments", "Importance", "SentToAddresses",
    ]


def test_action_element_order_matches_ews_schema():
    actions = {
        "stop_processing": True,
        "redirect_to": ["c@x.com"],
        "permanent_delete": True,
        "move_to_folder": "f1",
        "mark_as_read": True,
        "mark_importance": "low",
        "forward_to": ["d@x.com"],
        "delete": True,
        "copy_to_folder": "f2",
    }
    elem = build_actions_element(actions, _resolve)
    tags = [child.tag.split("}", 1)[-1] for child in elem]
    assert tags == [
        "CopyToFolder", "Delete", "ForwardToRecipients", "MarkImportance",
        "MarkAsRead", "MoveToFolder", "PermanentDelete", "RedirectToRecipients",
        "StopProcessingRules",
    ]


def test_flag_actions_are_presence_markers_not_valued():
    elem = build_actions_element({"delete": True}, _resolve)
    delete_elem = elem.find(f"{{{TNS}}}Delete")
    assert delete_elem is not None
    assert list(delete_elem) == []
    assert not (delete_elem.text or "").strip()


def test_false_flags_are_omitted_entirely():
    elem = build_actions_element({"delete": False, "mark_as_read": True}, _resolve)
    assert elem.find(f"{{{TNS}}}Delete") is None
    assert elem.find(f"{{{TNS}}}MarkAsRead") is not None


def test_address_list_uses_address_wrapper_not_mailbox():
    elem = build_actions_element({"forward_to": ["a@x.com", "b@x.com"]}, _resolve)
    addrs = elem.findall(f"{{{TNS}}}ForwardToRecipients/{{{TNS}}}Address")
    assert [a.findtext(f"{{{TNS}}}EmailAddress") for a in addrs] == ["a@x.com", "b@x.com"]


def test_create_and_set_and_delete_operations_wrap_correctly():
    rule = build_rule_element(rule_id=None, display_name="x", priority=1, is_enabled=True,
                              conditions={}, actions={"stop_processing": True},
                              resolve_folder=_resolve)
    create_op = build_create_operation(rule)
    assert create_op.tag == f"{{{TNS}}}CreateRuleOperation"
    assert create_op[0] is rule

    set_op = build_set_operation(rule)
    assert set_op.tag == f"{{{TNS}}}SetRuleOperation"

    delete_op = build_delete_operation("abc123")
    assert delete_op.tag == f"{{{TNS}}}DeleteRuleOperation"
    assert delete_op.find(f"{{{TNS}}}RuleId").text == "abc123"


# --- parsing -------------------------------------------------------------------

_MSDN_GET_INBOX_RULES_EXAMPLE = f"""<Rule xmlns="{TNS}">
  <RuleId>dCsAAABjzvA=</RuleId>
  <DisplayName>MoveInterestingToJunk</DisplayName>
  <Priority>1</Priority>
  <IsEnabled>true</IsEnabled>
  <Conditions>
    <ContainsSubjectStrings>
      <String>Interesting</String>
    </ContainsSubjectStrings>
  </Conditions>
  <Actions>
    <MoveToFolder>
      <FolderId ChangeKey="AQAAAA==" Id="AAMkAGYzZjZm" />
    </MoveToFolder>
  </Actions>
</Rule>"""


def test_parse_rule_element_matches_msdn_response_example():
    elem = etree.fromstring(_MSDN_GET_INBOX_RULES_EXAMPLE.encode())
    parsed = parse_rule_element(elem)
    assert parsed == {
        "rule_id": "dCsAAABjzvA=",
        "display_name": "MoveInterestingToJunk",
        "priority": 1,
        "is_enabled": True,
        "conditions": {"subject_contains": ["Interesting"]},
        "actions": {"move_to_folder": "AAMkAGYzZjZm"},
        "unsupported_fields": None,
    }


def test_parse_round_trips_through_build():
    built = build_rule_element(
        rule_id="R1", display_name="Test Rule", priority=3, is_enabled=False,
        conditions={"from_addresses": ["a@x.com"], "has_attachments": True},
        actions={"forward_to": ["b@x.com"], "delete": True},
        resolve_folder=_resolve,
    )
    parsed = parse_rule_element(built)
    assert parsed["rule_id"] == "R1"
    assert parsed["display_name"] == "Test Rule"
    assert parsed["priority"] == 3
    assert parsed["is_enabled"] is False
    assert parsed["conditions"] == {"from_addresses": ["a@x.com"], "has_attachments": True}
    assert parsed["actions"] == {"forward_to": ["b@x.com"], "delete": True}
    assert parsed["unsupported_fields"] is None


def test_parse_owa_style_recipient_contains_rule():
    """Regression: a real-world OWA-built rule ("contains these words in
    the recipient address" -> move to folder, stop processing) used to
    show up entirely conditionless with ContainsRecipientStrings flagged
    unsupported -- verified live against an actual "BMW OUT" rule."""
    xml = f"""<Rule xmlns="{TNS}">
      <RuleId>owa1</RuleId>
      <DisplayName>BMW OUT</DisplayName>
      <Priority>11</Priority>
      <IsEnabled>true</IsEnabled>
      <Conditions>
        <ContainsRecipientStrings><String>bmw.</String></ContainsRecipientStrings>
      </Conditions>
      <Actions>
        <MoveToFolder><FolderId Id="RAW-BMW-FOLDER"/></MoveToFolder>
        <StopProcessingRules/>
      </Actions>
    </Rule>"""
    parsed = parse_rule_element(etree.fromstring(xml.encode()))
    assert parsed["conditions"] == {"recipient_contains": ["bmw."]}
    assert parsed["actions"] == {"move_to_folder": "RAW-BMW-FOLDER", "stop_processing": True}
    assert parsed["unsupported_fields"] is None


def test_build_sender_and_recipient_contains():
    elem = build_conditions_element({"sender_contains": ["bmw."], "recipient_contains": ["mercedes."]})
    assert elem.findtext(f"{{{TNS}}}ContainsSenderStrings/{{{TNS}}}String") == "bmw."
    assert elem.findtext(f"{{{TNS}}}ContainsRecipientStrings/{{{TNS}}}String") == "mercedes."


def test_unsupported_conditions_are_flagged_not_dropped_silently():
    xml = f"""<Rule xmlns="{TNS}">
      <RuleId>abc</RuleId>
      <DisplayName>Weird</DisplayName>
      <Priority>2</Priority>
      <IsEnabled>false</IsEnabled>
      <Conditions>
        <IsMeetingRequest/>
        <FromAddresses><Address><EmailAddress>a@b.com</EmailAddress></Address></FromAddresses>
      </Conditions>
      <Actions>
        <StopProcessingRules/>
        <SendSMSAlertToRecipients><MobilePhone><EmailAddress>x</EmailAddress></MobilePhone></SendSMSAlertToRecipients>
      </Actions>
    </Rule>"""
    parsed = parse_rule_element(etree.fromstring(xml.encode()))
    assert parsed["conditions"] == {"from_addresses": ["a@b.com"]}
    assert parsed["actions"] == {"stop_processing": True}
    assert sorted(parsed["unsupported_fields"]) == ["IsMeetingRequest", "SendSMSAlertToRecipients"]


def test_parse_empty_conditions_and_actions():
    xml = f"""<Rule xmlns="{TNS}">
      <RuleId>x</RuleId>
      <DisplayName>Empty</DisplayName>
      <Priority>1</Priority>
      <IsEnabled>true</IsEnabled>
      <Conditions/>
      <Actions><StopProcessingRules/></Actions>
    </Rule>"""
    parsed = parse_rule_element(etree.fromstring(xml.encode()))
    assert parsed["conditions"] == {}
    assert parsed["unsupported_fields"] is None
