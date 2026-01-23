# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class TPPhoneNumberConfig(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from telephony.ftelephony.doctype.tp_business_hours.tp_business_hours import TPBusinessHours
		from telephony.ftelephony.doctype.tp_routing_agent.tp_routing_agent import TPRoutingAgent

		business_hours: DF.Table[TPBusinessHours]
		friendly_name: DF.Data | None
		is_active: DF.Check
		max_ring_time: DF.Int
		no_answer_action: DF.Literal["Voicemail", "Hangup", "Forward", "Message"]
		outside_hours_action: DF.Literal["Voicemail", "Hangup", "Message"]
		outside_hours_message: DF.SmallText | None
		phone_number: DF.Data
		recording_channels: DF.Literal["dual", "record-from-answer-dual", "record-from-ringing-dual"]
		recording_enabled: DF.Check
		recording_trim_silence: DF.Check
		ring_mode: DF.Literal["Simultaneous", "Sequential", "Round Robin"]
		ring_timeout: DF.Int
		routing_agents: DF.Table[TPRoutingAgent]
		use_business_hours: DF.Check
		voicemail_email: DF.Data | None
		voicemail_enabled: DF.Check
		voicemail_greeting: DF.SmallText | None
		voicemail_transcribe: DF.Check
	# end: auto-generated types

	def validate(self):
		self.validate_phone_number()
		self.validate_agents()
		self.validate_business_hours()

	def validate_phone_number(self):
		"""Ensure phone number is in E.164 format."""
		if self.phone_number and not self.phone_number.startswith("+"):
			self.phone_number = f"+{self.phone_number}"

	def validate_agents(self):
		"""Validate routing agents configuration."""
		if not self.routing_agents:
			frappe.msgprint(
				_("No routing agents configured. Calls to this number will go to voicemail."),
				indicator="orange",
				alert=True,
			)

		# Validate priorities are unique
		priorities = [a.priority for a in self.routing_agents if a.priority]
		if len(priorities) != len(set(priorities)):
			frappe.throw(_("Agent priorities must be unique"))

	def validate_business_hours(self):
		"""Validate business hours configuration."""
		if self.use_business_hours and not self.business_hours:
			frappe.throw(_("Please configure business hours or disable the option"))

	def get_available_agents(self):
		"""Get list of agents who can receive calls right now.

		Returns:
			list: List of agent dicts with user, ring_delay, and availability info
		"""
		available = []

		for agent_row in self.routing_agents:
			if not agent_row.is_enabled:
				continue

			# Get agent details
			agent = frappe.get_doc("TP Telephony Agent", agent_row.agent)

			if not agent.is_available:
				continue

			available.append({
				"user": agent.user,
				"user_name": agent.user_name,
				"priority": agent_row.priority,
				"ring_delay": agent_row.ring_delay,
				"call_receiving_device": agent.call_receiving_device,
				"mobile_no": agent.mobile_no,
			})

		# Sort by priority (lower = higher priority)
		available.sort(key=lambda x: x["priority"])

		return available

	def is_within_business_hours(self):
		"""Check if current time is within configured business hours.

		Returns:
			bool: True if within business hours or if business hours not enabled
		"""
		if not self.use_business_hours:
			return True

		from datetime import datetime

		now = datetime.now()
		current_day = now.strftime("%A")  # Monday, Tuesday, etc.
		current_time = now.time()

		for hours in self.business_hours:
			if hours.day_of_week == current_day:
				start = datetime.strptime(hours.start_time, "%H:%M").time()
				end = datetime.strptime(hours.end_time, "%H:%M").time()

				if start <= current_time <= end:
					return True

		return False


def get_routing_config(phone_number: str) -> dict:
	"""Get routing configuration for a phone number.

	Args:
		phone_number: The phone number to get config for

	Returns:
		dict: Configuration or None if not found
	"""
	# Normalize phone number
	if phone_number and not phone_number.startswith("+"):
		phone_number = f"+{phone_number}"

	if not frappe.db.exists("TP Phone Number Config", phone_number):
		return None

	config = frappe.get_doc("TP Phone Number Config", phone_number)

	if not config.is_active:
		return None

	return {
		"phone_number": config.phone_number,
		"friendly_name": config.friendly_name,
		"ring_mode": config.ring_mode,
		"ring_timeout": config.ring_timeout,
		"max_ring_time": config.max_ring_time,
		"no_answer_action": config.no_answer_action,
		"voicemail_enabled": config.voicemail_enabled,
		"voicemail_greeting": config.voicemail_greeting,
		"recording_enabled": config.recording_enabled,
		"recording_channels": config.recording_channels,
		"within_business_hours": config.is_within_business_hours(),
		"outside_hours_action": config.outside_hours_action,
		"outside_hours_message": config.outside_hours_message,
		"available_agents": config.get_available_agents(),
	}
