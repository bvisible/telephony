# //// Neoffice — added file (no upstream equivalent)
"""The secrets of the telephony settings live in __Auth, never in clear in tabSingles or in a Version.

A Password field keeps its value encrypted in __Auth and a mask ("*****") in tabSingles. A value written with
frappe.db.set_single_value skips that step: TP Twilio Settings.set_api_credentials wrote the API secret so, the
Twilio sync of neoffice_theme wrote the auth token and the API secret so, and the Exotel webhook token was a Data
field. tabSingles is what frappe.client.get_single_value returns to every account that may read the settings, and
the Version of a save keeps both sides of a change (neoffice-maintenance#2296).

This moves each clear value of a Password field into __Auth, leaves the mask in its place, and masks the
Versions. A value already masked is left as it is, so the patch may run again.
"""

import json

import frappe
from frappe.utils.password import set_encrypted_password

SETTINGS = ("TP Twilio Settings", "TP Exotel Settings")


def execute():
    for doctype in SETTINGS:
        if not frappe.db.exists("DocType", doctype):
            continue
        fields = password_fields(doctype)
        for field in fields:
            encrypt_clear_value(doctype, field)
        mask_versions(doctype, fields)
        frappe.clear_document_cache(doctype, doctype)


def password_fields(doctype: str) -> list[str]:
    return [df.fieldname for df in frappe.get_meta(doctype).fields if df.fieldtype == "Password"]


def is_mask(value) -> bool:
    return bool(value) and set(str(value)) == {"*"}


def mask(value) -> str:
    return "*" * len(str(value))


def encrypt_clear_value(doctype: str, field: str) -> None:
    singles = frappe.qb.DocType("Singles")
    where = (singles.doctype == doctype) & (singles.field == field)
    rows = frappe.qb.from_(singles).select(singles.value).where(where).run()
    value = rows[0][0] if rows else None
    if not value or is_mask(value):
        return
    set_encrypted_password(doctype, doctype, value, field)
    frappe.qb.update(singles).set(singles.value, mask(value)).where(where).run()


def mask_versions(doctype: str, fields: list[str]) -> None:
    for version in frappe.get_all("Version", filters={"ref_doctype": doctype}, fields=["name", "data"]):
        try:
            data = json.loads(version.data or "{}")
        except ValueError:
            continue
        changed = False
        for row in data.get("changed") or []:
            if len(row) < 3 or row[0] not in fields:
                continue
            for side in (1, 2):
                if row[side] and not is_mask(row[side]):
                    row[side] = mask(row[side])
                    changed = True
        if changed:
            frappe.db.set_value("Version", version.name, "data", frappe.as_json(data), update_modified=False)
