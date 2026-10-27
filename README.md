# neomutt-nextcloud-rsvp

Answer meeting invitations from NeoMutt's attachment menu. **Nextcloud sends the
calendar reply; NeoMutt stays your mail client.** No extra calendar shortcut,
browser switch, or second SMTP reply is needed.

In a message, open the attachment list, select `invite.ics`, and open it. Choose
accept, tentative, or decline. The helper updates your own attendee status,
imports the event with khal, and synchronizes the configured collection with
vdirsyncer. Two short progress lines show the calendar sync and response sync.

The interactive messages are currently in German: `1 Annehmen`, `2 Unter
Vorbehalt`, `3 Ablehnen`; an empty answer cancels. Internationalization is not
implemented yet.

## What this solves

Saving an invitation into a calendar is not the same as accepting it. An unchanged
`NEEDS-ACTION` invitation can be imported and synced without sending a reply.
This helper deliberately sets **only your** `ATTENDEE` participation status and
lets Nextcloud's CalDAV scheduling generate the standards-based `METHOD:REPLY`.
It checks the server's scheduling result instead of assuming that an import means
an invitation was answered.

## Requirements

- Linux, a working NeoMutt account, and an interactive terminal.
- A writable Nextcloud calendar with invitation delivery enabled and working SMTP.
- The invitation's attendee address must belong to your Nextcloud account. Add
  additional addresses in your Nextcloud profile when using aliases.
- Python 3.10 or newer, khal, vdirsyncer, icalendar, and requests.
- An already configured khal calendar and vdirsyncer pair. Each response mapping
  uses one explicitly named collection; it does not synchronize every account.

The initial live integration was tested with Nextcloud **35.0.2**, NeoMutt
**20260616**, khal **0.14.1**, vdirsyncer **0.21.0**, icalendar **7.3.0**, requests
**2.34.2**, and Python **3.14**. Other versions need their own integration check.
The helper imports two vdirsyncer Python helpers as well as invoking its CLI, so
keep the Python environment and CLI versions together.

## Install

Clone this repository, then install the Python dependencies in a dedicated venv:

```sh
git clone https://github.com/delightfuldude/neomutt-nextcloud-rsvp.git
cd neomutt-nextcloud-rsvp
python3 -m venv "$HOME/.local/share/neomutt-nextcloud-rsvp/venv"
"$HOME/.local/share/neomutt-nextcloud-rsvp/venv/bin/python" -m pip install -r requirements.txt
install -d -m 700 "$HOME/.config/mutt"
install -m 755 calendar-reply.py "$HOME/.config/mutt/calendar-reply.py"
install -m 755 integration/eventhandler.sh "$HOME/.config/mutt/eventhandler.sh"
```

Copy `examples/calendar-reply.json` to `~/.config/mutt/calendar-reply.json` and
adapt it. Preserve any existing file before replacing it. Your actual account
mapping and vdirsyncer credentials are local configuration, not repository data.

The wrapper uses `NEOMUTT_RSVP_PYTHON` if set, and prepends that interpreter's
directory to its child PATH so khal and vdirsyncer come from the same venv.
If using compatible distribution packages instead, omit that environment setting.

## Configure the calendar mapping

The JSON example maps an address to a khal calendar name, the corresponding local
vdir directory, and a vdirsyncer pair/collection:

```json
{
  "accounts": {
    "alice@example.org": {
      "calendar": "personal",
      "path": "~/.local/share/vdir/calendars/personal",
      "pair": "personal",
      "collection": "personal"
    }
  }
}
```

Use your names from `khal printcalendars` and your existing vdirsyncer config.
The filesystem collection name must match the remote collection name; renamed
three-element vdirsyncer collection mappings are not supported in this release.
`examples/khal.conf` and `examples/vdirsyncer.conf` show a matching minimal setup.
They are examples, not replacements for an existing multi-calendar configuration.

Credentials are read from your existing vdirsyncer storage, including its
`password.fetch` provider. The helper verifies `calendar-user-address-set` before
importing; mapping an arbitrary email address to a calendar does not make that
address a valid Nextcloud identity. The CalDAV backend must be Nextcloud and the
calendar must live in that configured user's calendar home.

## Configure NeoMutt

Add the settings in `integration/neomutt.conf` to your configuration. Set the
identity environment variable **after each account sets `$from`**, including in
whatever file you source when switching accounts:

```muttrc
setenv NEOMUTT_RSVP_PYTHON "$HOME/.local/share/neomutt-nextcloud-rsvp/venv/bin/python"
setenv NEOMUTT_CALENDAR_EMAIL "$from"
```

Merge the entries from `integration/mailcap` into the mailcap file NeoMutt
actually uses. `khal printics` provides the read-only automatic preview; it does
not import the file. If using the dedicated venv, set the preview command to
`~/.local/share/neomutt-nextcloud-rsvp/venv/bin/khal printics %s`.

The interactive mailcap entries deliberately do **not** have `copiousoutput`.
Only the preview entries do. This keeps reading a message separate from answering
an invitation.

With the supplied bindings:

1. Open a message.
2. Press **l** to show attachments.
3. Select the calendar attachment with **j/k**.
4. Press **l** or **Enter** to open the response menu.
5. Choose **1**, **2**, or **3**. Press Enter afterward to return to attachments.

These `l` bindings match mutt-wizard's existing attachment workflow. If your setup
uses different navigation, the native operation to open the response handler is
`<view-mailcap>`. Other attachment types retain their configured mailcap actions.

## Command-line use

```sh
python3 calendar-reply.py invite.ics --config /path/to/calendar-reply.json
python3 calendar-reply.py invite.ics --config /path/to/calendar-reply.json \
  --email alice@example.org --response accept
```

`--response` also accepts `tentative` and `decline`. Without it, the helper asks
through `/dev/tty`; stdin may contain a complete MIME email. `--khal-config` and
`--vdir-config` select alternate configuration files. Their defaults are
`~/.config/khal/config` and `~/.config/vdirsyncer/config`. The response mapping
defaults to a `.json` file next to the script.

## Behavior and limits

- Preserves UID, organizer SEQUENCE, timezone and recurrence data; changes only
  the selected identity's participation status.
- A repeated, server-confirmed answer does not send another reply. An updated
  organizer revision can be acknowledged even when you keep the same status.
- Existing objects without `LAST-MODIFIED` need a Nextcloud 35 workaround: an
  ETag-protected metadata-only PUT with scheduling disabled, followed by the
  ordinary import/sync. vdirsyncer ignores timestamp-only local changes.
- A second sync reads Nextcloud's final scheduling receipt after the upload.
  `SCHEDULE-STATUS` 1.1/1.2 means the server accepted or locally delivered the
  scheduling message. It is not proof of delivery into an external inbox.
- Full recurring invitations are supported. Detached recurrence exceptions are
  explicitly rejected; they need a separate reconciliation workflow.
- Only `METHOD:REQUEST` is accepted. Cancellation and reply emails are not
  processed automatically. Multiple unrelated UIDs are rejected.
- Failed or uncertain operations are reported without automatic retries or an
  SMTP fallback. A per-user lock prevents simultaneous helper runs.
- This is a Nextcloud workflow, not a standalone SMTP RSVP client.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests use synthetic calendar data and mocks; they do not use your real
calendar or send mail. See [the verification notes](docs/testing.md) for the
separate local Nextcloud/Mailpit integration matrix and its limits.

## Sources

- [NeoMutt MIME and mailcap support](https://neomutt.org/guide/mimesupport.html)
- [Nextcloud invitation settings](https://docs.nextcloud.com/server/stable/admin_manual/groupware/calendar.html#invitations)
- [sabre/dav CalDAV scheduling](https://sabre.io/dav/scheduling/)
- [RFC 5546: iTIP replies](https://www.rfc-editor.org/rfc/rfc5546.html#section-3.2.3)
- [RFC 6638: CalDAV scheduling](https://www.rfc-editor.org/rfc/rfc6638.html)

## License

MIT. See [LICENSE](LICENSE). No private dotfiles, credentials, real invitations,
or third-party calendar viewer scripts are included.
