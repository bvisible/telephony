# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class TPCallConference(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from telephony.ftelephony.doctype.tp_conference_participant.tp_conference_participant import (
			TPConferenceParticipant,
		)

		called_number: DF.Data | None
		caller_number: DF.Data | None
		conference_name: DF.Data
		conference_sid: DF.Data | None
		created_at: DF.Datetime | None
		ended_at: DF.Datetime | None
		original_call_sid: DF.Data | None
		participants: DF.Table[TPConferenceParticipant]
		status: DF.Literal["Active", "On Hold", "Transferring", "Ended"]
	# end: auto-generated types

	def before_insert(self):
		if not self.created_at:
			self.created_at = now_datetime()

	def add_participant(
		self,
		call_sid: str,
		participant_type: str,
		phone_number: str = None,
		user: str = None,
		status: str = "Ringing",
	):
		"""Add a participant to the conference."""
		self.append(
			"participants",
			{
				"call_sid": call_sid,
				"participant_type": participant_type,
				"phone_number": phone_number,
				"user": user,
				"status": status,
				"joined_at": now_datetime() if status == "Connected" else None,
			},
		)
		self.save(ignore_permissions=True)

	def update_participant_status(self, call_sid: str, status: str):
		"""Update a participant's status."""
		for p in self.participants:
			if p.call_sid == call_sid:
				p.status = status
				if status == "Connected" and not p.joined_at:
					p.joined_at = now_datetime()
				elif status == "Left" and not p.left_at:
					p.left_at = now_datetime()
				elif status == "On Hold":
					p.hold_start = now_datetime()
				break
		self.save(ignore_permissions=True)

	def get_participant(self, call_sid: str = None, participant_type: str = None):
		"""Get a participant by call_sid or type."""
		for p in self.participants:
			if call_sid and p.call_sid == call_sid:
				return p
			if participant_type and p.participant_type == participant_type and p.status != "Left":
				return p
		return None

	def get_active_agent(self):
		"""Get the currently active agent."""
		for p in self.participants:
			if p.participant_type == "Agent" and p.status in ["Connected", "On Hold"]:
				return p
		return None

	def get_caller(self):
		"""Get the caller participant."""
		return self.get_participant(participant_type="Caller")

	def end_conference(self):
		"""Mark the conference as ended."""
		self.status = "Ended"
		self.ended_at = now_datetime()

		# Mark all remaining participants as left
		for p in self.participants:
			if p.status != "Left":
				p.status = "Left"
				p.left_at = now_datetime()

		self.save(ignore_permissions=True)

	def can_transfer(self) -> bool:
		"""Check if the conference can accept a transfer."""
		if self.status == "Ended":
			return False

		# Must have an active caller
		caller = self.get_caller()
		if not caller or caller.status == "Left":
			return False

		# Must have an active agent
		agent = self.get_active_agent()
		if not agent:
			return False

		return True


def get_active_conference_by_call_sid(call_sid: str):
	"""Find an active conference containing a specific call SID."""
	# First check if it's the original call
	conf_name = frappe.db.get_value(
		"TP Call Conference",
		{"original_call_sid": call_sid, "status": ["!=", "Ended"]},
		"name",
	)

	if conf_name:
		return frappe.get_doc("TP Call Conference", conf_name)

	# Check participants
	participant = frappe.db.get_value(
		"TP Conference Participant",
		{"call_sid": call_sid, "status": ["!=", "Left"]},
		["parent"],
		as_dict=True,
	)

	if participant:
		return frappe.get_doc("TP Call Conference", participant.parent)

	return None


def generate_conference_name(call_sid: str) -> str:
	"""Generate a unique conference name."""
	import time

	timestamp = int(time.time() * 1000)
	# Use last 8 chars of call_sid + timestamp for uniqueness
	short_sid = call_sid[-8:] if call_sid else "unknown"
	return f"conf_{short_sid}_{timestamp}"
