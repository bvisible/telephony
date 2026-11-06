# //// Neoffice — added file (no upstream equivalent): who may use the telephony web methods.
"""Who may use the telephony web methods (neoffice-maintenance#2296).

`@frappe.whitelist()` opens a method to every signed-in account, a customer of the portal included. The telephony
methods act on calls, on the people who take them and on the account that pays for them, so they answer the staff
who handle calls and refuse everyone else.

A staff member who handles calls holds a telephony role, or a phone line switched on in their TP Telephony Agent
row (`twilio` or `exotel`) or their CRM Telephony Agent row (`twilio`, where the mobile app writes the number a
user picks): that is how the softphone of neoffice_theme decides who gets a phone (`check_softphone_access`). The
line counts on a desk account only: any account may create its own empty agent row (`create_telephony_agent`), but
switching a line on takes a desk account or a manager.
"""

import frappe
from frappe import _

MANAGER_ROLES = ("System Manager", "TP Manager")
AGENT_ROLES = (*MANAGER_ROLES, "TP Agent", "Telephony User")


def is_telephony_manager(user: str | None = None) -> bool:
    """May run the account's maintenance and act on any call."""
    user = user or frappe.session.user
    return user == "Administrator" or bool(set(frappe.get_roles(user)) & set(MANAGER_ROLES))


def is_telephony_user(user: str | None = None) -> bool:
    """A staff member who handles calls: a telephony role, or a line switched on for a desk account."""
    user = user or frappe.session.user
    if not user or user == "Guest":
        return False
    if set(frappe.get_roles(user)) & set(AGENT_ROLES):
        return True
    if frappe.db.get_value("User", user, "user_type") != "System User":
        return False
    if any(frappe.db.exists("TP Telephony Agent", {"user": user, line: 1}) for line in ("twilio", "exotel")):
        return True
    return _has_crm_twilio_line(user)


def _has_crm_twilio_line(user: str) -> bool:
    """A CRM Telephony Agent row with its Twilio line on, as check_softphone_access reads it. The `twilio` column is
    not in every crm schema: without it, no line."""
    return bool(
        frappe.db.exists("DocType", "CRM Telephony Agent")
        and frappe.db.has_column("CRM Telephony Agent", "twilio")
        and frappe.db.exists("CRM Telephony Agent", {"user": user, "twilio": 1})
    )


def check_telephony_user() -> None:
    """Refuse an account that does not handle calls."""
    if not is_telephony_user():
        frappe.throw(_("Not permitted"), frappe.PermissionError)


def check_telephony_manager() -> None:
    """Refuse an account that may not run the telephony account's maintenance."""
    if not is_telephony_manager():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
