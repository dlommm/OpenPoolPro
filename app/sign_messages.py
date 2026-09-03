"""Admin-editable text for the e-sign flow: the sign-request email, the
signed-copy email, and the post-sign confirmation page.

Each is stored as a Setting (subject/body or heading/body). Tokens in
{curly_braces} are substituted from the contract at send/sign time; an
unknown token is left as-is rather than raising, so a typo can never 500 a
send. get_message() falls back to DEFAULTS for any unset key.
"""
import string

from app import models

# token -> human label, shown in the editor's "available fields" hint.
TOKENS = {
    "customer_name": "Customer name",
    "company_name": "Your company name",
    "season_year": "Season year",
    "property_address": "Service address",
    "sign_url": "Signing link (request email only)",
    "signer_name": "Signed name (signed email / confirmation only)",
    "signed_date": "Date signed (signed email / confirmation only)",
    "expiry_label": "Link lifetime, e.g. '7 days' (request email only)",
}

DEFAULTS = {
    "sign_email_subject": "Your {season_year} Pool Service Agreement",
    "sign_email_body": (
        "Attached is your {season_year} pool service agreement.\n\n"
        "You can review and sign it online here:\n\n"
        "  {sign_url}\n\n"
        "For security, this signing link expires in {expiry_label} and stops "
        "working once the agreement is signed. If it expires, just reply to "
        "this email and we'll send you a fresh one.\n\n"
        "Please review and let us know if you have any questions.\n\n"
        "{company_name}"),
    "signed_email_subject": "Signed: your {season_year} Pool Service Agreement",
    "signed_email_body": (
        "Thank you, {signer_name}!\n\n"
        "Attached is your signed {season_year} pool service agreement for your "
        "records.\n\n{company_name}"),
    "sign_done_heading": "Thank you — you're all set!",
    "sign_done_body": (
        "{signer_name} signed the {season_year} Pool Service Agreement on "
        "{signed_date}. {company_name} now has your signed copy on file."),
}


class _SafeDict(dict):
    def __missing__(self, key):  # leave unknown {tokens} untouched
        return "{" + key + "}"


def apply_tokens(text: str, ctx: dict) -> str:
    try:
        return string.Formatter().vformat(text or "", (), _SafeDict(ctx))
    except (ValueError, IndexError):
        # Malformed braces in admin text: show it literally rather than 500.
        return text or ""


def get_message(session, key: str) -> str:
    return models.get_setting(session, key, DEFAULTS.get(key, ""))


def resolved(session, key: str, ctx: dict) -> str:
    return apply_tokens(get_message(session, key), ctx)
