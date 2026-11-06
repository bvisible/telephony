# //// Neoffice — added file (no upstream equivalent)
"""A voicemail transcription is text: the markup a stored one holds is escaped.

handle_voicemail_transcription stored the text as it came, and the voicemail form shows the field as HTML; a
request from a signed-in account skipped the sanitizer guests get (neoffice-maintenance#2296). The webhook escapes
it now; this escapes what was stored before. An escaped text holds no "<", so the patch may run again.
"""

import frappe
from frappe.utils import escape_html


def execute():
    if not frappe.db.table_exists("TP Voicemail"):
        return
    rows = frappe.get_all(
        "TP Voicemail", filters={"transcription": ["like", "%<%"]}, fields=["name", "transcription"]
    )
    for row in rows:
        frappe.db.set_value(
            "TP Voicemail", row.name, "transcription", escape_html(row.transcription), update_modified=False
        )
