# Verification notes

The automated unit suite uses reserved example addresses and mocks for external
operations. It covers MIME/base64 parsing, folded lines, identity selection,
preservation of other attendees and event metadata, rejection of unsupported
messages, progress output, and failure before mutation.

A separate local test used the official Nextcloud 35.0.2 Apache image and Mailpit
on an internal Docker network. The test instance had synthetic users and a
throwaway calendar; outbound mail could only reach the local SMTP sink.

Observed results:

| Case | Result |
| --- | --- |
| Save an attachment in NeoMutt, import unchanged, sync | No acceptance reply |
| Set own PARTSTAT before import, then sync | Correct METHOD:REPLY to organizer |
| New accept, tentative and decline | Correct reply for each status |
| Repeat an already confirmed response | No duplicate reply |
| Existing event without LAST-MODIFIED | Correct reply after conditional metadata repair |
| Change accepted to declined | One new reply with the changed status |
| Acknowledge a newer organizer revision with the same status | One reply; repeat produces none |
| Whole recurring invitation | Reply sent; recurrence rule preserved |
| Original NeoMutt interactive flow | Prompt and action completed |

The current attachment-only UI was additionally exercised through the real
NeoMutt `l` → attachment → `l`/Enter path for both `text/calendar` and
`application/ics`, with multiple configured identities. Those UI tests cancelled
the operation before any sync.

These observations do not certify every Nextcloud release or every invitation
producer. Detached exceptions and incoming cancellation/reply processing remain
explicit limitations. A server scheduling receipt does not prove delivery into
an external recipient's inbox.

When adding integration tests, use an isolated instance and an SMTP sink. A
shared production calendar is not a test fixture. No real invitations or private
deployment evidence are included in this repository.
