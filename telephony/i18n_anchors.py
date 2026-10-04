# //// Neoffice — added file (no upstream equivalent)
#
# A DocType field label lives in a DocType JSON file. The desk translates it with the DocType name as
# context (`__(df.label, null, df.parent)`): it looks up "<label>:<DocType name>" first and falls back to
# the bare label. The extractor that reads DocType JSON writes the bare label only, never the context,
# so a `msgctxt "<DocType name>"` entry in `locale/fr.po` that no `_()` call mentions is dropped as
# obsolete the next time `bench update-po-files` runs, and the screen silently falls back to the bare word.
#
# Why this context exists: all the apps' French catalogues are merged into one dictionary and the last app
# to translate a bare key wins. The bare key "Number" is translated "Nombre" (a quantity) by other apps,
# and here it is a phone number.
#
# This function is NEVER called. It exists so that `bench generate-pot-file` finds the string.

from frappe import _


def _i18n_anchors():
	"""Never called. See the comment at the top of the module."""
	# Data field "Number" of the child table TP Telephony Phone: a phone number.
	_("Number", context="TP Telephony Phone")
