# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TPConferenceParticipant(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		call_sid: DF.Data | None
		hold_start: DF.Datetime | None
		joined_at: DF.Datetime | None
		left_at: DF.Datetime | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		participant_type: DF.Literal["Caller", "Agent", "Transfer Target"]
		phone_number: DF.Data | None
		status: DF.Literal["Ringing", "Connected", "On Hold", "Left"]
		user: DF.Link | None
	# end: auto-generated types

	pass
