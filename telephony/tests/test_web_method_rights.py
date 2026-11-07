# //// Neoffice — added file (no upstream equivalent)
"""The telephony web methods answer the staff who handle calls, and refuse everyone else.

Each refusal is played by the account that should be refused (a portal customer, a desk user with no telephony
role, an agent who is not on the call) and sits next to the legitimate caller it must keep serving: the agent on
the call, the telephony manager, the user the call log names (neoffice-maintenance#2296).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.roles import as_role, make_persona
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import set_encrypted_password

from telephony import api
from telephony.exotel import handler as exotel
from telephony.twilio import api as twilio_api
from telephony.twilio import twilio_transfer

AGENT = "tp-test-agent@yopmail.com"
OTHER_AGENT = "tp-test-other-agent@yopmail.com"
MANAGER = "tp-test-manager@yopmail.com"
LINE_ONLY = "tp-test-line-only@yopmail.com"
CRM_LINE = "tp-test-crm-line@yopmail.com"
HELPDESK_AGENT = "tp-test-helpdesk-agent@yopmail.com"
CONTACT_PHONE = "+41 21 555 01 23"


def make_user(email: str, roles: list[str]) -> str:  # role-check: ok the scenery: accounts the tests act as
    user = frappe.get_doc("User", email) if frappe.db.exists("User", email) else frappe.new_doc("User")
    if user.is_new():
        user.update({"email": email, "first_name": email.split("@")[0], "send_welcome_email": 0})
    user.enabled = 1
    user.set("roles", [{"role": role} for role in roles])
    user.save(ignore_permissions=True)
    frappe.clear_cache(user=email)
    return email


class TestTelephonyWebMethodRights(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        make_user(AGENT, ["TP Agent"])
        make_user(OTHER_AGENT, ["TP Agent"])
        make_user(MANAGER, ["TP Manager"])
        # A desk account with a line the softphone gave it, and no telephony role.
        make_user(LINE_ONLY, ["Blogger"] if frappe.db.exists("Role", "Blogger") else [])
        frappe.db.set_value("User", LINE_ONLY, "user_type", "System User")
        for user, line in ((AGENT, {"twilio": 1}), (LINE_ONLY, {"twilio": 1})):
            if not frappe.db.exists("TP Telephony Agent", user):
                frappe.get_doc({"doctype": "TP Telephony Agent", "user": user, **line}).insert(
                    ignore_permissions=True
                )
            frappe.db.set_value("TP Telephony Agent", user, line)

    def setUp(self):
        frappe.db.savepoint("tp_rights")
        # Several methods commit their work: here it stays inside the savepoint.
        self.commit = patch.object(frappe.db, "commit")
        self.commit.start()

    def tearDown(self):
        self.commit.stop()
        frappe.db.rollback(save_point="tp_rights")

    # ------------------------------------------------------------------ call logs

    def make_call_log(self, sid, **values):
        return frappe.get_doc(
            {
                "doctype": "TP Call Log",
                "id": sid,
                "telephony_medium": "Manual",
                "type": "Incoming",
                "from": "+41215550000",
                "to": "+41215550001",
                **values,
            }
        ).insert(ignore_permissions=True)

    def test_get_call_log_refuses_an_account_with_no_part_in_the_call(self):
        call = self.make_call_log("CArights000000000000000000000001", receiver=AGENT)
        for persona in ("portal_customer", "sales_user"):
            with self.subTest(persona=persona), as_role(persona):
                with self.assertRaises(frappe.PermissionError):
                    api.get_call_log(call.name)

    def test_get_call_log_serves_the_agents_and_the_people_on_the_call(self):
        call = self.make_call_log("CArights000000000000000000000002", receiver=AGENT)
        with as_role(OTHER_AGENT):
            self.assertEqual(api.get_call_log(call.name)["name"], call.name)
        with as_role("sales_user") as user:
            frappe.db.set_value("TP Call Log", call.name, "receiver", user)
            frappe.clear_document_cache("TP Call Log", call.name)
            self.assertEqual(api.get_call_log(call.name)["name"], call.name)

    def test_get_call_log_serves_whoever_reads_the_document_it_was_filed_on(self):
        customer = frappe.get_all("Customer", limit=1, pluck="name")
        if not customer:
            self.skipTest("no Customer on this site")
        call = self.make_call_log(
            "CArights000000000000000000000003",
            links=[{"link_doctype": "Customer", "link_name": customer[0]}],
        )
        with as_role("sales_user"):
            self.assertEqual(api.get_call_log(call.name)["name"], call.name)
        with as_role("portal_customer"), self.assertRaises(frappe.PermissionError):
            api.get_call_log(call.name)

    def test_get_call_log_takes_a_name_not_a_filter(self):
        with as_role(AGENT), self.assertRaises((TypeError, frappe.ValidationError)):
            api.get_call_log({"name": ["like", "%"]})

    def test_create_call_log_needs_the_right_to_create_one(self):
        args = dict(
            telephony_medium="Manual",
            from_number="+41215550000",
            to_number="+41215550001",
            duration=10,
            status="Completed",
            call_type="Outgoing",
            caller=AGENT,
            receiver=None,
            links=[],
        )
        with as_role("portal_customer"), self.assertRaises(frappe.PermissionError):
            api.create_call_log(id="CArights000000000000000000000004", **args)
        with as_role(AGENT):
            created = api.create_call_log(id="CArights000000000000000000000005", **args)
        self.assertEqual(created.caller, AGENT)

    # ------------------------------------------------------------------ logging a call on a document

    def logged_call(self, links, caller=None):
        return dict(
            telephony_medium="Manual",
            from_number="+41215550000",
            to_number="+41215550001",
            duration=10,
            status="Completed",
            call_type="Outgoing",
            caller=caller,
            receiver=None,
            links=links,
        )

    def test_a_desk_account_logs_a_call_on_a_document_it_may_write_in_its_own_name(self):
        user = make_persona("sales_user")
        own = frappe.get_doc({"doctype": "ToDo", "description": "Call back", "allocated_to": user}).insert(
            ignore_permissions=True
        )
        with as_role("sales_user"):
            created = api.create_call_log(
                id="CArights000000000000000000000006",
                **self.logged_call([{"link_doctype": "ToDo", "link_name": own.name}], caller=AGENT),
            )
        self.assertEqual(created.caller, user, "the call is in the name of whoever logs it")
        self.assertIn(("ToDo", own.name), [(row.link_doctype, row.link_name) for row in created.links])

    def test_a_call_is_not_logged_on_a_document_one_may_not_write(self):
        user = make_persona("sales_user")
        theirs = frappe.get_doc(
            {"doctype": "ToDo", "description": "Someone else's", "allocated_to": MANAGER}
        ).insert(ignore_permissions=True)
        own = frappe.get_doc({"doctype": "ToDo", "description": "Mine", "allocated_to": user}).insert(
            ignore_permissions=True
        )
        refused = {
            "a document of someone else": [{"link_doctype": "ToDo", "link_name": theirs.name}],
            "no document": [],
            "a contact only": [{"link_doctype": "Contact", "link_name": "x"}],
            "no such doctype": [{"link_doctype": "No Such Doctype", "link_name": "x"}],
            "one of the documents": [
                {"link_doctype": "ToDo", "link_name": own.name},
                {"link_doctype": "ToDo", "link_name": theirs.name},
            ],
        }
        for n, (case, links) in enumerate(refused.items()):
            with self.subTest(case=case), as_role("sales_user"), self.assertRaises(frappe.PermissionError):
                api.create_call_log(id=f"CArights0000000000000000000001{n:02d}", **self.logged_call(links))
        portal = make_persona("portal_customer")
        portal_todo = frappe.get_doc(
            {"doctype": "ToDo", "description": "Portal", "allocated_to": portal}
        ).insert(ignore_permissions=True)
        with as_role("portal_customer"), self.assertRaises(frappe.PermissionError):
            api.create_call_log(
                id="CArights000000000000000000000199",
                **self.logged_call([{"link_doctype": "ToDo", "link_name": portal_todo.name}]),
            )

    def test_a_helpdesk_agent_logs_a_call_on_a_ticket(self):
        if not (frappe.db.exists("DocType", "HD Ticket") and frappe.db.exists("DocType", "HD Agent")):
            self.skipTest("helpdesk is not installed on this site")
        ticket = frappe.get_all("HD Ticket", limit=1, pluck="name")
        if not ticket:
            self.skipTest("no HD Ticket on this site")
        make_user(HELPDESK_AGENT, ["Agent"])
        if not frappe.db.exists("HD Agent", HELPDESK_AGENT):
            frappe.get_doc(
                {"doctype": "HD Agent", "user": HELPDESK_AGENT, "agent_name": "Telephony Test", "is_active": 1}
            ).insert(ignore_permissions=True)
        frappe.db.set_single_value("HD Settings", "restrict_tickets_by_agent_group", 0)
        links = [{"link_doctype": "HD Ticket", "link_name": ticket[0]}]
        with as_role(HELPDESK_AGENT):
            created = api.create_call_log(
                id="CArights000000000000000000000200", **self.logged_call(links, caller=AGENT)
            )
        self.assertEqual(created.caller, HELPDESK_AGENT)
        with as_role("sales_user"), self.assertRaises(frappe.PermissionError):
            api.create_call_log(id="CArights000000000000000000000201", **self.logged_call(links))

    # ------------------------------------------------------------------ contact lookup

    def test_get_contact_by_phone_number_tells_a_contact_to_staff_only(self):
        contact = frappe.get_doc(
            {
                "doctype": "Contact",
                "first_name": "Telephony",
                "last_name": "Rights",
                "phone_nos": [{"phone": CONTACT_PHONE, "is_primary_phone": 1}],
            }
        ).insert(ignore_permissions=True)
        for user in (AGENT, LINE_ONLY, "sales_user"):
            with self.subTest(user=user), as_role(user):
                self.assertEqual(api.get_contact_by_phone_number(CONTACT_PHONE).get("name"), contact.name)
        with as_role("portal_customer"):
            found = api.get_contact_by_phone_number(CONTACT_PHONE)
        self.assertNotIn("name", found)
        self.assertEqual(found, {"mobile_no": CONTACT_PHONE})

    # ------------------------------------------------------------------ call control

    def make_conference(self, name="conf_rights_1"):
        conference = frappe.get_doc(
            {
                "doctype": "TP Call Conference",
                "conference_name": name,
                "status": "Active",
                "caller_number": "+41215550000",
                "called_number": "+41215550009",
                "participants": [
                    {"call_sid": f"{name}_caller", "participant_type": "Caller", "status": "Connected"},
                    {
                        "call_sid": f"{name}_agent",
                        "participant_type": "Agent",
                        "user": AGENT,
                        "status": "Connected",
                    },
                ],
            }
        )
        return conference.insert(ignore_permissions=True)

    def test_call_control_refuses_whoever_is_not_on_the_call(self):
        conference = self.make_conference()
        calls = {
            "hold_call": lambda: twilio_transfer.hold_call(call_sid="conf_rights_1_agent"),
            "cold_transfer": lambda: twilio_transfer.cold_transfer(
                call_sid="conf_rights_1_agent", transfer_to="+41215550999", transfer_type="phone"
            ),
            "warm_transfer_initiate": lambda: twilio_transfer.warm_transfer_initiate(
                call_sid="conf_rights_1_agent", transfer_to="+41215550999", transfer_type="phone"
            ),
            "warm_transfer_complete": lambda: twilio_transfer.warm_transfer_complete(call_sid="conf_rights_1_agent"),
            "warm_transfer_cancel": lambda: twilio_transfer.warm_transfer_cancel(call_sid="conf_rights_1_agent"),
            "get_conference_status": lambda: twilio_transfer.get_conference_status(
                conference_name=conference.name
            ),
        }
        with patch.object(twilio_transfer, "Twilio") as twilio:
            for user in (OTHER_AGENT, "portal_customer", "sales_user"):
                for name, call in calls.items():
                    with self.subTest(user=user, method=name), as_role(user):
                        with self.assertRaises(frappe.PermissionError):
                            call()
        self.assertFalse(twilio.mock_calls, "nothing reached Twilio")

    def test_call_control_serves_the_agent_on_the_call_and_the_manager(self):
        self.make_conference()
        fake = MagicMock()
        fake.hold_participant.return_value = True
        with patch.object(twilio_transfer.Twilio, "connect", return_value=fake):
            with as_role(AGENT):
                held = twilio_transfer.hold_call(call_sid="conf_rights_1_agent")
                status = twilio_transfer.get_conference_status(call_sid="conf_rights_1_agent")
            with as_role(MANAGER):
                resumed = twilio_transfer.hold_call(call_sid="conf_rights_1_agent", hold=False)
        self.assertTrue(held["ok"], held)
        self.assertTrue(status["ok"], status)
        self.assertTrue(resumed["ok"], resumed)
        fake.hold_participant.assert_called()

    def test_a_line_in_crm_telephony_agent_counts_as_a_line(self):
        if not (
            frappe.db.exists("DocType", "CRM Telephony Agent")
            and frappe.db.has_column("CRM Telephony Agent", "twilio")
        ):
            self.skipTest("no CRM Telephony Agent with a Twilio line on this site")
        make_user(CRM_LINE, ["Blogger"] if frappe.db.exists("Role", "Blogger") else [])
        frappe.db.set_value("User", CRM_LINE, "user_type", "System User")
        if not frappe.db.exists("CRM Telephony Agent", {"user": CRM_LINE}):
            frappe.get_doc({"doctype": "CRM Telephony Agent", "user": CRM_LINE}).insert(ignore_permissions=True)
        name = frappe.db.get_value("CRM Telephony Agent", {"user": CRM_LINE})

        def line(on):
            frappe.db.sql("update `tabCRM Telephony Agent` set twilio=%s where name=%s", (on, name))

        line(0)
        with as_role(CRM_LINE), self.assertRaises(frappe.PermissionError):
            twilio_transfer.get_available_transfer_targets()
        line(1)
        with as_role(CRM_LINE):
            self.assertIn("ok", twilio_transfer.get_available_transfer_targets())
        frappe.db.set_value("User", CRM_LINE, "user_type", "Website User")
        with as_role(CRM_LINE), self.assertRaises(frappe.PermissionError):
            twilio_transfer.get_available_transfer_targets()

    def test_transfer_targets_are_for_the_staff_who_take_calls(self):
        with as_role("portal_customer"), self.assertRaises(frappe.PermissionError):
            twilio_transfer.get_available_transfer_targets()
        for user in (AGENT, LINE_ONLY):
            with self.subTest(user=user), as_role(user):
                self.assertIn("ok", twilio_transfer.get_available_transfer_targets())

    def test_the_transfer_targets_are_the_colleagues_with_a_twilio_line(self):
        # The list read an `enabled` field the agent does not have, and `Sessions` as a DocType: it answered
        # ok False to everyone, so no transfer could pick a colleague.
        with as_role(LINE_ONLY):
            answer = twilio_transfer.get_available_transfer_targets()
        self.assertTrue(answer["ok"], answer)
        users = {target["user"] for target in answer["targets"]}
        self.assertIn(AGENT, users)
        self.assertNotIn(LINE_ONLY, users, "not oneself")

    def test_a_transfer_from_the_mobile_app_names_its_destination_target(self):
        # The phone sends {call_sid, target}, the desk {call_sid, transfer_to}: played through frappe.call, which
        # binds the arguments as the web handler does.
        fake = MagicMock()
        fake.dial_into_conference.return_value = SimpleNamespace(call_sid="CAtransfer000000000000000000000001")
        fake.hold_participant.return_value = True
        with patch.object(twilio_transfer.Twilio, "connect", return_value=fake):
            for method in ("cold_transfer", "warm_transfer_initiate"):
                conference = self.make_conference(name=f"conf_mobile_{method}")
                with self.subTest(method=method), as_role(AGENT):
                    answer = frappe.call(
                        f"telephony.twilio.twilio_transfer.{method}",
                        call_sid=f"{conference.conference_name}_agent",
                        target="+41215550999",
                        transfer_type="phone",
                    )
                    self.assertTrue(answer["ok"], answer)
                    self.assertEqual(fake.dial_into_conference.call_args.kwargs["to"], "+41215550999")

    # ------------------------------------------------------------------ the managers' maintenance

    def test_twilio_maintenance_is_the_managers(self):
        with patch.object(twilio_api.Twilio, "connect", return_value=None), patch.object(
            twilio_api.Twilio, "get_twilio_client"
        ) as client:
            client.return_value.applications.list.return_value = [SimpleNamespace(friendly_name="App")]
            for user in (AGENT, "portal_customer"):
                with self.subTest(user=user), as_role(user):
                    with self.assertRaises(frappe.PermissionError):
                        twilio_api.recalculate_call_costs()
                    with self.assertRaises(frappe.PermissionError):
                        twilio_api.fetch_applications()
            with as_role(MANAGER):
                self.assertEqual(twilio_api.recalculate_call_costs(), {"error": "Twilio not connected"})
                self.assertEqual(twilio_api.fetch_applications(), ["App"])

    # ------------------------------------------------------------------ Exotel

    def test_make_a_call_is_a_post(self):
        self.assertEqual(list(frappe.allowed_http_methods_for_whitelisted_func[exotel.make_a_call]), ["POST"])

    def test_make_a_call_uses_the_callers_own_line(self):
        frappe.db.set_value(
            "TP Telephony Agent", AGENT, {"exotel": 1, "mobile_no": "+41790000001", "exotel_number": "EXO1"}
        )
        response = MagicMock()
        response.json.return_value = {"Call": {"Sid": "EXrights0001", "From": "+41790000001", "To": "+41215550001"}}
        with (
            patch.object(exotel, "is_integration_enabled", return_value=True),
            patch.object(exotel, "get_exotel_endpoint", return_value="https://api.exotel.com/x"),
            patch.object(exotel, "get_all_exophones", return_value=["EXO1", "EXO2"]),
            patch.object(exotel, "get_status_updater_url", return_value="https://site.example.com/cb"),
            patch.object(exotel.requests, "post", return_value=response) as post,
            patch.object(exotel, "create_call_log"),
        ):
            for user in ("portal_customer", "sales_user", OTHER_AGENT):
                with self.subTest(user=user), as_role(user), self.assertRaises(frappe.PermissionError):
                    exotel.make_a_call(to_number="+41215550001")
            with as_role(AGENT), self.assertRaises(frappe.PermissionError):
                exotel.make_a_call(to_number="+41215550001", from_number="+41790000999")
            self.assertFalse(post.called, "no refused call reached Exotel")

            with as_role(AGENT):
                exotel.make_a_call(to_number="+41215550001")
            self.assertEqual(post.call_args.kwargs["data"]["From"], "+41790000001")
            with as_role(MANAGER):
                exotel.make_a_call(to_number="+41215550001", from_number="+41790000999", caller_id="EXO2")
            self.assertEqual(post.call_args.kwargs["data"]["From"], "+41790000999")

    def test_the_exotel_webhook_key_is_checked_before_and_after_the_patch(self):
        token = "exotel-webhook-key-0123456789"
        for stored in ("clear", "encrypted"):
            with self.subTest(stored=stored):
                if stored == "clear":  # a site the patch has not reached yet
                    frappe.db.set_single_value("TP Exotel Settings", "webhook_verify_token", token)
                else:
                    set_encrypted_password("TP Exotel Settings", "TP Exotel Settings", token, "webhook_verify_token")
                    frappe.db.set_single_value("TP Exotel Settings", "webhook_verify_token", "*" * len(token))
                for key, accepted in ((token, True), ("wrong", False), ("*" * len(token), False)):
                    frappe.local.request = SimpleNamespace(args={"key": key})
                    with as_role("Guest"):
                        if accepted:
                            exotel.validate_request()
                        else:
                            with self.assertRaises(frappe.PermissionError):
                                exotel.validate_request()
        frappe.local.request = None

    def test_an_exotel_call_event_goes_to_the_agent_of_the_call_only(self):
        with patch.object(frappe, "publish_realtime") as publish:
            exotel._publish_call_event({"CallSid": "EX1", "AgentEmail": AGENT, "CallFrom": "+4121", "key": "s"})
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(publish.call_args.kwargs["user"], AGENT)
        self.assertNotIn("key", publish.call_args.args[1])

    def test_the_exotel_subdomain_is_an_exotel_api_host(self):
        settings = frappe.get_doc("TP Exotel Settings")
        settings.enabled = 0
        for host in ("internal-host:8443/x?", "attacker.example/", "169.254.169.254", "exotel.com.evil.example"):
            settings.subdomain = host
            with self.subTest(host=host), self.assertRaises(frappe.ValidationError):
                settings.validate_subdomain()
        for host in ("api.exotel.com", "API.in.exotel.com "):
            settings.subdomain = host
            settings.validate_subdomain()
