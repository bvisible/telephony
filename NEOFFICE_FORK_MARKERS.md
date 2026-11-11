# Neoffice fork markers

Everything Neoffice changed in this fork carries a `//// Neoffice` comment saying **why**
(`grep -rn "////"` maps the whole divergence). This file holds what a comment cannot reach: files with no
comment syntax. Read it before merging upstream.

Tooling: `bvisible/neoffice-ci` → `scripts/fork_markers.py` (`check --base <BASE> --head <HEAD>`).

## telephony

Fork `bvisible/telephony`, branch `version-15`. Upstream `frappe/telephony` (default branch `develop`).

Neoffice added files carry the header `//// Neoffice — added file (no upstream equivalent)`. Several of them
predate that rule and have no header yet: the call routing, conference, voicemail and phone number
DocTypes (`tp_call_conference`, `tp_conference_participant`, `tp_voicemail`, `tp_phone_number_config`,
`tp_routing_agent`, `tp_business_hours`, `tp_agent_phone_assignment`) and the call routing, conference
and cost code of `telephony/twilio/api.py` and `twilio_handler.py`. Upstream has none of them.

### Files with no comment syntax

| Path | Why |
|---|---|
| `telephony/ftelephony/doctype/tp_twilio_settings/tp_twilio_settings.json` | Adds `webhook_signature_check` (Enforce / Observe / Off, default Enforce, an empty value enforces): how `telephony.twilio.api.validate_twilio_request` treats a webhook whose Twilio signature does not verify. Upstream refuses always (its `validate_twilio_request`, 2026-10); we keep Observe as a choice, because a wrong URL refuses every incoming call and only a real call proves it (neoffice-maintenance#2276, #2296). The default was Observe until 2026-11; the patch `telephony.patches.v15_0.enforce_twilio_signature_by_default` writes Enforce where the value is empty (neoffice-maintenance#893). |
| `telephony/ftelephony/doctype/tp_exotel_settings/tp_exotel_settings.json` | `webhook_verify_token` is a Password field, not Data: it is the only credential of the guest Exotel webhook, and every account that may read the settings (role All) read it in clear. The patch `telephony.patches.v15_0.encrypt_telephony_settings_secrets` moves a saved value into `__Auth` (neoffice-maintenance#2296). |

### Upstream fixes to absorb at the next merge

Upstream `develop` validates the Twilio webhook signature in `telephony/twilio/api.py`
(`validate_twilio_request`, `get_request_url`, commits `f75cc11`, `af7b6ac`, `5e1c70c`) and refuses
every request that does not verify. Ours has the same names and adds the Off / Observe / Enforce switch:
keep ours, and take their test file `telephony/twilio/test_api.py` as a second check.
