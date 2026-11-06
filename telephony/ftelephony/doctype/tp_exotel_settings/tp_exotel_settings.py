# Copyright (c) 2025, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re  # //// Neoffice — see EXOTEL_API_HOST

import frappe
import requests
from frappe import _
from frappe.model.document import Document

# //// Neoffice — the API host of an Exotel account (api.exotel.com, api.in.exotel.com…). The field went as typed
# into the URL of the server's requests, so it could point them at any host, port and path
# (neoffice-maintenance#2296).
EXOTEL_API_HOST = re.compile(r"(?:[a-z0-9-]+\.)+exotel\.(?:com|in)")


class TPExotelSettings(Document):
    def validate(self):
        self.validate_subdomain()  # //// Neoffice — see EXOTEL_API_HOST
        self.verify_credentials()

    # //// Neoffice — see EXOTEL_API_HOST.
    def validate_subdomain(self):
        if not self.subdomain:
            return
        self.subdomain = self.subdomain.strip().lower()
        if not EXOTEL_API_HOST.fullmatch(self.subdomain):
            frappe.throw(
                _("The subdomain must be the API host of your Exotel account, such as api.exotel.com"),
                title=_("Invalid subdomain"),
            )

    def verify_credentials(self):
        if self.enabled:
            response = requests.get(
                "https://{subdomain}/v1/Accounts/{sid}".format(
                    subdomain=self.subdomain, sid=self.account_sid
                ),
                auth=(self.api_key, self.get_password("api_token")),
                timeout=15,  # //// Neoffice — a host that never answers held a web worker
            )
            if response.status_code != 200:
                frappe.throw(
                    _(
                        f"Please enter valid exotel Account SID, API key & API token: {response.reason}"
                    ),
                    title=_("Invalid credentials"),
                )
