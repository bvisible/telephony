# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TPBusinessHours(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		day_of_week: DF.Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
		end_time: DF.Time
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		start_time: DF.Time
	# end: auto-generated types

	pass
