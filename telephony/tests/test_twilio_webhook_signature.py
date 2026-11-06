# //// Neoffice — added file (no upstream equivalent)
"""The Twilio webhooks check the request signature: off, observe (log, serve) or enforce (refuse).

Twilio is the guest that calls these webhooks in real life, so the requests are played as Guest. A request is
built as nginx hands it to the site: plain HTTP to the host Twilio called, with X-Forwarded-Proto https
(neoffice-maintenance#2296, #2276).
"""

from unittest.mock import patch

import frappe
from frappe.tests.roles import as_role
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request
from frappe.utils.password import set_encrypted_password
from twilio.request_validator import RequestValidator

from telephony.twilio import api
from telephony.twilio.api import (
    handle_conference_participant,
    handle_conference_status,
    handle_dial_status,
    handle_sequential_dial,
    handle_voicemail,
    handle_voicemail_transcription,
    twilio_incoming_call_handler,
    update_call_status_info,
    update_recording_info,
    voice,
)

AUTH_TOKEN = "test-auth-token-0123456789abcdef"
HOST = "calls.example.com"
SITE_URL = "https://site.example.com"
PATH = "/api/method/telephony.twilio.api.twilio_incoming_call_handler"
CALL_SID = "CAtestsignature0000000000000000001"
FORM = {
    "CallSid": CALL_SID,
    "Caller": "+15550000000",
    "From": "+15550000000",
    "To": "+15550000001",
    "CallStatus": "ringing",
}
MISMATCH_TITLE = "Twilio webhook signature mismatch: twilio_incoming_call_handler"


class TestTwilioWebhookSignature(FrappeTestCase):
    def setUp(self):
        frappe.db.savepoint("tp_twilio_signature")
        frappe.db.set_single_value(
            "TP Twilio Settings",
            {"enabled": 1, "account_sid": "ACtest", "twiml_sid": "APtest", "webhook_signature_check": ""},
        )
        for field, value in {"auth_token": AUTH_TOKEN, "api_secret": "secret"}.items():
            set_encrypted_password("TP Twilio Settings", "TP Twilio Settings", value, field)
            frappe.db.set_single_value("TP Twilio Settings", field, "*" * len(value))
        for webhook in ("twilio_incoming_call_handler", "update_recording_info"):
            frappe.cache().delete_value(f"telephony:twilio_signature_mismatch:{webhook}")
        self.commit = patch.object(frappe.db, "commit")
        self.commit.start()

    def tearDown(self):
        self.commit.stop()
        frappe.db.rollback(save_point="tp_twilio_signature")
        frappe.local.request = None

    def set_mode(self, mode):
        frappe.db.set_single_value("TP Twilio Settings", "webhook_signature_check", mode)

    def make_request(self, sign_with=None, data=FORM, url=None, host=HOST, path=PATH):
        """A POST as nginx passes it on, signed by `sign_with` over FORM and the URL Twilio called."""
        url = url or f"https://{host}{path}"
        signature = RequestValidator(sign_with).compute_signature(url, FORM) if sign_with else ""
        set_request(
            method="POST",
            base_url=f"http://{host}",
            path=path,
            data=data,
            headers={"X-Forwarded-Proto": "https", "X-Twilio-Signature": signature},
        )

    def mismatch_rows(self):
        return frappe.get_all("Error Log", filters={"method": MISMATCH_TITLE}, pluck="name")

    def test_enforce_serves_a_request_signed_for_the_url_twilio_called(self):
        self.set_mode("Enforce")
        self.make_request(sign_with=AUTH_TOKEN)
        with as_role("Guest"):
            twilio_incoming_call_handler(**FORM)
        self.assertTrue(frappe.db.exists("TP Call Log", CALL_SID))

    def test_enforce_serves_a_callback_signed_for_the_sites_host_name(self):
        # The callbacks this app gives Twilio are built on host_name (get_public_url), whatever Host nginx sees.
        self.set_mode("Enforce")
        self.make_request(sign_with=AUTH_TOKEN, url=f"{SITE_URL}{PATH}", host="10.0.0.5")
        with patch.dict(frappe.local.conf, {"host_name": SITE_URL}), as_role("Guest"):
            twilio_incoming_call_handler(**FORM)
        self.assertTrue(frappe.db.exists("TP Call Log", CALL_SID))

    def test_enforce_refuses_unsigned_forged_or_tampered_requests(self):
        self.set_mode("Enforce")
        for token, data in [
            (None, FORM),
            ("wrong-token", FORM),
            (AUTH_TOKEN, {**FORM, "From": "+15559999999"}),
        ]:
            with self.subTest(token=token, data=data):
                self.make_request(sign_with=token, data=data)
                with as_role("Guest"), self.assertRaises(frappe.PermissionError):
                    twilio_incoming_call_handler(**data)
        self.assertFalse(frappe.db.exists("TP Call Log", CALL_SID))

    def test_observe_is_the_default_serves_and_logs_one_row_per_window(self):
        self.make_request()
        with as_role("Guest"):
            twilio_incoming_call_handler(**FORM)
        self.assertTrue(frappe.db.exists("TP Call Log", CALL_SID))
        self.assertEqual(len(self.mismatch_rows()), 1)

        second = {**FORM, "CallSid": CALL_SID[:-1] + "2"}
        self.make_request(data=second)
        with as_role("Guest"):
            twilio_incoming_call_handler(**second)
        self.assertTrue(frappe.db.exists("TP Call Log", second["CallSid"]))
        self.assertEqual(len(self.mismatch_rows()), 1, "a second mismatch in the window writes no row")

    def test_observe_logs_nothing_for_a_signed_request(self):
        self.set_mode("Observe")
        self.make_request(sign_with=AUTH_TOKEN)
        with as_role("Guest"):
            twilio_incoming_call_handler(**FORM)
        self.assertFalse(self.mismatch_rows())

    def test_off_serves_without_checking(self):
        self.set_mode("Off")
        self.make_request()
        with as_role("Guest"):
            twilio_incoming_call_handler(**FORM)
        self.assertTrue(frappe.db.exists("TP Call Log", CALL_SID))
        self.assertFalse(self.mismatch_rows())

    def test_every_webhook_checks_the_signature(self):
        self.set_mode("Enforce")
        webhooks = [
            voice,
            twilio_incoming_call_handler,
            update_recording_info,
            update_call_status_info,
            handle_dial_status,
            handle_sequential_dial,
            handle_voicemail,
            handle_voicemail_transcription,
            handle_conference_status,
            handle_conference_participant,
        ]
        self.make_request()
        with (
            patch("telephony.twilio.api.Twilio") as twilio,
            patch("telephony.twilio.api.create_call_log") as create_call_log,
            patch("telephony.twilio.api.update_call_log") as update_call_log,
        ):
            for webhook in webhooks:
                with self.subTest(webhook=webhook.__name__), as_role("Guest"):
                    with self.assertRaises(frappe.PermissionError):
                        webhook(**FORM)
        for mock in (twilio, create_call_log, update_call_log):
            self.assertFalse(mock.mock_calls)

    def test_a_filter_in_place_of_a_call_id_reaches_no_query(self):
        self.set_mode("Off")
        for sid in ("CAfilter0000000000000000000000001", "CAfilter0000000000000000000000002"):
            frappe.get_doc(
                {
                    "doctype": "TP Call Log",
                    "id": sid,
                    "telephony_medium": "Twilio",
                    "type": "Incoming",
                    "from": "+15550000000",
                    "to": "+15550000001",
                }
            ).insert(ignore_permissions=True)
        frappe.get_doc(
            {"doctype": "TP Voicemail", "call_sid": "CAvoicemail00000000000000000000001", "status": "New"}
        ).insert(ignore_permissions=True)
        conference = frappe.get_doc(
            {"doctype": "TP Call Conference", "conference_name": "conf_filter_1", "status": "Active"}
        ).insert(ignore_permissions=True)

        self.make_request()
        with as_role("Guest"):
            update_recording_info(CallSid={"name": ["like", "CAfilter%"]}, RecordingUrl="https://x.example/r")
            handle_voicemail_transcription(
                CallSid=["like", "CAvoicemail%"], TranscriptionStatus="completed", TranscriptionText="forged"
            )
            handle_conference_status(StatusCallbackEvent="conference-end", FriendlyName={"status": "Active"})

        self.assertFalse(
            frappe.get_all("TP Call Log", filters={"recording_url": "https://x.example/r"}, pluck="name")
        )
        self.assertFalse(frappe.db.get_value("TP Voicemail", {"call_sid": "CAvoicemail00000000000000000000001"}, "transcription"))
        self.assertEqual(frappe.db.get_value("TP Call Conference", conference.name, "status"), "Active")

    def test_a_transcription_is_stored_as_text(self):
        self.set_mode("Enforce")
        sid = "CAvoicemail00000000000000000000002"
        frappe.get_doc({"doctype": "TP Voicemail", "call_sid": sid, "status": "New"}).insert(
            ignore_permissions=True
        )
        form = {
            "CallSid": sid,
            "TranscriptionStatus": "completed",
            "TranscriptionText": '<img src=x onerror="alert(1)"> call me back',
        }
        path = "/api/method/telephony.twilio.api.handle_voicemail_transcription"
        url = f"https://{HOST}{path}"
        set_request(
            method="POST",
            base_url=f"http://{HOST}",
            path=path,
            data=form,
            headers={
                "X-Forwarded-Proto": "https",
                "X-Twilio-Signature": RequestValidator(AUTH_TOKEN).compute_signature(url, form),
            },
        )
        with as_role("Guest"):
            handle_voicemail_transcription(**form)
        stored = frappe.db.get_value("TP Voicemail", {"call_sid": sid}, "transcription")
        self.assertIn("call me back", stored)
        self.assertNotIn("<img", stored)

    def test_the_mismatch_row_names_the_urls_tried_and_no_value(self):
        self.make_request(data={**FORM, "From": "+15558887777"})
        with as_role("Guest"):
            twilio_incoming_call_handler(**{**FORM, "From": "+15558887777"})
        row = frappe.get_doc("Error Log", self.mismatch_rows()[0])
        self.assertIn(f"https://{HOST}{PATH}", row.error)
        self.assertIn("From", row.error)
        self.assertNotIn("+15558887777", row.error)

    def test_the_urls_tried_include_the_request_and_the_host_name(self):
        set_request(method="POST", base_url=f"http://{HOST}", path=PATH, headers={"X-Forwarded-Proto": "https"})
        with patch.dict(frappe.local.conf, {"host_name": SITE_URL}):
            urls = api.get_request_urls()
        self.assertIn(f"https://{HOST}{PATH}", urls)
        self.assertIn(f"{SITE_URL}{PATH}", urls)
