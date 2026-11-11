# //// Neoffice — added file (no upstream equivalent)
"""An empty Twilio webhook signature check becomes Enforce.

The check of TP Twilio Settings read an empty value as Observe: a webhook whose signature does not verify was
served and logged. No instance takes real calls through Twilio, so the default is now to refuse
(neoffice-maintenance#893). A Single keeps the values it had when a field's default changes, so this writes
Enforce where the value is empty; a value someone chose (Observe, Off, Enforce) is kept, and the patch may run
again.
"""

import frappe

DOCTYPE = "TP Twilio Settings"
FIELD = "webhook_signature_check"


def execute():
    if not frappe.db.exists("DocType", DOCTYPE):
        return
    if frappe.db.get_single_value(DOCTYPE, FIELD):
        return
    frappe.db.set_single_value(DOCTYPE, FIELD, "Enforce")
    frappe.clear_document_cache(DOCTYPE, DOCTYPE)
