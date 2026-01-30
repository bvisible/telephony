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

    # Pass metadata to IncomingCall for conference tracking
    resp = IncomingCall(args.From, args.To, meta=dict(args)).process()
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

            # Calculate call cost when call is completed
            if call_log.status == "Completed" and not call_log.cost_calculated:
                _calculate_call_cost(call_log, call_details)

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


# =============================================================================
# Conference Callbacks for Call Transfer Support
# =============================================================================

@frappe.whitelist(allow_guest=True)
def handle_conference_status(**kwargs):
    """Handle conference status updates from Twilio.

    Called when conference starts, ends, or participant joins/leaves.
    """
    from twilio.twiml.voice_response import VoiceResponse

    args = frappe._dict(kwargs)
    conference_name = args.FriendlyName or args.ConferenceSid
    status_event = args.StatusCallbackEvent

    frappe.logger().info(f"Conference {conference_name} event: {status_event}")

    try:
        if status_event == "conference-start":
            # Conference started - dial agents
            _dial_agents_to_conference(conference_name)

        elif status_event == "participant-join":
            # Update participant status in our records
            call_sid = args.CallSid
            _update_conference_participant(conference_name, call_sid, "Connected")

        elif status_event == "participant-leave":
            # Update participant status
            call_sid = args.CallSid
            _update_conference_participant(conference_name, call_sid, "Left")

        elif status_event == "conference-end":
            # End conference in our records
            _end_conference(conference_name)

    except Exception as e:
        frappe.log_error("Conference Status Error", f"{conference_name}: {str(e)}")

    resp = VoiceResponse()
    return Response(resp.to_xml(), mimetype="text/xml")


@frappe.whitelist(allow_guest=True)
def handle_conference_participant(**kwargs):
    """Handle participant status updates in conference."""
    args = frappe._dict(kwargs)
    call_sid = args.CallSid
    call_status = args.CallStatus
    conference_name = args.get("FriendlyName") or args.get("ConferenceSid")

    frappe.logger().info(f"Participant {call_sid} status: {call_status}")

    try:
        if call_status == "completed":
            _update_conference_participant(conference_name, call_sid, "Left")
        elif call_status == "answered":
            _update_conference_participant(conference_name, call_sid, "Connected")
        elif call_status in ["no-answer", "busy", "failed", "canceled"]:
            _handle_agent_no_answer(conference_name, call_sid)
    except Exception as e:
        frappe.log_error("Participant Status Error", f"{call_sid}: {str(e)}")

    return "OK"


@frappe.whitelist(allow_guest=True)
def conference_wait_music(**kwargs):
    """Return TwiML for conference waiting music."""
    from twilio.twiml.voice_response import VoiceResponse

    resp = VoiceResponse()
    # Play waiting music while caller waits for agent
    resp.play(url="http://com.twilio.sounds.music.s3.amazonaws.com/MARKOVICHAMP-B7U4sLvFlo.mp3", loop=10)
    return Response(resp.to_xml(), mimetype="text/xml")


@frappe.whitelist(allow_guest=True)
def hold_music(**kwargs):
    """Return TwiML for hold music."""
    from twilio.twiml.voice_response import VoiceResponse

    resp = VoiceResponse()
    resp.play(url="http://com.twilio.sounds.music.s3.amazonaws.com/ClockworkWaltz.mp3", loop=10)
    return Response(resp.to_xml(), mimetype="text/xml")


def _dial_agents_to_conference(conference_name: str):
    """Dial agents into a conference when it starts."""
    # Get cached agent info
    cache_key = f"conf_agents_{conference_name}"
    agent_data = frappe.cache().get_value(cache_key)

    if not agent_data:
        frappe.logger().warning(f"No agent data for conference {conference_name}")
        return

    agents = agent_data.get("agents", [])
    caller_id = agent_data.get("caller_id")
    mode = agent_data.get("mode", "simultaneous")

    if not agents:
        return

    twilio = Twilio.connect()
    if not twilio:
        return

    if mode == "simultaneous":
        # Dial all agents at once
        for agent in agents:
            _dial_single_agent(twilio, conference_name, agent, caller_id)
    else:
        # Sequential/Round Robin - dial first agent only
        first_agent = agents[0]
        _dial_single_agent(twilio, conference_name, first_agent, caller_id)


def _dial_single_agent(twilio, conference_name: str, agent: dict, caller_id: str):
    """Dial a single agent into the conference."""
    try:
        if agent["device"] == "Phone" and agent.get("mobile_no"):
            participant = twilio.dial_into_conference(
                conference_name=conference_name,
                to=agent["mobile_no"],
                caller_id=caller_id,
                is_client=False,
            )
        elif agent["device"] == "Computer" and agent.get("user"):
            participant = twilio.dial_into_conference(
                conference_name=conference_name,
                to=agent["user"],
                caller_id=caller_id,
                is_client=True,
            )
        else:
            return None

        # Add participant to conference record
        if participant:
            _add_agent_participant(conference_name, participant.call_sid, agent)

        return participant
    except Exception as e:
        frappe.log_error("Agent Dial Error", f"Failed to dial agent: {str(e)}")
        return None


def _add_agent_participant(conference_name: str, call_sid: str, agent: dict):
    """Add an agent participant to the conference record."""
    try:
        conf_doc = frappe.get_doc("TP Call Conference", conference_name)
        conf_doc.add_participant(
            call_sid=call_sid,
            participant_type="Agent",
            phone_number=agent.get("mobile_no"),
            user=agent.get("user"),
            status="Ringing",
        )
    except Exception as e:
        frappe.log_error("Add Participant Error", f"Conference {conference_name}: {str(e)}")


def _update_conference_participant(conference_name: str, call_sid: str, status: str):
    """Update a participant's status in the conference record."""
    try:
        if not conference_name:
            # Try to find conference by call_sid
            from telephony.ftelephony.doctype.tp_call_conference.tp_call_conference import (
                get_active_conference_by_call_sid,
            )
            conf_doc = get_active_conference_by_call_sid(call_sid)
        else:
            if frappe.db.exists("TP Call Conference", conference_name):
                conf_doc = frappe.get_doc("TP Call Conference", conference_name)
            else:
                return

        if conf_doc:
            conf_doc.update_participant_status(call_sid, status)
    except Exception as e:
        frappe.log_error("Update Participant Error", f"{call_sid}: {str(e)}")


def _handle_agent_no_answer(conference_name: str, call_sid: str):
    """Handle when an agent doesn't answer in sequential/round robin mode."""
    try:
        # Update participant status
        _update_conference_participant(conference_name, call_sid, "Left")

        # Check if we need to dial next agent (sequential/round robin)
        cache_key = f"conf_agents_{conference_name}"
        agent_data = frappe.cache().get_value(cache_key)

        if not agent_data:
            return

        mode = agent_data.get("mode", "simultaneous")

        if mode in ["sequential", "round_robin"]:
            agents = agent_data.get("agents", [])
            current_index = agent_data.get("current_index", 0)
            next_index = current_index + 1

            if next_index < len(agents):
                # Dial next agent
                agent_data["current_index"] = next_index
                frappe.cache().set_value(cache_key, agent_data, expires_in_sec=300)

                twilio = Twilio.connect()
                if twilio:
                    next_agent = agents[next_index]
                    _dial_single_agent(twilio, conference_name, next_agent, agent_data.get("caller_id"))
            else:
                # No more agents - cleanup cache
                frappe.cache().delete_value(cache_key)
    except Exception as e:
        frappe.log_error("Agent No Answer Error", f"{conference_name}: {str(e)}")


def _end_conference(conference_name: str):
    """Mark a conference as ended in our records."""
    try:
        if frappe.db.exists("TP Call Conference", conference_name):
            conf_doc = frappe.get_doc("TP Call Conference", conference_name)
            conf_doc.end_conference()

        # Clean up cache
        cache_key = f"conf_agents_{conference_name}"
        frappe.cache().delete_value(cache_key)
    except Exception as e:
        frappe.log_error("End Conference Error", f"{conference_name}: {str(e)}")


# =============================================================================
# Call Cost Calculation
# =============================================================================

def _calculate_call_cost(call_log, call_details):
    """Calculate and update call cost from Twilio call details.

    Twilio provides the cost in USD. We convert to CHF and store it.

    Args:
        call_log: TP Call Log document
        call_details: Twilio call resource object
    """
    try:
        # Get price from Twilio (negative value means debit)
        price = call_details.price
        price_unit = call_details.price_unit

        if price is None:
            # Price not yet available, will be calculated on next update
            return

        # Convert price to positive value (Twilio returns negative for debits)
        cost = abs(float(price))

        # Convert to CHF if not already
        if price_unit and price_unit.upper() != "CHF":
            cost = _convert_to_chf(cost, price_unit)

        # Update call log
        call_log.call_cost = cost
        call_log.call_cost_currency = "CHF"
        call_log.cost_calculated = 1

        frappe.logger().info(f"Call {call_log.id} cost calculated: CHF {cost:.4f}")

    except Exception as e:
        frappe.log_error("Call Cost Calculation Error", f"Call {call_log.id}: {str(e)}")


def _convert_to_chf(amount: float, from_currency: str) -> float:
    """Convert amount from source currency to CHF.

    Uses ERPNext currency exchange if available, otherwise uses fixed rates.

    Args:
        amount: Amount in source currency
        from_currency: Source currency code (e.g., 'USD')

    Returns:
        Amount converted to CHF
    """
    if from_currency.upper() == "CHF":
        return amount

    # Try to get exchange rate from ERPNext
    try:
        exchange_rate = frappe.db.get_value(
            "Currency Exchange",
            {"from_currency": from_currency.upper(), "to_currency": "CHF"},
            "exchange_rate",
            order_by="date desc",
        )
        if exchange_rate:
            return amount * float(exchange_rate)
    except Exception:
        pass

    # Fallback to approximate fixed rates (USD to CHF)
    fixed_rates = {
        "USD": 0.88,  # Approximate USD to CHF rate
        "EUR": 0.95,  # Approximate EUR to CHF rate
        "GBP": 1.10,  # Approximate GBP to CHF rate
    }

    rate = fixed_rates.get(from_currency.upper(), 1.0)
    return amount * rate


@frappe.whitelist()
def recalculate_call_costs(from_date=None, to_date=None):
    """Recalculate costs for calls that don't have cost calculated.

    This is useful for backfilling costs for existing calls.

    Args:
        from_date: Optional start date filter
        to_date: Optional end date filter
    """
    filters = {
        "telephony_medium": "Twilio",
        "status": "Completed",
        "cost_calculated": 0,
    }

    if from_date:
        filters["creation"] = [">=", from_date]
    if to_date:
        filters["creation"] = ["<=", to_date] if "creation" not in filters else ["between", [from_date, to_date]]

    calls = frappe.get_all("TP Call Log", filters=filters, fields=["name", "id"])

    twilio = Twilio.connect()
    if not twilio:
        return {"error": "Twilio not connected"}

    updated = 0
    errors = 0

    for call in calls:
        try:
            call_details = twilio.get_call_info(call.id)
            call_log = frappe.get_doc("TP Call Log", call.name)
            _calculate_call_cost(call_log, call_details)
            call_log.save(ignore_permissions=True)
            updated += 1
        except Exception as e:
            frappe.log_error("Recalculate Cost Error", f"Call {call.id}: {str(e)}")
            errors += 1

    frappe.db.commit()

    return {
        "success": True,
        "updated": updated,
        "errors": errors,
        "message": f"Recalculated costs for {updated} calls ({errors} errors)",
    }
