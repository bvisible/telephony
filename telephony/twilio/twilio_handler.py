import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Dial, VoiceResponse, Gather

from .utils import get_public_url, merge_dicts

# Conference-based architecture for call transfer support
from telephony.ftelephony.doctype.tp_call_conference.tp_call_conference import (
    generate_conference_name,
    get_active_conference_by_call_sid,
)


class Twilio:
    """Twilio connector over TwilioClient."""

    def __init__(self, settings):
        """
        :param settings: `TP Twilio Settings` doctype
        """
        self.settings = settings
        self.account_sid = settings.account_sid
        self.application_sid = settings.twiml_sid
        self.api_key = settings.api_key
        self.api_secret = settings.get_password("api_secret")
        self.twilio_client = self.get_twilio_client()

    @classmethod
    def connect(self):
        """Make a twilio connection."""
        settings = frappe.get_doc("TP Twilio Settings")
        if not (settings and settings.enabled):
            return
        return Twilio(settings=settings)

    def get_phone_numbers(self):
        """Get account's twilio phone numbers."""
        numbers = self.twilio_client.incoming_phone_numbers.list()
        return [n.phone_number for n in numbers]

    def generate_voice_access_token(self, identity: str, ttl=60 * 60):
        """Generates a token required to make voice calls from the browser."""
        # identity is used by twilio to identify the user uniqueness at browser(or any endpoints).
        identity = self.safe_identity(identity)

        # Create access token with credentials
        token = AccessToken(
            self.account_sid, self.api_key, self.api_secret, identity=identity, ttl=ttl
        )

        # Create a Voice grant and add to token
        voice_grant = VoiceGrant(
            outgoing_application_sid=self.application_sid,
            incoming_allow=True,  # Allow incoming calls
        )
        token.add_grant(voice_grant)
        return token.to_jwt()

    @classmethod
    def safe_identity(cls, identity: str):
        """Create a safe identity by replacing unsupported special charaters `@` with (at)).
        Twilio Client JS fails to make a call connection if identity has special characters like @, [, / etc)
        https://www.twilio.com/docs/voice/client/errors (#31105)
        """
        return identity.replace("@", "(at)")

    @classmethod
    def emailid_from_identity(cls, identity: str):
        """Convert safe identity string into emailID."""
        return identity.replace("(at)", "@")

    def get_recording_status_callback_url(self):
        url_path = "/api/method/telephony.twilio.api.update_recording_info"
        return get_public_url(url_path)

    def get_update_call_status_callback_url(self):
        url_path = "/api/method/telephony.twilio.api.update_call_status_info"
        return get_public_url(url_path)

    def generate_twilio_dial_response(self, from_number: str, to_number: str):
        """Generates voice call instructions to forward the call to agents Phone."""
        resp = VoiceResponse()
        dial = Dial(
            caller_id=from_number,
            record=self.settings.record_calls,
            recording_status_callback=self.get_recording_status_callback_url(),
            recording_status_callback_event="completed",
        )
        dial.number(
            to_number,
            status_callback_event="initiated ringing answered completed",
            status_callback=self.get_update_call_status_callback_url(),
            status_callback_method="POST",
        )
        resp.append(dial)
        return resp

    def get_call_info(self, call_sid):
        return self.twilio_client.calls(call_sid).fetch()

    def get_conference_status_callback_url(self):
        """URL for conference status callbacks."""
        url_path = "/api/method/telephony.twilio.api.handle_conference_status"
        return get_public_url(url_path)

    def get_conference_participant_callback_url(self):
        """URL for conference participant callbacks."""
        url_path = "/api/method/telephony.twilio.api.handle_conference_participant"
        return get_public_url(url_path)

    def dial_into_conference(self, conference_name: str, to: str, caller_id: str, is_client: bool = False):
        """Dial a participant into an existing conference via REST API.

        Args:
            conference_name: The friendly name of the conference
            to: Phone number or client identity to dial
            caller_id: Caller ID to show
            is_client: If True, dial a softphone client; if False, dial a phone number
        """
        participant_callback = self.get_conference_participant_callback_url()

        # Build the destination
        if is_client:
            to = f"client:{self.safe_identity(to)}"

        try:
            participant = self.twilio_client.conferences(conference_name).participants.create(
                to=to,
                from_=caller_id,
                early_media=True,
                beep=False,
                start_conference_on_enter=True,
                end_conference_on_exit=False,
                status_callback=participant_callback,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )
            return participant
        except Exception as e:
            frappe.log_error("Conference Dial Error", f"Failed to dial {to} into {conference_name}: {str(e)}")
            return None

    def hold_participant(self, conference_name: str, call_sid: str, hold: bool = True, hold_url: str = None):
        """Put a conference participant on hold or resume them.

        Args:
            conference_name: The friendly name of the conference
            call_sid: The call SID of the participant
            hold: True to hold, False to resume
            hold_url: Optional URL for hold music
        """
        try:
            self.twilio_client.conferences(conference_name).participants(call_sid).update(
                hold=hold,
                hold_url=hold_url or get_public_url("/api/method/telephony.twilio.api.hold_music"),
            )
            return True
        except Exception as e:
            frappe.log_error("Hold Error", f"Failed to hold/resume {call_sid}: {str(e)}")
            return False

    def remove_participant(self, conference_name: str, call_sid: str):
        """Remove a participant from a conference.

        Args:
            conference_name: The friendly name of the conference
            call_sid: The call SID of the participant to remove
        """
        try:
            self.twilio_client.conferences(conference_name).participants(call_sid).delete()
            return True
        except Exception as e:
            frappe.log_error("Remove Participant Error", f"Failed to remove {call_sid}: {str(e)}")
            return False

    def generate_twilio_client_response(self, client, ring_tone="at"):
        """Generates voice call instructions to forward the call to agents computer."""
        resp = VoiceResponse()
        dial = Dial(
            ring_tone=ring_tone,
            record=self.settings.record_calls,
            recording_status_callback=self.get_recording_status_callback_url(),
            recording_status_callback_event="completed",
        )
        dial.client(
            client,
            status_callback_event="initiated ringing answered completed",
            status_callback=self.get_update_call_status_callback_url(),
            status_callback_method="POST",
        )
        resp.append(dial)
        return resp

    @classmethod
    def get_twilio_client(self):
        twilio_settings = frappe.get_doc("TP Twilio Settings")
        if not twilio_settings.enabled:
            frappe.throw(_("Please enable twilio to proceed."))

        auth_token = get_decrypted_password(
            "TP Twilio Settings", "TP Twilio Settings", "auth_token"
        )
        client = TwilioClient(twilio_settings.account_sid, auth_token)

        return client


class IncomingCall:
    def __init__(self, from_number, to_number, meta=None):
        self.from_number = from_number
        self.to_number = to_number
        self.meta = meta

    def process(self):
        """Process the incoming call
        * First check for routing configuration (TP Phone Number Config)
        * If exists, use advanced routing logic
        * Otherwise, fall back to legacy behavior
        """
        # Try to get routing configuration
        routing_config = self._get_routing_config()

        if routing_config:
            return self._process_with_routing(routing_config)

        # Fall back to legacy behavior
        return self._process_legacy()

    def _get_routing_config(self):
        """Get routing configuration for the called number."""
        try:
            from telephony.ftelephony.doctype.tp_phone_number_config.tp_phone_number_config import (
                get_routing_config,
            )

            return get_routing_config(self.to_number)
        except Exception as e:
            frappe.log_error("Routing Config Error", f"Failed to get routing config: {str(e)}")
            return None

    def _process_with_routing(self, config):
        """Process call using advanced routing configuration."""
        twilio = Twilio.connect()

        # Check business hours
        if not config.get("within_business_hours"):
            return self._handle_outside_hours(config)

        # Get available agents
        agents = config.get("available_agents", [])

        if not agents:
            return self._handle_no_agents(config)

        # Generate response based on ring mode
        ring_mode = config.get("ring_mode", "Simultaneous")

        if ring_mode == "Simultaneous":
            return self._ring_simultaneous(twilio, agents, config)
        elif ring_mode == "Sequential":
            return self._ring_sequential(twilio, agents, config)
        elif ring_mode == "Round Robin":
            return self._ring_round_robin(twilio, agents, config)

        # Default to simultaneous
        return self._ring_simultaneous(twilio, agents, config)

    def _ring_simultaneous(self, twilio, agents, config):
        """Ring all agents at the same time using Conference architecture.

        This creates a conference that allows call transfers.
        The caller joins first, then agents are dialed in via REST API.
        """
        call_sid = self.meta.get("CallSid") if self.meta else None
        conference_name = generate_conference_name(call_sid or "unknown")

        # Create conference record for tracking
        try:
            conference_doc = frappe.get_doc({
                "doctype": "TP Call Conference",
                "conference_name": conference_name,
                "original_call_sid": call_sid,
                "caller_number": self.from_number,
                "called_number": self.to_number,
                "status": "Active",
            })
            conference_doc.add_participant(
                call_sid=call_sid,
                participant_type="Caller",
                phone_number=self.from_number,
                status="Ringing",
            )
            conference_doc.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as e:
            frappe.log_error("Conference Create Error", f"Failed to create conference: {str(e)}")

        # Build TwiML to put caller in conference
        resp = VoiceResponse()

        dial = Dial(
            caller_id=self.from_number,
            timeout=config.get("ring_timeout", 30),
            record="record-from-answer-dual" if config.get("recording_enabled") else "do-not-record",
            recording_status_callback=twilio.get_recording_status_callback_url() if config.get("recording_enabled") else None,
            recording_status_callback_event="completed" if config.get("recording_enabled") else None,
            action=get_public_url("/api/method/telephony.twilio.api.handle_dial_status"),
        )

        # Put caller into conference
        dial.conference(
            conference_name,
            start_conference_on_enter=True,
            end_conference_on_exit=True,
            beep=False,
            wait_url=get_public_url("/api/method/telephony.twilio.api.conference_wait_music"),
            status_callback=twilio.get_conference_status_callback_url(),
            status_callback_event="start end join leave",
        )

        resp.append(dial)

        # Queue agent dial requests (will be processed by conference start callback)
        # Store agents to dial in cache for the conference callback to pick up
        agents_to_dial = []
        for agent in agents:
            agents_to_dial.append({
                "user": agent.get("user"),
                "mobile_no": agent.get("mobile_no"),
                "device": agent["call_receiving_device"],
            })

        if agents_to_dial:
            frappe.cache().set_value(
                f"conf_agents_{conference_name}",
                {
                    "agents": agents_to_dial,
                    "caller_id": self.to_number,  # Use the called number as caller ID for agents
                    "from_number": self.from_number,
                },
                expires_in_sec=120,
            )

        return resp

    def _ring_sequential(self, twilio, agents, config):
        """Ring agents one by one based on priority using Conference architecture.

        Creates conference and dials first agent. If not answered, callback dials next.
        """
        if not agents:
            return self._handle_no_agents(config)

        call_sid = self.meta.get("CallSid") if self.meta else None
        conference_name = generate_conference_name(call_sid or "unknown")

        # Create conference record
        try:
            conference_doc = frappe.get_doc({
                "doctype": "TP Call Conference",
                "conference_name": conference_name,
                "original_call_sid": call_sid,
                "caller_number": self.from_number,
                "called_number": self.to_number,
                "status": "Active",
            })
            conference_doc.add_participant(
                call_sid=call_sid,
                participant_type="Caller",
                phone_number=self.from_number,
                status="Ringing",
            )
            conference_doc.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as e:
            frappe.log_error("Conference Create Error", f"Failed to create conference: {str(e)}")

        resp = VoiceResponse()
        timeout = config.get("ring_timeout", 30)

        dial = Dial(
            caller_id=self.from_number,
            timeout=timeout,
            record="record-from-answer-dual" if config.get("recording_enabled") else "do-not-record",
            recording_status_callback=twilio.get_recording_status_callback_url() if config.get("recording_enabled") else None,
            recording_status_callback_event="completed" if config.get("recording_enabled") else None,
            action=get_public_url("/api/method/telephony.twilio.api.handle_dial_status"),
        )

        # Put caller into conference
        dial.conference(
            conference_name,
            start_conference_on_enter=True,
            end_conference_on_exit=True,
            beep=False,
            wait_url=get_public_url("/api/method/telephony.twilio.api.conference_wait_music"),
            status_callback=twilio.get_conference_status_callback_url(),
            status_callback_event="start end join leave",
        )

        resp.append(dial)

        # Store agents for sequential dialing (first agent first)
        agents_to_dial = []
        for agent in agents:
            agents_to_dial.append({
                "user": agent.get("user"),
                "mobile_no": agent.get("mobile_no"),
                "device": agent["call_receiving_device"],
            })

        frappe.cache().set_value(
            f"conf_agents_{conference_name}",
            {
                "agents": agents_to_dial,
                "caller_id": self.to_number,
                "from_number": self.from_number,
                "mode": "sequential",
                "current_index": 0,
                "timeout": timeout,
            },
            expires_in_sec=300,
        )

        return resp

    def _ring_round_robin(self, twilio, agents, config):
        """Ring agents in round robin fashion using Conference architecture.

        Creates conference and dials the next agent in rotation.
        """
        if not agents:
            return self._handle_no_agents(config)

        # Get current round robin index
        rr_cache_key = f"twilio_rr_{self.to_number}"
        current_index = frappe.cache().get_value(rr_cache_key) or 0

        # Move to next agent for next call
        next_index = (current_index + 1) % len(agents)
        frappe.cache().set_value(rr_cache_key, next_index, expires_in_sec=3600)

        call_sid = self.meta.get("CallSid") if self.meta else None
        conference_name = generate_conference_name(call_sid or "unknown")

        # Create conference record
        try:
            conference_doc = frappe.get_doc({
                "doctype": "TP Call Conference",
                "conference_name": conference_name,
                "original_call_sid": call_sid,
                "caller_number": self.from_number,
                "called_number": self.to_number,
                "status": "Active",
            })
            conference_doc.add_participant(
                call_sid=call_sid,
                participant_type="Caller",
                phone_number=self.from_number,
                status="Ringing",
            )
            conference_doc.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as e:
            frappe.log_error("Conference Create Error", f"Failed to create conference: {str(e)}")

        resp = VoiceResponse()
        timeout = config.get("ring_timeout", 30)

        dial = Dial(
            caller_id=self.from_number,
            timeout=timeout,
            record="record-from-answer-dual" if config.get("recording_enabled") else "do-not-record",
            recording_status_callback=twilio.get_recording_status_callback_url() if config.get("recording_enabled") else None,
            recording_status_callback_event="completed" if config.get("recording_enabled") else None,
            action=get_public_url("/api/method/telephony.twilio.api.handle_dial_status"),
        )

        # Put caller into conference
        dial.conference(
            conference_name,
            start_conference_on_enter=True,
            end_conference_on_exit=True,
            beep=False,
            wait_url=get_public_url("/api/method/telephony.twilio.api.conference_wait_music"),
            status_callback=twilio.get_conference_status_callback_url(),
            status_callback_event="start end join leave",
        )

        resp.append(dial)

        # Reorder agents starting from current_index for round robin
        ordered_agents = agents[current_index:] + agents[:current_index]
        agents_to_dial = []
        for agent in ordered_agents:
            agents_to_dial.append({
                "user": agent.get("user"),
                "mobile_no": agent.get("mobile_no"),
                "device": agent["call_receiving_device"],
            })

        frappe.cache().set_value(
            f"conf_agents_{conference_name}",
            {
                "agents": agents_to_dial,
                "caller_id": self.to_number,
                "from_number": self.from_number,
                "mode": "round_robin",
                "current_index": 0,
                "timeout": timeout,
            },
            expires_in_sec=300,
        )

        return resp

    def _handle_outside_hours(self, config):
        """Handle call when outside business hours."""
        resp = VoiceResponse()
        action = config.get("outside_hours_action", "Message")

        if action == "Voicemail" and config.get("voicemail_enabled"):
            return self._send_to_voicemail(config)
        elif action == "Message":
            message = config.get("outside_hours_message") or _(
                "We are currently closed. Please call back during business hours."
            )
            resp.say(message, language="en-US")
            resp.hangup()
        else:
            resp.hangup()

        return resp

    def _handle_no_agents(self, config):
        """Handle call when no agents are available."""
        resp = VoiceResponse()
        action = config.get("no_answer_action", "Voicemail")

        if action == "Voicemail" and config.get("voicemail_enabled"):
            return self._send_to_voicemail(config)
        elif action == "Message":
            resp.say(
                _("All agents are currently busy. Please try again later."),
                language="en-US",
            )
            resp.hangup()
        elif action == "Forward":
            # TODO: Implement forwarding to another number
            resp.say(
                _("All agents are currently busy. Please try again later."),
                language="en-US",
            )
            resp.hangup()
        else:
            resp.hangup()

        return resp

    def _send_to_voicemail(self, config):
        """Send caller to voicemail."""
        resp = VoiceResponse()

        # Play greeting
        greeting = config.get("voicemail_greeting") or _(
            "Please leave a message after the tone."
        )
        resp.say(greeting, language="en-US")

        # Record voicemail
        resp.record(
            max_length=120,
            action=get_public_url("/api/method/telephony.twilio.api.handle_voicemail"),
            transcribe=config.get("voicemail_transcribe", False),
            transcribe_callback=get_public_url("/api/method/telephony.twilio.api.handle_voicemail_transcription") if config.get("voicemail_transcribe") else None,
            play_beep=True,
        )

        return resp

    def _process_legacy(self):
        """Legacy call processing (fallback when no routing config exists)."""
        twilio = Twilio.connect()
        owners = get_twilio_number_owners(self.to_number)
        attender = get_the_call_attender(owners)

        if not attender:
            resp = VoiceResponse()
            resp.say(
                _("Agent is unavailable to take the call, please call after some time.")
            )
            return resp

        if attender["call_receiving_device"] == "Phone":
            return twilio.generate_twilio_dial_response(
                self.from_number, attender["mobile_no"]
            )
        else:
            return twilio.generate_twilio_client_response(
                twilio.safe_identity(attender["name"])
            )


def get_twilio_number_owners(phone_number):
    """Get list of users who is using the phone_number.
    >>> get_twilio_number_owners("+11234567890")
    {
            'owner1': {'name': '..', 'mobile_no': '..', 'call_receiving_device': '...'},
            'owner2': {....}
    }
    """
    # remove special characters from phone number and get only digits also remove white spaces
    # keep + sign in the number at start of the number
    phone_number = "".join([c for c in phone_number if c.isdigit() or c == "+"])
    user_voice_settings = frappe.get_all(
        "TP Telephony Agent",
        filters={"twilio_number": phone_number},
        fields=["name", "call_receiving_device"],
    )
    user_wise_voice_settings = {user["name"]: user for user in user_voice_settings}

    user_general_settings = frappe.get_all(
        "User",
        filters=[["name", "IN", user_wise_voice_settings.keys()]],
        fields=["name", "mobile_no"],
    )
    user_wise_general_settings = {user["name"]: user for user in user_general_settings}

    return merge_dicts(user_wise_general_settings, user_wise_voice_settings)


def get_active_loggedin_users(users):
    """Filter the current loggedin users from the given users list"""
    rows = frappe.db.sql(
        """
		SELECT `user`
		FROM `tabSessions`
		WHERE `user` IN %(users)s
		""",
        {"users": users},
    )
    return [row[0] for row in set(rows)]


def get_the_call_attender(owners):
    """Get attender details from list of owners"""
    if not owners:
        return
    current_loggedin_users = get_active_loggedin_users(list(owners.keys()))

    for name, details in owners.items():
        if (details["call_receiving_device"] == "Phone" and details["mobile_no"]) or (
            details["call_receiving_device"] == "Computer"
            and name in current_loggedin_users
        ):
            return details


class TwilioCallDetails:
    def __init__(self, call_info, call_from=None, call_to=None):
        self.call_info = call_info
        self.account_sid = call_info.get("AccountSid")
        self.application_sid = call_info.get("ApplicationSid")
        self.call_sid = call_info.get("CallSid")
        self.call_status = self.get_call_status(call_info.get("CallStatus"))
        self._call_from = call_from or call_info.get("From")
        self._call_to = call_to or call_info.get("To")

    def get_direction(self):
        if self.call_info.get("Caller").lower().startswith("client"):
            return "Outgoing"
        return "Incoming"

    def get_from_number(self):
        return self._call_from or self.call_info.get("From")

    def get_to_number(self):
        return self._call_to or self.call_info.get("To")

    @classmethod
    def get_call_status(cls, twilio_status):
        """Convert Twilio given status into system status."""
        twilio_status = twilio_status or ""
        return " ".join(twilio_status.split("-")).title()

    def to_dict(self):
        """Convert call details into dict."""
        direction = self.get_direction()
        from_number = self.get_from_number()
        to_number = self.get_to_number()
        caller = ""
        receiver = ""

        if direction == "Outgoing":
            caller = self.call_info.get("Caller")
            identity = caller.replace("client:", "").strip()
            caller = Twilio.emailid_from_identity(identity) if identity else ""
        else:
            owners = get_twilio_number_owners(to_number)
            attender = get_the_call_attender(owners)
            receiver = attender["name"] if attender else ""

        return {
            "type": direction,
            "status": self.call_status,
            "id": self.call_sid,
            "from": from_number,
            "to": to_number,
            "receiver": receiver,
            "caller": caller,
        }
