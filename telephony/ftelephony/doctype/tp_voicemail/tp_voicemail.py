# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TPVoicemail(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		call_sid: DF.Data | None
		duration: DF.Int
		from_number: DF.Data | None
		recording_url: DF.Data | None
		status: DF.Literal["New", "Listened", "Archived"]
		to_number: DF.Data | None
		transcription: DF.SmallText | None
	# end: auto-generated types

	pass
