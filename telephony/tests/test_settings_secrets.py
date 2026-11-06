# //// Neoffice — added file (no upstream equivalent)
"""The secrets of the telephony settings stay out of tabSingles and the Versions, and the call code still reads them.

tabSingles is what frappe.client.get_single_value returns to every account that may read the settings (role All
on both), and a Version keeps both sides of a change. The scenery is a site as the old code left it: secrets
written in clear with db.set_single_value. The patch is run twice: the second run changes nothing
(neoffice-maintenance#2296).
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.client import get_single_value
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from telephony.patches.v15_0 import encrypt_telephony_settings_secrets, escape_voicemail_transcriptions
from telephony.twilio.twilio_handler import Twilio

AUTH_TOKEN = "throwaway-auth-token-0123456789ab"
API_SECRET = "throwaway-api-secret-0123456789ab"
EXOTEL_TOKEN = "throwaway-exotel-token-0123"


def tab_singles(doctype: str, field: str):
    return frappe.db.sql("select value from tabSingles where doctype=%s and field=%s", (doctype, field))[0][0]


def is_mask(value) -> bool:
    return bool(value) and set(value) == {"*"}


class TestTelephonySettingsSecrets(FrappeTestCase):
    def setUp(self):
        frappe.db.savepoint("tp_secrets")

    def tearDown(self):
        frappe.db.rollback(save_point="tp_secrets")

    def leave_clear_secrets(self):
        """The site as the old code left it: the values in clear in tabSingles, and in a Version."""
        frappe.db.set_single_value(
            "TP Twilio Settings",
            {"enabled": 1, "account_sid": "ACthrowaway", "auth_token": AUTH_TOKEN, "api_secret": API_SECRET},
        )
        frappe.db.set_single_value("TP Exotel Settings", "webhook_verify_token", EXOTEL_TOKEN)
        version = frappe.get_doc(
            {
                "doctype": "Version",
                "ref_doctype": "TP Twilio Settings",
                "docname": "TP Twilio Settings",
                "data": frappe.as_json(
                    {
                        "changed": [
                            ["api_secret", None, API_SECRET],
                            ["auth_token", "*****", "*" * len(AUTH_TOKEN)],
                            ["app_name", None, "Frappe Telephony"],
                        ]
                    }
                ),
            }
        ).insert(ignore_permissions=True)
        return version.name

    def test_the_patch_moves_clear_secrets_and_masks_the_versions(self):
        version = self.leave_clear_secrets()
        self.assertEqual(tab_singles("TP Twilio Settings", "api_secret"), API_SECRET)

        encrypt_telephony_settings_secrets.execute()

        for doctype, field, value in (
            ("TP Twilio Settings", "auth_token", AUTH_TOKEN),
            ("TP Twilio Settings", "api_secret", API_SECRET),
            ("TP Exotel Settings", "webhook_verify_token", EXOTEL_TOKEN),
        ):
            with self.subTest(field=field):
                self.assertTrue(is_mask(tab_singles(doctype, field)))
                self.assertEqual(get_decrypted_password(doctype, doctype, field), value)
        changed = {row[0]: row for row in json.loads(frappe.db.get_value("Version", version, "data"))["changed"]}
        self.assertTrue(is_mask(changed["api_secret"][2]))
        self.assertEqual(changed["app_name"][2], "Frappe Telephony")
        self.assertNotIn(API_SECRET, frappe.db.get_value("Version", version, "data"))

        before = {f: tab_singles("TP Twilio Settings", f) for f in ("auth_token", "api_secret")}
        encrypt_telephony_settings_secrets.execute()
        self.assertEqual({f: tab_singles("TP Twilio Settings", f) for f in before}, before)
        self.assertEqual(get_decrypted_password("TP Twilio Settings", "TP Twilio Settings", "api_secret"), API_SECRET)

    def test_the_call_code_still_reads_the_secrets_after_the_patch(self):
        self.leave_clear_secrets()
        encrypt_telephony_settings_secrets.execute()
        with patch("telephony.twilio.twilio_handler.TwilioClient") as client:
            twilio = Twilio.connect()
        self.assertEqual(twilio.api_secret, API_SECRET)
        client.assert_called_with("ACthrowaway", AUTH_TOKEN)
        self.assertEqual(frappe.get_doc("TP Twilio Settings").get_password("auth_token"), AUTH_TOKEN)

    def test_a_reader_of_the_settings_gets_masks_only(self):
        # A desk account reads both settings: role All in the app, Desk User where neoffice_custom_fields
        # narrowed the open grants.
        self.leave_clear_secrets()
        encrypt_telephony_settings_secrets.execute()
        with as_role("sales_user"):
            for doctype, field in (
                ("TP Twilio Settings", "auth_token"),
                ("TP Twilio Settings", "api_secret"),
                ("TP Exotel Settings", "webhook_verify_token"),
            ):
                with self.subTest(field=field):
                    self.assertTrue(is_mask(get_single_value(doctype, field)))

    def test_a_new_api_key_is_stored_encrypted(self):
        frappe.db.set_single_value("TP Twilio Settings", {"enabled": 1, "account_sid": "ACthrowaway"})
        set_encrypted_password("TP Twilio Settings", "TP Twilio Settings", AUTH_TOKEN, "auth_token")
        settings = frappe.get_doc("TP Twilio Settings")
        settings.api_key = settings.api_secret = None
        settings.new_sid = True
        twilio = MagicMock()
        twilio.new_keys.create.return_value = SimpleNamespace(sid="SKthrowaway", secret=API_SECRET)
        settings.set_api_credentials(twilio)

        self.assertEqual(settings.api_key, "SKthrowaway")
        self.assertTrue(is_mask(settings.api_secret), "the document keeps a mask, so its Version does too")
        self.assertTrue(is_mask(tab_singles("TP Twilio Settings", "api_secret")))
        self.assertEqual(settings.get_password("api_secret"), API_SECRET)
        with patch("telephony.twilio.twilio_handler.TwilioClient"):
            self.assertEqual(Twilio.connect().api_secret, API_SECRET)

    def test_stored_transcriptions_are_escaped_once(self):
        name = frappe.get_doc(
            {
                "doctype": "TP Voicemail",
                "call_sid": "CAsecrets000000000000000000000001",
                "status": "New",
            }
        ).insert(ignore_permissions=True).name
        frappe.db.set_value("TP Voicemail", name, "transcription", '<img src=x onerror="alert(1)"> hello')
        escape_voicemail_transcriptions.execute()
        once = frappe.db.get_value("TP Voicemail", name, "transcription")
        self.assertNotIn("<", once)
        self.assertIn("hello", once)
        escape_voicemail_transcriptions.execute()
        self.assertEqual(frappe.db.get_value("TP Voicemail", name, "transcription"), once)
