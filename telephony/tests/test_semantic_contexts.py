# //// Neoffice — added file (no upstream equivalent)
"""Words that mean something else in another app carry their sense with them (static test).

All the apps' `locale/fr.po` are merged into ONE French dictionary: when two apps translate the
same bare English key differently, the app loaded last wins and the other one shows the wrong
French word. The desk translates a DocType field label as `__(label, null, <DocType name>)`: it looks
up "label:DocType name" first and only then falls back to the bare label, so a `msgctxt` equal to the
DocType name cannot hurt another app or another language.

This reads `locale/fr.po` and the sources as text: no Frappe site, no import of the app, so it runs
on its own with `python3 -m unittest` (path of this file) as well as under `bench run-tests`.
"""

import json
import unittest
from pathlib import Path

from babel.messages.pofile import read_po

APP = Path(__file__).resolve().parents[1]  # <repo>/telephony  (the app package)

# (msgid, context or None, expected French)
PO_ENTRIES = (
	# "Number" of a phone in the agent's phone table. Bare "Number" is a quantity ("Nombre") in other apps
	# and wins the merged dictionary.
	("Number", "TP Telephony Phone", "Numéro"),
)


def _catalog():
	with open(APP / "locale" / "fr.po", "rb") as handle:
		return read_po(handle)


class TestSemanticContexts(unittest.TestCase):
	def test_the_catalogue_carries_each_entry(self):
		catalog = _catalog()
		for msgid, context, expected in PO_ENTRIES:
			message = catalog.get(msgid, context)
			self.assertIsNotNone(message, f"fr.po has no entry for {msgid!r} (context {context!r})")
			self.assertEqual(message.string, expected, f"{msgid!r} (context {context!r})")

	def test_the_context_is_the_doctype_whose_label_it_translates(self):
		# The desk passes the DocType name as context: the entry only means something while the field
		# still carries the label "Number" in a DocType of that exact name.
		path = APP / "ftelephony" / "doctype" / "tp_telephony_phone" / "tp_telephony_phone.json"
		doctype = json.loads(path.read_text(encoding="utf-8"))
		self.assertEqual(doctype["name"], "TP Telephony Phone")
		labels = {field["fieldname"]: field.get("label") for field in doctype["fields"]}
		self.assertEqual(labels["number"], "Number")

	def test_the_anchor_keeps_the_entry_in_the_pot(self):
		# `update-po-files` drops a msgctxt entry that no call mentions: the JSON extractor writes no context.
		source = (APP / "i18n_anchors.py").read_text(encoding="utf-8")
		self.assertIn('_("Number", context="TP Telephony Phone")', source)


if __name__ == "__main__":
	unittest.main()
