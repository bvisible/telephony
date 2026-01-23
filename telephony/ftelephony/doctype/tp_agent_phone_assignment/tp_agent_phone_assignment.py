# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TPAgentPhoneAssignment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		can_listen_recordings: DF.Check
		friendly_name: DF.Data | None
		is_primary: DF.Check
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		phone_number: DF.Data
		ring_delay: DF.Int
	# end: auto-generated types

	pass
