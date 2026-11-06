import frappe

# //// Neoffice — the web methods below answer the staff who handle calls, not every signed-in account
# (neoffice-maintenance#2296): telephony/access.py says who that is.
from telephony.access import is_telephony_user
from telephony.utils import (
    _get_contact_by_phone_number,
    link_call_with_contact,
    parse_call_log,
)


@frappe.whitelist()
def is_call_integration_enabled():
    twilio_enabled = frappe.db.get_single_value("TP Twilio Settings", "enabled")
    exotel_enabled = frappe.db.get_single_value("TP Exotel Settings", "enabled")

    return {
        "twilio_enabled": twilio_enabled,
        "exotel_enabled": exotel_enabled,
        "default_calling_medium": get_user_default_calling_medium(),
    }


@frappe.whitelist()
def set_default_calling_medium(medium):
    if not frappe.db.exists("TP Telephony Agent", frappe.session.user):
        frappe.get_doc(
            {
                "doctype": "TP Telephony Agent",
                "user": frappe.session.user,
                "default_medium": medium,
            }
        ).insert(ignore_permissions=True)
    else:
        frappe.db.set_value(
            "TP Telephony Agent", frappe.session.user, "default_medium", medium
        )

    return get_user_default_calling_medium()


# //// Neoffice — upstream answered any signed-in account, a portal customer included, from a query that reads every
# Contact with no permission: a reverse lookup from a number to a person, and a way to list the contacts by
# pattern (neoffice-maintenance#2296). A staff member who handles calls still sees who is calling; anyone else
# sees a contact only when they may read it.
# permission-check: ok staff who take calls, or the read right on the Contact found
@frappe.whitelist()
def get_contact_by_phone_number(phone_number: str):  # //// Neoffice — `phone_number` is text, see above
    """Get contact by phone number."""
    contact = _get_contact_by_phone_number(phone_number)
    if (
        contact.get("name")
        and not is_telephony_user()
        and not frappe.has_permission("Contact", "read", contact.get("name"))
    ):
        return {"mobile_no": phone_number}
    return contact


def get_user_default_calling_medium():
    if not frappe.db.exists("TP Telephony Agent", frappe.session.user):
        return None

    default_medium = frappe.db.get_value(
        "TP Telephony Agent", frappe.session.user, "default_medium"
    )

    if not default_medium:
        return None

    return default_medium


@frappe.whitelist()
def create_call_log(
    id,
    telephony_medium,
    from_number,
    to_number,
    duration,
    status,
    call_type,
    caller,
    receiver,
    links,
):
    # //// Neoffice — the insert below skips the permissions, so any signed-in account wrote call records in the
    # name of another user (neoffice-maintenance#2296): it takes the right to create a TP Call Log, as the
    # REST API asks for the same record.
    frappe.has_permission("TP Call Log", "create", throw=True)
    call_log = frappe.get_doc(
        {
            "doctype": "TP Call Log",
            "id": id,
            "to": to_number,
            "type": call_type,
            "status": status,
            "telephony_medium": telephony_medium,
            "from": from_number,
            "duration": duration,
            "links": links,
        }
    ).insert(ignore_permissions=True)

    if call_type == "Incoming":
        call_log.receiver = receiver
    else:
        call_log.caller = caller

    contact_number = from_number if call_type == "Incoming" else to_number
    link_call_with_contact(contact_number, call_log)

    call_log.save(ignore_permissions=True)

    return call_log


# //// Neoffice — upstream returned any call record to any signed-in account, and an untyped `name` sent as a JSON
# filter picked records without knowing their id (neoffice-maintenance#2296). `name` is text now, and the
# record goes to whoever may read TP Call Log, to the user who placed or took the call, and to whoever may read
# a document the call was filed on (the ticket view of helpdesk opens the calls of the ticket this way).
# permission-check: ok _check_call_log_access refuses whoever has no part in the call
@frappe.whitelist()
def get_call_log(name: str):  # //// Neoffice — `name` is text, see above
    call_doc = frappe.get_cached_doc(  # //// Neoffice — the document, checked before its dict goes out
        "TP Call Log",
        name,
        fields=[
            "name",
            "caller",
            "receiver",
            "duration",
            "type",
            "status",
            "from",
            "to",
            "recording_url",
            "creation",
        ],
    )
    _check_call_log_access(call_doc)  # //// Neoffice — see above
    call = call_doc.as_dict()

    call = parse_call_log(call)
    return call


# //// Neoffice — see get_call_log.
def _check_call_log_access(call_doc) -> None:
    user = frappe.session.user
    if frappe.has_permission("TP Call Log", "read", call_doc):
        return
    if user in (call_doc.caller, call_doc.receiver):
        return
    # The Contact link is set from the phone number alone: reading that contact does not make one a party to the
    # call. A link someone chose (the ticket the call was logged on) does.
    for link in call_doc.get("links") or []:
        if not (link.link_doctype and link.link_name) or link.link_doctype == "Contact":
            continue
        if not frappe.db.exists(link.link_doctype, link.link_name):
            continue
        if frappe.has_permission(link.link_doctype, "read", link.link_name):
            return
    frappe.throw(frappe._("Not permitted"), frappe.PermissionError)


@frappe.whitelist()
def create_telephony_agent():
    if not frappe.db.exists("TP Telephony Agent", {"user": frappe.session.user}):
        agent = frappe.get_doc(
            {
                "doctype": "TP Telephony Agent",
                "user": frappe.session.user,
            }
        ).insert(ignore_permissions=True)
    else:
        agent = frappe.db.get_value("TP Telephony Agent", {"user": frappe.session.user})

    return agent
