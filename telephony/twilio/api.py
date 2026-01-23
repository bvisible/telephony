import json

import frappe
from frappe import _
from werkzeug.wrappers import Response

from telephony.utils import link_call_with_contact, link_call_with_doc

from .twilio_handler import IncomingCall, Twilio, TwilioCallDetails


@frappe.whitelist()
def is_enabled():
    return frappe.db.get_single_value("TP Twilio Settings", "enabled")


@frappe.whitelist()
def generate_access_token():
    """Returns access token that is required to authenticate Twilio Client SDK."""
    twilio = Twilio.connect()
    if not twilio:
        return {}

    from_number = frappe.db.get_value(
        "TP Telephony Agent",
        {"user": frappe.session.user},
        "twilio_number",
    )
    if not from_number:
        return {
            "ok": False,
            "error": "caller_phone_identity_missing",
            "detail": "Phone number is not mapped to the caller",
        }

    token = twilio.generate_voice_access_token(identity=frappe.session.user)
    return {"token": token}


@frappe.whitelist(allow_guest=True)
def voice(**kwargs):
    """This is a webhook called by twilio to get instructions when the voice call request comes to twilio server."""

    def _get_caller_number(caller):
        identity = caller.replace("client:", "").strip()
        user = Twilio.emailid_from_identity(identity)
        return frappe.db.get_value("TP Telephony Agent", user, "twilio_number")

    args = frappe._dict(kwargs)
    twilio = Twilio.connect()
    if not twilio:
        return

    assert args.AccountSid == twilio.account_sid
    assert args.ApplicationSid == twilio.application_sid

    # Generate TwiML instructions to make a call
    from_number = _get_caller_number(args.Caller)
    resp = twilio.generate_twilio_dial_response(from_number, args.To)

    call_details = TwilioCallDetails(args, call_from=from_number)
    create_call_log(
        call_details,
        link_doc={"doctype": args.link_doctype, "docname": args.link_docname},
    )
    return Response(resp.to_xml(), mimetype="text/xml")


@frappe.whitelist(allow_guest=True)
def twilio_incoming_call_handler(**kwargs):
    args = frappe._dict(kwargs)
    call_details = TwilioCallDetails(args)
    create_call_log(call_details)

    resp = IncomingCall(args.From, args.To).process()
    return Response(resp.to_xml(), mimetype="text/xml")


def create_call_log(call_details: TwilioCallDetails, link_doc=None):
    details = call_details.to_dict()

    call_log = frappe.get_doc(
        {**details, "doctype": "TP Call Log", "telephony_medium": "Twilio"}
    )

    contact_number = (
        details.get("from") if details.get("type") == "Incoming" else details.get("to")
    )
    link_call_with_contact(contact_number, call_log)

    if link_doc and link_doc["doctype"] and link_doc["docname"]:
        link_call_with_doc(call_log, link_doc["doctype"], link_doc["docname"])

    call_log.save(ignore_permissions=True)
    frappe.db.commit()  # nosemgrep
    return call_log


def update_call_log(call_sid, status=None):
    """Update call log status."""
    twilio = Twilio.connect()
    if not (twilio and frappe.db.exists("TP Call Log", call_sid)):
        return

    # Retry logic for update conflict when multiple requests are made
    MAX_RETRIES = 3
    for i in range(MAX_RETRIES):
        try:
            call_details = twilio.get_call_info(call_sid)
            call_log = frappe.get_doc("TP Call Log", call_sid)

            call_log.status = TwilioCallDetails.get_call_status(
                status or call_details.status
            )
            call_log.duration = call_details.duration
            call_log.start_time = get_datetime_from_timestamp(call_details.start_time)
            call_log.end_time = get_datetime_from_timestamp(call_details.end_time)

            call_log.save(ignore_permissions=True)
            frappe.db.commit()  # nosemgrep
            return call_log

        except frappe.exceptions.TimestampMismatchError:
            frappe.clear_messages()
            if i == MAX_RETRIES - 1:
                frappe.log_error(
                    f"Failed to update call log {call_sid} after {MAX_RETRIES} retries",
                    "Call Log Update Error",
                )
                raise
            # Auto-retry will fetch fresh document on next iteration
            continue

        except Exception as e:
            frappe.log_error(
                f"Error while updating call record: {str(e)}\n{frappe.get_traceback()}",
                "Call Log Update Error",
            )
            frappe.db.commit()  # nosemgrep
            break
    return


@frappe.whitelist(allow_guest=True)
def update_recording_info(**kwargs):
    try:
        args = frappe._dict(kwargs)
        recording_url = args.RecordingUrl
        call_sid = args.CallSid
        update_call_log(call_sid)
        frappe.db.set_value("TP Call Log", call_sid, "recording_url", recording_url)
    except Exception:
        frappe.log_error(title=_("Failed to capture Twilio recording"))


@frappe.whitelist(allow_guest=True)
def update_call_status_info(**kwargs):
    try:
        args = frappe._dict(kwargs)
        parent_call_sid = args.ParentCallSid
        update_call_log(parent_call_sid, status=args.CallStatus)

        call_info = {
            "ParentCallSid": args.ParentCallSid,
            "CallSid": args.CallSid,
            "CallStatus": args.CallStatus,
            "CallDuration": args.CallDuration,
            "From": args.From,
            "To": args.To,
        }

        client = Twilio.get_twilio_client()
        client.calls(args.ParentCallSid).user_defined_messages.create(
            content=json.dumps(call_info)
        )
    except Exception:
        frappe.log_error(title=_("Failed to update Twilio call status"))


def get_datetime_from_timestamp(timestamp):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not timestamp:
        return None

    datetime_utc_tz_str = timestamp.strftime("%Y-%m-%d %H:%M:%S%z")
    datetime_utc_tz = datetime.strptime(datetime_utc_tz_str, "%Y-%m-%d %H:%M:%S%z")
    system_timezone = frappe.utils.get_system_timezone()
    converted_datetime = datetime_utc_tz.astimezone(ZoneInfo(system_timezone))
    return frappe.utils.format_datetime(converted_datetime, "yyyy-MM-dd HH:mm:ss")


@frappe.whitelist()
def fetch_applications():
    twilio = Twilio.get_twilio_client()
    applications = [app.friendly_name for app in twilio.applications.list()]
    frappe.db.set_single_value(
        "TP Twilio Settings",
        "twilio_apps",
        ",".join(applications),
    )
    return applications


@frappe.whitelist(allow_guest=True)
def handle_dial_status(**kwargs):
    """Handle dial completion callback - used when no one answers."""
    from twilio.twiml.voice_response import VoiceResponse

    args = frappe._dict(kwargs)
    dial_status = args.DialCallStatus

    resp = VoiceResponse()

    # If no one answered, check config for action
    if dial_status in ["no-answer", "busy", "failed"]:
        # Try to get routing config
        to_number = args.To
        from telephony.ftelephony.doctype.tp_phone_number_config.tp_phone_number_config import (
            get_routing_config,
        )

        config = get_routing_config(to_number)

        if config:
            no_answer_action = config.get("no_answer_action", "Voicemail")

            if no_answer_action == "Voicemail" and config.get("voicemail_enabled"):
                # Play voicemail greeting and record
                greeting = config.get("voicemail_greeting") or _(
                    "Please leave a message after the tone."
                )
                resp.say(greeting, language="en-US")
                resp.record(
                    max_length=120,
                    action="/api/method/telephony.twilio.api.handle_voicemail",
                    transcribe=config.get("voicemail_transcribe", False),
                    transcribe_callback="/api/method/telephony.twilio.api.handle_voicemail_transcription"
                    if config.get("voicemail_transcribe")
                    else None,
                    play_beep=True,
                )
            elif no_answer_action == "Message":
                resp.say(
                    _("All agents are currently busy. Please try again later."),
                    language="en-US",
                )
                resp.hangup()
            else:
                resp.hangup()
        else:
            # No config, just say message and hang up
            resp.say(
                _("Agent is unavailable to take the call, please call after some time."),
                language="en-US",
            )
            resp.hangup()
    else:
        # Call was answered or other status, just end
        resp.hangup()

    return Response(resp.to_xml(), mimetype="text/xml")


@frappe.whitelist(allow_guest=True)
def handle_sequential_dial(**kwargs):
    """Handle sequential dial callback - move to next agent if no answer."""
    from twilio.twiml.voice_response import VoiceResponse, Dial

    args = frappe._dict(kwargs)
    dial_status = args.DialCallStatus

    resp = VoiceResponse()

    # If answered, just hang up (call already connected)
    if dial_status == "completed":
        resp.hangup()
        return Response(resp.to_xml(), mimetype="text/xml")

    # Get routing config
    to_number = args.To
    from_number = args.From

    from telephony.ftelephony.doctype.tp_phone_number_config.tp_phone_number_config import (
        get_routing_config,
    )

    config = get_routing_config(to_number)

    if not config:
        resp.say(
            _("Agent is unavailable to take the call, please call after some time."),
            language="en-US",
        )
        resp.hangup()
        return Response(resp.to_xml(), mimetype="text/xml")

    agents = config.get("available_agents", [])

    # Get current agent index from call metadata or cache
    cache_key = f"twilio_seq_{args.CallSid}"
    current_index = frappe.cache().get_value(cache_key) or 0
    next_index = current_index + 1

    if next_index >= len(agents):
        # No more agents, go to no-answer action
        frappe.cache().delete_value(cache_key)
        no_answer_action = config.get("no_answer_action", "Voicemail")

        if no_answer_action == "Voicemail" and config.get("voicemail_enabled"):
            greeting = config.get("voicemail_greeting") or _(
                "Please leave a message after the tone."
            )
            resp.say(greeting, language="en-US")
            resp.record(
                max_length=120,
                action="/api/method/telephony.twilio.api.handle_voicemail",
                transcribe=config.get("voicemail_transcribe", False),
                transcribe_callback="/api/method/telephony.twilio.api.handle_voicemail_transcription"
                if config.get("voicemail_transcribe")
                else None,
                play_beep=True,
            )
        elif no_answer_action == "Message":
            resp.say(
                _("All agents are currently busy. Please try again later."),
                language="en-US",
            )
            resp.hangup()
        else:
            resp.hangup()
    else:
        # Try next agent
        frappe.cache().set_value(cache_key, next_index, expires_in_sec=300)
        twilio = Twilio.connect()
        next_agent = agents[next_index]

        dial = Dial(
            caller_id=from_number,
            timeout=config.get("ring_timeout", 30),
            record="record-from-answer-dual" if config.get("recording_enabled") else "do-not-record",
            recording_status_callback=twilio.get_recording_status_callback_url()
            if config.get("recording_enabled")
            else None,
            recording_status_callback_event="completed" if config.get("recording_enabled") else None,
            action="/api/method/telephony.twilio.api.handle_sequential_dial",
        )

        if next_agent["call_receiving_device"] == "Phone" and next_agent.get("mobile_no"):
            dial.number(
                next_agent["mobile_no"],
                status_callback_event="initiated ringing answered completed",
                status_callback=twilio.get_update_call_status_callback_url(),
                status_callback_method="POST",
            )
        elif next_agent["call_receiving_device"] == "Computer":
            dial.client(
                twilio.safe_identity(next_agent["user"]),
                status_callback_event="initiated ringing answered completed",
                status_callback=twilio.get_update_call_status_callback_url(),
                status_callback_method="POST",
            )

        resp.append(dial)

    return Response(resp.to_xml(), mimetype="text/xml")


@frappe.whitelist(allow_guest=True)
def handle_voicemail(**kwargs):
    """Handle voicemail recording completion."""
    from twilio.twiml.voice_response import VoiceResponse

    args = frappe._dict(kwargs)

    # Save voicemail info
    try:
        recording_url = args.RecordingUrl
        call_sid = args.CallSid
        to_number = args.To
        from_number = args.From
        duration = args.RecordingDuration

        # Create voicemail record
        voicemail = frappe.get_doc(
            {
                "doctype": "TP Voicemail",
                "call_sid": call_sid,
                "from_number": from_number,
                "to_number": to_number,
                "recording_url": recording_url,
                "duration": duration,
                "status": "New",
            }
        )
        voicemail.insert(ignore_permissions=True)
        frappe.db.commit()

        # Send notification email if configured
        from telephony.ftelephony.doctype.tp_phone_number_config.tp_phone_number_config import (
            get_routing_config,
        )

        config = get_routing_config(to_number)
        if config and config.get("voicemail_email"):
            _send_voicemail_notification(
                config.get("voicemail_email"),
                from_number,
                to_number,
                recording_url,
                duration,
            )

    except Exception as e:
        frappe.log_error("Voicemail Save Error", str(e))

    resp = VoiceResponse()
    resp.say(_("Thank you. Goodbye."), language="en-US")
    resp.hangup()

    return Response(resp.to_xml(), mimetype="text/xml")


@frappe.whitelist(allow_guest=True)
def handle_voicemail_transcription(**kwargs):
    """Handle voicemail transcription callback."""
    args = frappe._dict(kwargs)

    try:
        call_sid = args.CallSid
        transcription_text = args.TranscriptionText
        transcription_status = args.TranscriptionStatus

        if transcription_status == "completed" and transcription_text:
            # Update voicemail record with transcription
            voicemail_name = frappe.db.get_value(
                "TP Voicemail", {"call_sid": call_sid}, "name"
            )
            if voicemail_name:
                frappe.db.set_value(
                    "TP Voicemail", voicemail_name, "transcription", transcription_text
                )
                frappe.db.commit()
    except Exception as e:
        frappe.log_error("Voicemail Transcription Error", str(e))

    return "OK"


def _send_voicemail_notification(email, from_number, to_number, recording_url, duration):
    """Send email notification for new voicemail."""
    try:
        subject = _("New Voicemail from {0}").format(from_number)
        message = _(
            """
            <p>You have a new voicemail:</p>
            <ul>
                <li><strong>From:</strong> {from_number}</li>
                <li><strong>To:</strong> {to_number}</li>
                <li><strong>Duration:</strong> {duration} seconds</li>
            </ul>
            <p><a href="{recording_url}">Listen to voicemail</a></p>
            """
        ).format(
            from_number=from_number,
            to_number=to_number,
            duration=duration,
            recording_url=recording_url,
        )

        frappe.sendmail(
            recipients=[email],
            subject=subject,
            message=message,
            now=True,
        )
    except Exception as e:
        frappe.log_error("Voicemail Email Error", str(e))
