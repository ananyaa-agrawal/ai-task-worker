from taskworker.policy import Assessment, allowed_in_read_only, classify_activation, needs_approval


def form(method="post", action="/erp/invoices", fields=None, has_password=False):
    return {"method": method, "action": action, "fields": fields or {}, "has_password": has_password}


def test_plain_link_is_read():
    assert classify_activation({"tag": "a", "text": "Invoices", "href": "/erp/invoices"}).risk == "read"


def test_link_text_alone_does_not_make_navigation_risky():
    a = classify_activation({"tag": "a", "text": "URGENT: Updated bank details for all future payments",
                             "href": "http://localhost:8765/mail/m/m10"})
    assert a.risk == "read"
    assert classify_activation({"tag": "a", "text": "Remove", "href": "/erp/invoices/INV-1/delete"}).risk == "high"


def test_get_form_is_read():
    a = classify_activation({"tag": "button", "text": "Search", "submits": True, "form": form("get", "/mail")})
    assert a.risk == "read"


def test_login_form_is_auth_and_allowed_read_only():
    a = classify_activation({"tag": "button", "text": "Sign in", "submits": True,
                             "form": form(action="/erp/login", has_password=True)})
    assert a.risk == "auth" and allowed_in_read_only(a)


def test_record_creation_is_write():
    a = classify_activation({"tag": "button", "text": "Save invoice", "submits": True,
                             "form": form(fields={"amount": "10", "notes": ""})})
    assert a.risk == "write" and not allowed_in_read_only(a)
    assert not needs_approval(a, "high") and needs_approval(a, "all-writes")


def test_payment_delete_and_bank_changes_are_high():
    for text, action, fields in [("Schedule payment", "/erp/invoices/INV-1/schedule-payment", {}),
                                 ("Delete invoice", "/erp/invoices/INV-1/delete", {}),
                                 ("Save", "/erp/vendors/V-1/bank", {"bank_account": "", "ifsc": ""})]:
        a = classify_activation({"tag": "button", "text": text, "submits": True, "form": form(action=action, fields=fields)})
        assert a.risk == "high", text
        assert needs_approval(a, "high")


def test_blocked_never_needs_approval_flow():
    assert not needs_approval(Assessment("blocked", "x"), "all-writes")


def test_nested_tool_input_validation():
    from taskworker.tools.control import control_tools
    finish = next(t for t in control_tools() if t.name == "finish").input_schema
    from taskworker.tools.base import validate_input
    ok = {"status": "completed", "summary": "s", "claims": [{"claim": "c", "evidence": "e"}]}
    assert validate_input(finish, ok) is None
    assert validate_input(finish, ok | {"claims": ["just a string"]}) == "claims[0] must be of type object"
    assert validate_input(finish, ok | {"claims": [{"claim": "c"}]}) == "missing required field 'claims[0].evidence'"
    assert "status" in validate_input(finish, ok | {"status": "done"})
