# QuickBooks transport contract

The accounting client uses minor version 75, explicit invoice creation and a
separate send request. The application never retries a send automatically.
Network errors, malformed success bodies, HTTP 408 and server errors on mutations
have an unknown outcome; reconcile before deciding whether another attempt is safe.
A 401 clears the access cache for the next explicit attempt. Refreshes serialize
within the application's single process, and cache entries bind to credentials,
environment and company. Rotated refresh tokens persist immediately.

Create callers must persist their approved body and stable request ID before
calling the API. Intuit request IDs are not a substitute for permanent local
transaction mappings or reconciliation after a long interruption. Customer create
bodies are frozen locally so retries cannot change a request's meaning.

Invoice creation sets `EmailStatus=NotSet`. This is not a verified override of
QuickBooks company automations. Before enabling billing, verify that automatic
invoice emails are disabled and exercise creation, tax calculation and delivery in
the connected sandbox/company configuration. Online payment links depend on the
company having online payments configured and a valid invoice billing email.
OpenPoolPro does not collect card numbers or implement a separate Payments API
charge workflow.

Full, paginated Invoice/Payment/CreditMemo scans are available, including invoice
links. An invalid/missing query envelope raises rather than masquerading as an
empty account. A repeated page raises so callers cannot treat partial data as a
complete reconciliation. Callers must only remove stale local mirrors after a
complete successful scan. PDF downloads are bounded to 20 MiB and checked for a
PDF signature.

Primary references reviewed:

- [Intuit PHP SDK DataService](https://github.com/intuit/QuickBooks-V3-PHP-SDK/blob/master/src/DataService/DataService.php): Add, FindById/include, SendEmail (`sendTo`, POST, octet-stream), PDF retrieval.
- [Intuit support: invoice payment link](https://help.developer.intuit.com/s/question/0D5TR000002nScJ0AU/how-we-can-get-payment-link-of-a-specific-invoice-from-where-can-we-get-payment-link-through-api-using-php-sdk-): `include=invoiceLink`, online payment and email prerequisites.
- [Intuit maintained sample](https://github.com/IntuitDeveloper/SampleApp-Projects-Java): current sample minor version 75.
- [Intuit invoice reference](https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/invoice): dynamic reference landing page; content could not be retrieved in this environment. This is not evidence that company-level automatic delivery can be suppressed by the API.

No live invoice creation, email delivery, or customer payment was performed during
implementation. Mocked transport tests verify wire format and failure handling;
connected-account acceptance testing remains necessary before production enablement.
