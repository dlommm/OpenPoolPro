# Reviewed QuickBooks billing

OpenPoolPro collects completed service work into monthly billing drafts. An owner or a user with **Can approve and send QuickBooks billing** reviews the charges and authorizes QuickBooks to create and email the official invoice. QuickBooks handles hosted payment collection. Invoice balances, payments, and credits are reconciled back into OpenPoolPro.

## Setup

1. Connect the intended QuickBooks company in **Settings → QuickBooks Online**. Start with an Intuit sandbox. The current billing workflow supports United States companies with USD home currency; enabling billing verifies these fields with QuickBooks.
2. Open **Settings → QuickBooks Online → Review customer sync**. It plans both directions from reads and writes nothing until you apply: missing customers are created, exact name matches are linked rather than duplicated, empty QuickBooks fields are filled automatically, and anything that would overwrite an existing QuickBooks value or rename a customer waits for per-field approval. The same page imports the QuickBooks roster into OpenPoolPro when QuickBooks already holds your customers. **QuickBooks billing → Verify existing QuickBooks customer matches** remains for confirming one ID by hand. Review the remote customer name and email. A match is tied to the connected company and environment; an old unverified ID is never adopted automatically.
3. Billing items resolve themselves. A line posts to an item chosen by the **kind** of work it is — weekly service labor, a job's template ("Green-to-clean recovery"), a job type for ad-hoc work, or the individual chemical used. Approving a draft creates any item that does not exist yet and links any that already does; the draft page lists what approving will add before you approve it. Set the income account new items post to once, in Settings, and billing never sends you back there. Customers resolve the same way: an exact QuickBooks name match links, a genuinely absent customer is created, and a near match stops and sends you to the customer review rather than risking a duplicate.

   Items are keyed on the kind, never on a label, because OpenPoolPro cannot delete a QuickBooks record — an item minted from an edited job title would be permanent. Renaming a chemical or template in OpenPoolPro keeps billing to the item already created for it.

4. Load QuickBooks items and save the service and chemical item mappings. If the company has no suitable items, **Don't have these items? Create them** makes the two service items against an income account you choose and maps them; items that already carry those names are linked rather than duplicated, and an existing inventory or inactive item of the same name is refused instead of mapped. Mappings must belong to the connected company. Configure customer tax treatment, addresses, and QuickBooks Payments in QuickBooks.
5. Disable automatic invoice delivery in QuickBooks. This is a required operator check: the invoice API's `EmailStatus` is not a verified override for every company automation. Validate this setup in the sandbox before enabling production billing.
6. Open **QuickBooks billing → Billing setup**, confirm the setup, and enable approved delivery. Configure any additional approvers under **Users**. Staff with `view_money` can prepare and edit unsubmitted drafts; approving or retrying delivery requires `approve_billing` or an administrator account.

## Monthly workflow

- Choose the month and prepare drafts. Completed, eligible work produces customer drafts from the existing visit/job labor and chemical billing sources, with service and chemical charges, source dates, recipient, due date, tax estimate, and total. Periods already reserved for local invoices or QuickBooks billing are skipped.
- Open each draft. Review zero-price warnings and confirm any work intended to be free. Edit descriptions and charge amounts, save, and review the recalculated total. Hold an entire customer's draft when it needs investigation. Discard an unsubmitted draft and prepare again if newly completed work must be collected.
- Select **Approve and send for invoicing**. Approval freezes the saved charges and recipient and records the approver and timestamp. Unsaved browser edits are not part of approval. Stale revisions are rejected.
- QuickBooks creates the invoice. OpenPoolPro compares its returned details against the approved snapshot before requesting email delivery. A difference stops delivery and exposes the official PDF, line amounts, tax, and total for review. Approving the revised total is bound to exactly the displayed QuickBooks snapshot.
- Known retryable creation failures retain the request identifier and frozen request body. An unknown creation outcome is searched for in QuickBooks; it is not blindly recreated. Uncertain email delivery is not blindly retried. Sync to confirm whether QuickBooks sent it; exceptional unresolved accounting or delivery cases require checking QuickBooks.

## Seeing payments in OpenPoolPro

With the scheduler enabled (`RUN_SCHEDULER=1`), connected companies reconcile every five minutes. Previously approved pending submissions are processed only while billing delivery is enabled; reconciliation continues when delivery is disabled. **Sync now** also refreshes records.

The billing page, customer profile, accounts receivable page, and customer portal display locally retained invoices, remaining balances, and known payment/credit allocations. The portal shows only the customer's invoices whose QuickBooks email delivery is confirmed. Payment and credit history retains dates and references supplied by QuickBooks. Unknown allocations are labeled rather than reported as paid cash. Balances older than 30 minutes are marked potentially stale.

Official invoice PDFs are downloaded on first access and then cached locally, allowing subsequent viewing during a connection outage. Payment links appear only when QuickBooks supplies a supported HTTPS Intuit link. The absence of a link is not proof that payments are enabled or disabled; verify the company payment setup.

QuickBooks records are separate from legacy local invoices and payments. Local payment-entry and regeneration endpoints do not mutate QuickBooks invoices. Current-company financial summaries do not combine sandbox and production records. Switching companies retains previous local mirror records but does not include them in the new company's balances.

## Operational limits and validation

- This is periodic reconciliation, not a guarantee of instant balances while offline. A disconnected or failing QuickBooks company needs reconnection or correction in Settings.
- QuickBooks is authoritative for remote invoice balances. Credits and payment allocations are retained separately from actual received cash. Unavailable/deleted remote invoices are flagged rather than silently discarded.
- Preparation reserves the customer/month across local and QuickBooks billing. Drafts collect existing work as snapshots; they are not continuously rebuilt behind an approver's back.
- Customer and item mapping setup, account payment eligibility, country/currency, tax differences, partial payments, credits, reversals, and ambiguous delivery must be verified in the actual sandbox/company configuration before production use.
- Work through [the sandbox test plan](quickbooks-sandbox-testing.md) end to end before enabling production billing. It covers connection, mappings, the approve/create/deliver path, the mismatch gate, payments and credits, the portal, and company isolation, with a results log and a go/no-go checklist.
- Automated tests use mocked QuickBooks transport and isolated databases. They do not send customer email or move money. Initial setup, OAuth reconnection, refunds/accounting corrections, and uncertain delivery may still require QuickBooks access.
