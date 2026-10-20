"""Action policy: every tool call is classified before it runs.

Risk levels
  read     no side effects (navigation, reading, typing into a form that isn't submitted)
  auth     signing in (a POST form that contains a password field)
  write    changes state in a business system (submitting a POST form)
  high     money movement, deletion, security-sensitive changes (bank details, approvals)
  blocked  never allowed (hosts outside the allowlist, harness-internal endpoints)

The classification is generic, based on HTML semantics (form method, password fields)
and the wording of the control the agent is about to activate, not on knowledge of the
sandbox apps. The model can also escalate on its own with the request_approval tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HIGH_RISK_WORDS = re.compile(
    r"\b(pay|payment|payments|transfer|wire|remit|refund|delete|remove|purge|bank|iban|ifsc|swift|"
    r"approve|authori[sz]e|terminate|deactivate|revoke|password)\b", re.I)

ORDER = ["read", "auth", "write", "high", "blocked"]


@dataclass
class Assessment:
    risk: str  # read | auth | write | high | blocked
    summary: str  # human-readable description of the effect
    reason: str = ""

    def at_least(self, risk: str) -> bool:
        return ORDER.index(self.risk) >= ORDER.index(risk)


def classify_activation(element: dict) -> Assessment:
    """Classify clicking an element (or pressing Enter in a form). `element` comes from the browser."""
    text = (element.get("text") or "").strip()
    label = f'"{text[:60]}"' if text else element.get("tag", "element")
    form = element.get("form")
    if element.get("tag") == "a" and not form:
        # Following a link is a GET. Its text can mention anything (an email titled "Updated bank
        # details" is still just an email); only a target that looks like an action is risky.
        href = element.get("href") or ""
        if re.search(r"/(delete|remove|pay|payment|transfer|approve)(/|\?|$|-)", href, re.I):
            return Assessment("high", f"follow link {label} -> {href}", "link target looks like a destructive action")
        return Assessment("read", f"follow link {label}")
    if form and element.get("submits"):
        method = (form.get("method") or "get").lower()
        action = form.get("action") or ""
        if method == "get":
            return Assessment("read", f"submit search/filter form {label}")
        if form.get("has_password"):
            return Assessment("auth", f"sign in via form {label} ({action})")
        fields = form.get("fields") or {}
        haystack = " ".join([text, action, " ".join(fields.keys())])
        if HIGH_RISK_WORDS.search(haystack):
            return Assessment("high", f"submit {label} (POST {action})",
                              "the action involves payments, deletion or banking/security data")
        return Assessment("write", f"submit {label} (POST {action})", "creates or changes a business record")
    # A button that runs page JavaScript: effect unknown, so treat it as a write.
    if element.get("tag") in {"button", "input"}:
        if HIGH_RISK_WORDS.search(text):
            return Assessment("high", f"click {label}", "button text suggests a payment/destructive action")
        return Assessment("write", f"click {label}", "button with unknown side effects")
    return Assessment("read", f"click {label}")


def needs_approval(assessment: Assessment, mode: str) -> bool:
    if mode == "all-writes":
        return assessment.at_least("write") and assessment.risk != "blocked"
    return assessment.risk == "high"  # modes: high, never-ask (never-ask converts approval into refusal)


def allowed_in_read_only(assessment: Assessment) -> bool:
    return assessment.risk in {"read", "auth"}
