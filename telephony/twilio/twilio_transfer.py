# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""
Twilio Call Transfer API

Provides cold transfer (blind) and warm transfer (attended) functionality
using Conference-based architecture.
"""

import frappe
from frappe import _

from .twilio_handler import Twilio
from telephony.ftelephony.doctype.tp_call_conference.tp_call_conference import (
    get_active_conference_by_call_sid,
)


@frappe.whitelist()
def get_conference_status(call_sid: str = None, conference_name: str = None):
    """Get the current status of a conference.

    Args:
        call_sid: The call SID of a participant in the conference
        conference_name: The conference name (optional if call_sid provided)

    Returns:
        dict with conference info and participants
    """
    try:
        if conference_name:
            if not frappe.db.exists("TP Call Conference", conference_name):
                return {"ok": False, "error": "Conference not found"}
            conf_doc = frappe.get_doc("TP Call Conference", conference_name)
        elif call_sid:
            conf_doc = get_active_conference_by_call_sid(call_sid)
            if not conf_doc:
                return {"ok": False, "error": "No active conference found for this call"}
        else:
            return {"ok": False, "error": "Either call_sid or conference_name required"}

        participants = []
        for p in conf_doc.participants:
            participants.append({
                "call_sid": p.call_sid,
                "type": p.participant_type,
                "user": p.user,
                "phone_number": p.phone_number,
                "status": p.status,
                "joined_at": str(p.joined_at) if p.joined_at else None,
                "hold_start": str(p.hold_start) if p.hold_start else None,
            })

        return {
            "ok": True,
            "conference": {
                "name": conf_doc.conference_name,
                "sid": conf_doc.conference_sid,
                "status": conf_doc.status,
                "caller_number": conf_doc.caller_number,
                "called_number": conf_doc.called_number,
                "created_at": str(conf_doc.created_at) if conf_doc.created_at else None,
            },
            "participants": participants,
            "can_transfer": conf_doc.can_transfer(),
        }
    except Exception as e:
        frappe.log_error("Get Conference Status Error", str(e))
        return {"ok": False, "error": str(e)}


@frappe.whitelist()
def cold_transfer(call_sid: str, transfer_to: str, transfer_type: str = "user"):
    """Perform a cold (blind) transfer.

    The current agent is disconnected immediately and the caller is connected
    to the transfer target.

    Args:
        call_sid: The call SID of the current call (agent's call)
        transfer_to: The destination - user email or phone number
        transfer_type: "user" for internal user, "phone" for external number

    Returns:
        dict with success status
    """
    try:
        # Find the active conference
        conf_doc = get_active_conference_by_call_sid(call_sid)
        if not conf_doc:
            return {"ok": False, "error": "No active conference found"}

        if not conf_doc.can_transfer():
            return {"ok": False, "error": "Conference cannot accept transfer"}

        twilio = Twilio.connect()
        if not twilio:
            return {"ok": False, "error": "Twilio not configured"}

        conference_name = conf_doc.conference_name
        caller_id = conf_doc.called_number  # Use original called number as caller ID

        # Update conference status
        conf_doc.status = "Transferring"
        conf_doc.save(ignore_permissions=True)

        # Get current agent's call SID
        current_agent = conf_doc.get_active_agent()
        if not current_agent:
            return {"ok": False, "error": "No active agent found"}

        agent_call_sid = current_agent.call_sid

        # Remove current agent from conference
        twilio.remove_participant(conference_name, agent_call_sid)
        conf_doc.update_participant_status(agent_call_sid, "Left")

        # Dial the transfer target into the conference
        if transfer_type == "user":
            # Get user's device preference
            agent_settings = frappe.db.get_value(
                "TP Telephony Agent",
                transfer_to,
                ["call_receiving_device", "twilio_number"],
                as_dict=True,
            )

            if not agent_settings:
                return {"ok": False, "error": f"User {transfer_to} not found"}

            user_data = frappe.db.get_value("User", transfer_to, "mobile_no")

            if agent_settings.call_receiving_device == "Phone":
                if not user_data:
                    return {"ok": False, "error": "User has no mobile number configured"}
                participant = twilio.dial_into_conference(
                    conference_name=conference_name,
                    to=user_data,
                    caller_id=caller_id,
                    is_client=False,
                )
                phone_number = user_data
            else:
                participant = twilio.dial_into_conference(
                    conference_name=conference_name,
                    to=transfer_to,
                    caller_id=caller_id,
                    is_client=True,
                )
                phone_number = None

            if participant:
                conf_doc.add_participant(
                    call_sid=participant.call_sid,
                    participant_type="Transfer Target",
                    phone_number=phone_number,
                    user=transfer_to,
                    status="Ringing",
                )

        elif transfer_type == "phone":
            # Transfer to external phone number
            participant = twilio.dial_into_conference(
                conference_name=conference_name,
                to=transfer_to,
                caller_id=caller_id,
                is_client=False,
            )

            if participant:
                conf_doc.add_participant(
                    call_sid=participant.call_sid,
                    participant_type="Transfer Target",
                    phone_number=transfer_to,
                    status="Ringing",
                )
        else:
            return {"ok": False, "error": f"Invalid transfer_type: {transfer_type}"}

        frappe.db.commit()

        return {
            "ok": True,
            "message": "Cold transfer initiated",
            "conference": conference_name,
        }

    except Exception as e:
        frappe.log_error("Cold Transfer Error", str(e))
        return {"ok": False, "error": str(e)}


@frappe.whitelist()
def warm_transfer_initiate(call_sid: str, transfer_to: str, transfer_type: str = "user"):
    """Initiate a warm (attended) transfer - Step 1.

    Puts the caller on hold and dials the transfer target.
    The current agent can speak with the transfer target before completing.

    Args:
        call_sid: The call SID of the current call (agent's call)
        transfer_to: The destination - user email or phone number
        transfer_type: "user" for internal user, "phone" for external number

    Returns:
        dict with transfer_call_sid to track the new call
    """
    try:
        # Find the active conference
        conf_doc = get_active_conference_by_call_sid(call_sid)
        if not conf_doc:
            return {"ok": False, "error": "No active conference found"}

        if not conf_doc.can_transfer():
            return {"ok": False, "error": "Conference cannot accept transfer"}

        twilio = Twilio.connect()
        if not twilio:
            return {"ok": False, "error": "Twilio not configured"}

        conference_name = conf_doc.conference_name
        caller_id = conf_doc.called_number

        # Put caller on hold
        caller = conf_doc.get_caller()
        if caller and caller.call_sid:
            twilio.hold_participant(conference_name, caller.call_sid, hold=True)
            conf_doc.update_participant_status(caller.call_sid, "On Hold")

        # Update conference status
        conf_doc.status = "Transferring"
        conf_doc.save(ignore_permissions=True)

        # Dial the transfer target
        if transfer_type == "user":
            agent_settings = frappe.db.get_value(
                "TP Telephony Agent",
                transfer_to,
                ["call_receiving_device", "twilio_number"],
                as_dict=True,
            )

            if not agent_settings:
                return {"ok": False, "error": f"User {transfer_to} not found"}

            user_data = frappe.db.get_value("User", transfer_to, "mobile_no")

            if agent_settings.call_receiving_device == "Phone":
                if not user_data:
                    return {"ok": False, "error": "User has no mobile number configured"}
                participant = twilio.dial_into_conference(
                    conference_name=conference_name,
                    to=user_data,
                    caller_id=caller_id,
                    is_client=False,
                )
                phone_number = user_data
            else:
                participant = twilio.dial_into_conference(
                    conference_name=conference_name,
                    to=transfer_to,
                    caller_id=caller_id,
                    is_client=True,
                )
                phone_number = None

            if participant:
                conf_doc.add_participant(
                    call_sid=participant.call_sid,
                    participant_type="Transfer Target",
                    phone_number=phone_number,
                    user=transfer_to,
                    status="Ringing",
                )
                transfer_call_sid = participant.call_sid
            else:
                return {"ok": False, "error": "Failed to dial transfer target"}

        elif transfer_type == "phone":
            participant = twilio.dial_into_conference(
                conference_name=conference_name,
                to=transfer_to,
                caller_id=caller_id,
                is_client=False,
            )

            if participant:
                conf_doc.add_participant(
                    call_sid=participant.call_sid,
                    participant_type="Transfer Target",
                    phone_number=transfer_to,
                    status="Ringing",
                )
                transfer_call_sid = participant.call_sid
            else:
                return {"ok": False, "error": "Failed to dial transfer target"}
        else:
            return {"ok": False, "error": f"Invalid transfer_type: {transfer_type}"}

        frappe.db.commit()

        return {
            "ok": True,
            "message": "Warm transfer initiated - caller on hold",
            "conference": conference_name,
            "transfer_call_sid": transfer_call_sid,
        }

    except Exception as e:
        frappe.log_error("Warm Transfer Initiate Error", str(e))
        return {"ok": False, "error": str(e)}


@frappe.whitelist()
def warm_transfer_complete(call_sid: str):
    """Complete a warm transfer - Step 2.

    The current agent leaves the conference, and the caller is taken off hold
    to speak with the transfer target.

    Args:
        call_sid: The call SID of the current agent's call

    Returns:
        dict with success status
    """
    try:
        conf_doc = get_active_conference_by_call_sid(call_sid)
        if not conf_doc:
            return {"ok": False, "error": "No active conference found"}

        twilio = Twilio.connect()
        if not twilio:
            return {"ok": False, "error": "Twilio not configured"}

        conference_name = conf_doc.conference_name

        # Get current agent
        current_agent = conf_doc.get_active_agent()
        if not current_agent:
            return {"ok": False, "error": "No active agent found"}

        # Remove current agent from conference
        twilio.remove_participant(conference_name, current_agent.call_sid)
        conf_doc.update_participant_status(current_agent.call_sid, "Left")

        # Take caller off hold
        caller = conf_doc.get_caller()
        if caller and caller.status == "On Hold":
            twilio.hold_participant(conference_name, caller.call_sid, hold=False)
            conf_doc.update_participant_status(caller.call_sid, "Connected")

        # Update conference status
        conf_doc.status = "Active"
        conf_doc.save(ignore_permissions=True)
        frappe.db.commit()

        return {
            "ok": True,
            "message": "Warm transfer completed",
            "conference": conference_name,
        }

    except Exception as e:
        frappe.log_error("Warm Transfer Complete Error", str(e))
        return {"ok": False, "error": str(e)}


@frappe.whitelist()
def warm_transfer_cancel(call_sid: str):
    """Cancel a warm transfer in progress.

    Removes the transfer target and takes the caller off hold.

    Args:
        call_sid: The call SID of the current agent's call

    Returns:
        dict with success status
    """
    try:
        conf_doc = get_active_conference_by_call_sid(call_sid)
        if not conf_doc:
            return {"ok": False, "error": "No active conference found"}

        twilio = Twilio.connect()
        if not twilio:
            return {"ok": False, "error": "Twilio not configured"}

        conference_name = conf_doc.conference_name

        # Find and remove transfer target
        transfer_target = conf_doc.get_participant(participant_type="Transfer Target")
        if transfer_target and transfer_target.status != "Left":
            twilio.remove_participant(conference_name, transfer_target.call_sid)
            conf_doc.update_participant_status(transfer_target.call_sid, "Left")

        # Take caller off hold
        caller = conf_doc.get_caller()
        if caller and caller.status == "On Hold":
            twilio.hold_participant(conference_name, caller.call_sid, hold=False)
            conf_doc.update_participant_status(caller.call_sid, "Connected")

        # Update conference status
        conf_doc.status = "Active"
        conf_doc.save(ignore_permissions=True)
        frappe.db.commit()

        return {
            "ok": True,
            "message": "Warm transfer cancelled",
            "conference": conference_name,
        }

    except Exception as e:
        frappe.log_error("Warm Transfer Cancel Error", str(e))
        return {"ok": False, "error": str(e)}


@frappe.whitelist()
def hold_call(call_sid: str, hold: bool = True):
    """Put a call on hold or resume it.

    Args:
        call_sid: The call SID to hold/resume
        hold: True to hold, False to resume

    Returns:
        dict with success status
    """
    try:
        conf_doc = get_active_conference_by_call_sid(call_sid)
        if not conf_doc:
            return {"ok": False, "error": "No active conference found"}

        twilio = Twilio.connect()
        if not twilio:
            return {"ok": False, "error": "Twilio not configured"}

        conference_name = conf_doc.conference_name

        # Find the participant to hold
        participant = conf_doc.get_participant(call_sid=call_sid)
        if not participant:
            # If agent's call_sid passed, hold the caller instead
            caller = conf_doc.get_caller()
            if caller:
                participant = caller
            else:
                return {"ok": False, "error": "Participant not found"}

        success = twilio.hold_participant(
            conference_name,
            participant.call_sid,
            hold=hold,
        )

        if success:
            new_status = "On Hold" if hold else "Connected"
            conf_doc.update_participant_status(participant.call_sid, new_status)
            frappe.db.commit()
            return {
                "ok": True,
                "message": f"Call {'held' if hold else 'resumed'}",
                "status": new_status,
            }
        else:
            return {"ok": False, "error": "Failed to update hold status"}

    except Exception as e:
        frappe.log_error("Hold Call Error", str(e))
        return {"ok": False, "error": str(e)}


@frappe.whitelist()
def get_available_transfer_targets():
    """Get list of users available to receive transfers.

    Returns:
        list of users with their availability status
    """
    try:
        # Get all telephony agents
        agents = frappe.get_all(
            "TP Telephony Agent",
            filters={"enabled": 1},
            fields=["name", "user", "twilio_number", "call_receiving_device"],
        )

        result = []
        current_user = frappe.session.user

        for agent in agents:
            # Skip current user
            if agent.user == current_user:
                continue

            # Get user info
            user_info = frappe.db.get_value(
                "User",
                agent.user,
                ["full_name", "mobile_no", "user_image"],
                as_dict=True,
            )

            if user_info:
                # Check if user is online (has active session)
                is_online = frappe.db.exists(
                    "Sessions",
                    {"user": agent.user},
                )

                result.append({
                    "user": agent.user,
                    "full_name": user_info.full_name,
                    "mobile_no": user_info.mobile_no,
                    "user_image": user_info.user_image,
                    "device": agent.call_receiving_device,
                    "is_online": bool(is_online),
                })

        # Sort by online status (online first), then by name
        result.sort(key=lambda x: (not x["is_online"], x["full_name"]))

        return {"ok": True, "targets": result}

    except Exception as e:
        frappe.log_error("Get Transfer Targets Error", str(e))
        return {"ok": False, "error": str(e)}
