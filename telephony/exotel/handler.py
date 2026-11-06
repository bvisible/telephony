# //// Neoffice — hmac: the webhook key compared in constant time; quote: the key put in a URL as data.
import hmac
from urllib.parse import quote

import bleach
import frappe
import requests
from frappe import _
from frappe.integrations.utils import create_request_log

# //// Neoffice — who may place a call, and a bound on every request to Exotel (neoffice-maintenance#2296).
from telephony.access import is_telephony_manager
from telephony.utils import link_call_with_contact, link_call_with_doc

EXOTEL_TIMEOUT = 15  # //// Neoffice — seconds: a host that never answered held a web worker for good

# Endpoints for webhook

# Incoming Call:
# <site>/api/method/telephony.exotel.handler.handle_request?key=<exotel-webhook-verify-token>

# Exotel Reference:
# https://developer.exotel.com/api/
# https://support.exotel.com/support/solutions/articles/48283-working-with-passthru-applet


# Incoming Call
# permission-check: ok Exotel webhook (guest): validate_request checks its key
@frappe.whitelist(allow_guest=True)
def handle_request(**kwargs):
    validate_request()
    if not is_integration_enabled():
        return

    request_log = create_request_log(
        kwargs,
        request_description="Exotel Call",
        service_name="Exotel",
        request_headers=frappe.request.headers,
        is_remote_request=1,
    )

    try:
        request_log.status = "Completed"
        exotel_settings = get_exotel_settings()
        if not exotel_settings.enabled:
            return

        call_payload = kwargs

        # //// Neoffice — upstream sent every call, numbers and recording included, to every desk user (no room):
        # it goes to the agents of the call now, and never with the webhook key (neoffice-maintenance#2296).
        _publish_call_event(call_payload)
        status = call_payload.get("Status")
        if status == "free":
            return

        if call_log := get_call_log(call_payload):
            update_call_log(call_payload, call_log=call_log)
        else:
            create_call_log(
                call_id=call_payload.get("CallSid"),
                from_number=call_payload.get("CallFrom"),
                to_number=call_payload.get("DialWhomNumber"),
                medium=call_payload.get("To"),
                status=get_call_log_status(call_payload),
                agent=call_payload.get("AgentEmail"),
            )
    except Exception:
        request_log.status = "Failed"
        request_log.error = frappe.get_traceback()
        frappe.db.rollback()
        frappe.log_error(title="Error while creating/updating call record")
        frappe.db.commit()  # nosemgrep
    finally:
        request_log.save(ignore_permissions=True)
        frappe.db.commit()  # nosemgrep


# Outgoing Call
# //// Neoffice — upstream let any signed-in account place a call billed to the company, between two numbers of
# its choice, and a GET from another site could do it in a desk user's name (neoffice-maintenance#2296). The
# call is placed by a POST, from the caller's own agent line (their mobile number and Exotel number), and
# only a telephony manager may give other numbers; the document a call is filed on is one the caller may read.
@frappe.whitelist(methods=["POST"])
def make_a_call(  # //// Neoffice — the parameters are text, see above
    to_number: str,
    from_number: str | None = None,
    caller_id: str | None = None,
    link_doctype: str | None = None,
    link_docname: str | None = None,
):
    if not is_integration_enabled():
        frappe.throw(
            _("Please setup Exotel intergration"), title=_("Integration Not Enabled")
        )
    _check_outgoing_call(from_number, caller_id, link_doctype, link_docname)  # //// Neoffice — see above

    endpoint = get_exotel_endpoint("Calls/connect.json?details=true")

    if not from_number:
        from_number = frappe.get_value(
            "TP Telephony Agent", {"user": frappe.session.user}, "mobile_no"
        )

    if not caller_id:
        caller_id = frappe.get_value(
            "TP Telephony Agent", {"user": frappe.session.user}, "exotel_number"
        )

    if not caller_id:
        frappe.throw(
            _("You do not have Exotel Number set in your Telephony Agent"),
            title=_("Exotel Number Missing"),
        )

    if caller_id and caller_id not in get_all_exophones():
        frappe.throw(
            _("Exotel Number {0} is not valid").format(caller_id),
            title=_("Invalid Exotel Number"),
        )

    if not from_number:
        frappe.throw(
            _("You do not have mobile number set in your Telephony Agent"),
            title=_("Mobile Number Missing"),
        )

    record_call = frappe.db.get_single_value("TP Exotel Settings", "record_call")

    try:
        response = requests.post(
            endpoint,
            data={
                "From": from_number,
                "To": to_number,
                "CallerId": caller_id,
                "Record": "true" if record_call else "false",
                "StatusCallback": get_status_updater_url(),
                "StatusCallbackEvents[0]": "terminal",
                "StatusCallbackEvents[1]": "answered",
            },
            timeout=EXOTEL_TIMEOUT,  # //// Neoffice — see EXOTEL_TIMEOUT
        )
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        if exc := response.json().get("RestException"):
            frappe.throw(
                bleach.linkify(exc.get("Message")), title=_("Exotel Exception")
            )
    else:
        res = response.json()
        call_payload = res.get("Call", {})

        create_call_log(
            call_id=call_payload.get("Sid"),
            from_number=call_payload.get("From"),
            to_number=call_payload.get("To"),
            medium=call_payload.get("PhoneNumberSid"),
            call_type="Outgoing",
            agent=frappe.session.user,
            link_doc={"doctype": link_doctype, "docname": link_docname},
        )

    call_details = response.json().get("Call", {})
    call_details["CallSid"] = call_details.get("Sid", "")
    return call_details


def get_exotel_endpoint(action=None, version="v1"):
    settings = get_exotel_settings()
    return "https://{api_key}:{api_token}@{subdomain}/{version}/Accounts/{sid}/{action}".format(
        api_key=settings.api_key,
        api_token=settings.get_password("api_token"),
        subdomain=settings.subdomain,
        version=version,
        sid=settings.account_sid,
        action=action,
    )


def get_all_exophones():
    endpoint = get_exotel_endpoint("IncomingPhoneNumbers", "v2_beta")
    response = requests.get(endpoint, timeout=EXOTEL_TIMEOUT)  # //// Neoffice — see EXOTEL_TIMEOUT
    return [
        phone.get("friendly_name")
        for phone in response.json().get("incoming_phone_numbers", [])
    ]


def get_status_updater_url():
    from frappe.utils.data import get_url

    # //// Neoffice — the token is a Password field now (patch encrypt_telephony_settings_secrets): read through
    # get_password, which also returns a value the patch has not moved yet.
    webhook_verify_token = get_webhook_verify_token()
    return get_url(
        # //// Neoffice — the key is quoted: a character of the URL syntax in it cut the URL short.
        f"api/method/telephony.exotel.handler.handle_request?key={quote(webhook_verify_token or '', safe='')}"
    )


def get_exotel_settings():
    return frappe.get_single("TP Exotel Settings")


def validate_request():
    # workaround security since exotel does not support request signature
    # /api/method/<exotel-integration-method>?key=<exotel-webhook=verify-token>
    # //// Neoffice — the token is read through get_password (a Password field now) and compared in constant time.
    webhook_verify_token = get_webhook_verify_token()
    key = frappe.request.args.get("key")
    is_valid = bool(
        key
        and webhook_verify_token
        and hmac.compare_digest(key.encode(), webhook_verify_token.encode())
    )

    if not is_valid:
        frappe.throw(_("Unauthorized request"), exc=frappe.PermissionError)


# //// Neoffice — see validate_request.
def get_webhook_verify_token() -> str | None:
    return get_exotel_settings().get_password("webhook_verify_token", raise_exception=False)


# //// Neoffice — see make_a_call.
def _check_outgoing_call(from_number, caller_id, link_doctype, link_docname) -> None:
    if not is_telephony_manager():
        agent = frappe.db.get_value(
            "TP Telephony Agent",
            {"user": frappe.session.user},
            ["mobile_no", "exotel", "exotel_number"],
            as_dict=True,
        )
        if not agent or not agent.exotel:
            frappe.throw(_("Not permitted"), frappe.PermissionError)
        if (from_number and from_number != agent.mobile_no) or (
            caller_id and caller_id != agent.exotel_number
        ):
            frappe.throw(
                _("Only a telephony manager may place a call from another number"),
                frappe.PermissionError,
            )
    if link_doctype and link_docname:
        if not frappe.db.exists(link_doctype, link_docname) or not frappe.has_permission(
            link_doctype, "read", link_docname
        ):
            frappe.throw(_("Not permitted"), frappe.PermissionError)


# //// Neoffice — see handle_request: the agents of the call, or the Exotel agents when the event names none.
# A failure here is logged: it must not stop the call log below, as the plain broadcast never did.
def _publish_call_event(call_payload) -> None:
    try:
        _publish_to_the_agents(call_payload)
    except Exception:
        frappe.log_error("Exotel call event not published", frappe.get_traceback())


def _publish_to_the_agents(call_payload) -> None:
    payload = {key: value for key, value in call_payload.items() if key != "key"}
    users = {payload.get("AgentEmail")} - {None, ""}
    call_id = payload.get("CallSid")
    if not users and isinstance(call_id, str) and call_id:
        users = set(
            frappe.db.get_value("TP Call Log", call_id, ["caller", "receiver"]) or []
        ) - {None, ""}
    if not users:
        users = set(
            frappe.get_all("TP Telephony Agent", filters={"exotel": 1}, pluck="user")
        ) - {None, ""}
    for user in users:
        frappe.publish_realtime("exotel_call", payload, user=user)  # nosemgrep


@frappe.whitelist()
def is_integration_enabled():
    return frappe.db.get_single_value("TP Exotel Settings", "enabled", True)


# Call Log Functions
def create_call_log(
    call_id,
    from_number,
    to_number,
    medium,
    agent,
    status="Ringing",
    call_type="Incoming",
    link_doc=None,
):
    call_log = frappe.new_doc("TP Call Log")
    call_log.id = call_id
    call_log.to = to_number
    call_log.medium = medium
    call_log.type = call_type
    call_log.status = status
    call_log.telephony_medium = "Exotel"
    setattr(call_log, "from", from_number)

    if call_type == "Incoming":
        call_log.receiver = agent
    else:
        call_log.caller = agent

    contact_number = from_number if call_type == "Incoming" else to_number
    link_call_with_contact(contact_number, call_log)

    if link_doc and link_doc["doctype"] and link_doc["docname"]:
        link_call_with_doc(call_log, link_doc["doctype"], link_doc["docname"])

    call_log.save(ignore_permissions=True)
    frappe.db.commit()  # nosemgrep
    return call_log


def get_call_log(call_payload):
    call_log_id = call_payload.get("CallSid")
    # //// Neoffice — the key of a call log is text: a list or a dict from a JSON body would be a filter.
    if isinstance(call_log_id, str) and frappe.db.exists("TP Call Log", call_log_id):
        return frappe.get_doc("TP Call Log", call_log_id)


def get_call_log_status(call_payload, direction="inbound"):
    if direction == "outbound-api" or direction == "outbound-dial":
        status = call_payload.get("Status")
        status_map = {
            "completed": "Completed",
            "in-progress": "In Progress",
            "busy": "Ringing",
            "no-answer": "No Answer",
            "failed": "Failed",
        }
        return status_map.get(status)

    call_type = call_payload.get("CallType")
    status = call_payload.get("DialCallStatus") or call_payload.get("Status")

    if call_type == "incomplete" and status == "no-answer":
        status = "No Answer"
    elif call_type == "client-hangup" and status == "canceled":
        status = "Canceled"
    elif call_type == "incomplete" and status == "failed":
        status = "Failed"
    elif call_type == "completed":
        status = "Completed"
    elif status == "busy":
        status = "Ringing"

    return status


def update_call_log(call_payload, status="Ringing", call_log=None):
    direction = call_payload.get("Direction")
    call_log = call_log or get_call_log(call_payload)
    status = get_call_log_status(call_payload, direction)
    try:
        if call_log:
            call_log.status = status
            # resetting this because call might be redirected to other number
            call_log.to = call_payload.get("DialWhomNumber") or call_payload.get("To")
            call_log.duration = (
                call_payload.get("DialCallDuration")
                or call_payload.get("ConversationDuration")
                or 0
            )
            call_log.recording_url = (
                call_payload.get("RecordingUrl")
                if call_payload.get("RecordingUrl")
                and call_payload.get("RecordingUrl") != "null"
                else ""
            )
            call_log.start_time = call_payload.get("StartTime")
            call_log.end_time = call_payload.get("EndTime")

            if direction == "incoming" and call_payload.get("AgentEmail"):
                call_log.receiver = call_payload.get("AgentEmail")

            call_log.save(ignore_permissions=True)
            frappe.db.commit()  # nosemgrep
            return call_log
    except Exception:
        frappe.log_error(title="Error while updating call record")
        frappe.db.commit()  # nosemgrep
