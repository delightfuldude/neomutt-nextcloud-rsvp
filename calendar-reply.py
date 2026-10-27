#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 delightfuldude
"""Answer ICS or RFC5322 invitations through Nextcloud; never send a second mail."""
from __future__ import annotations
import argparse
import configparser
import copy
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin, urlsplit
import icalendar
import requests
from vdirsyncer.cli.fetchparams import expand_fetch_params
from vdirsyncer.vobject import hash_item

STATUSES = {'accept': 'ACCEPTED', 'tentative': 'TENTATIVE', 'decline': 'DECLINED'}
INTERACTIVE = False


def address(value):
    value = str(value)
    if not value.lower().startswith('mailto:'):
        raise ValueError('Kalenderadresse ist keine mailto-Adresse.')
    return value[7:].casefold()


def attendees(event):
    value = event.get('ATTENDEE', [])
    return value if isinstance(value, list) else [value]


def master(cal):
    return next(e for e in cal.walk('VEVENT') if 'RECURRENCE-ID' not in e)


def own_attendee(event, identity):
    matches = [a for a in attendees(event) if address(a) == identity]
    if len(matches) > 1:
        raise ValueError('Die eigene Teilnehmeradresse kommt mehrfach vor.')
    return matches[0] if matches else None


def parse_invitation(data):
    if data.lstrip().upper().startswith(b'BEGIN:VCALENDAR'):
        payload = data
    else:
        mail = BytesParser(policy=policy.default).parsebytes(data)
        parts = [p.get_payload(decode=True) for p in mail.walk()
                 if p.get_content_type() in ('text/calendar', 'application/ics', 'application/icalendar')
                 or (p.get_filename() or '').lower().endswith('.ics')]
        parts = list(dict.fromkeys(p for p in parts if p))
        if len(parts) != 1:
            raise ValueError('Die Nachricht muss genau eine eindeutige Kalendereinladung enthalten.')
        payload = parts[0]
    cal = icalendar.Calendar.from_ical(payload)
    if str(cal.get('METHOD', '')).upper() != 'REQUEST':
        raise ValueError('Keine Einladung (METHOD:REQUEST). Absagen/Antworten werden nicht als Einladung importiert.')
    events = cal.walk('VEVENT')
    if not events or len({str(e.get('UID', '')) for e in events}) != 1 or not events[0].get('UID'):
        raise ValueError('Die Einladung muss genau eine Termin-ID enthalten.')
    if not any('RECURRENCE-ID' not in e for e in events):
        raise ValueError('Einzelne Serienausnahme: gesonderter Abgleich nötig; nichts geändert.')
    if any('ORGANIZER' not in e for e in events) or len({address(e['ORGANIZER']) for e in events}) != 1:
        raise ValueError('Der Organisator fehlt oder ist nicht eindeutig.')
    if any(str(e.get('STATUS', '')).upper() == 'CANCELLED' for e in events):
        raise ValueError('Der Termin ist abgesagt; keine Zusage möglich.')
    return cal


def prepare(cal, identity, status):
    result = copy.deepcopy(cal)
    if own_attendee(master(result), identity) is None:
        raise ValueError('Die gewählte Adresse steht nicht als Teilnehmer in der Einladung.')
    if address(master(result)['ORGANIZER']) == identity:
        raise ValueError('Dies ist dein eigener Termin, keine Einladung an dich.')
    for event in result.walk('VEVENT'):
        own = own_attendee(event, identity)
        if own is not None:
            own.params['PARTSTAT'] = status
            own.params['RSVP'] = 'FALSE'
        for prop in [event['ORGANIZER'], *attendees(event)]:
            prop.params.pop('SCHEDULE-STATUS', None)
            prop.params.pop('SCHEDULE-FORCE-SEND', None)
        if str(event['ORGANIZER'].params.get('SCHEDULE-AGENT', 'SERVER')).upper() != 'SERVER':
            raise ValueError('Die Einladung schaltet serverseitige Antworten ab (SCHEDULE-AGENT).')
        event['LAST-MODIFIED'] = icalendar.vDDDTypes(datetime.now(timezone.utc).replace(microsecond=0))
    return result


def run(args):
    proc = subprocess.run(args, text=True, capture_output=True, timeout=120)
    if proc.returncode:
        raise RuntimeError(f'{args[0]} fehlgeschlagen. Keine automatische Wiederholung.\n{proc.stderr[-1800:]}')
    return proc.stdout


def values(section):
    result = {}
    for key, value in section.items():
        try:
            result[key] = json.loads(value)
        except json.JSONDecodeError:
            result[key] = value
    return result


def validate_server(account, identity, vdirconf):
    config = configparser.RawConfigParser()
    if not config.read(vdirconf):
        raise ValueError('vdirsyncer-Konfiguration fehlt.')
    pair = values(config['pair ' + account['pair']])
    storages = [values(config['storage ' + pair[k]]) for k in ('a', 'b')]
    local = next(s for s in storages if s.get('type') == 'filesystem')
    remote = next(s for s in storages if s.get('type') == 'caldav')
    expected = (Path(local['path']).expanduser() / account['collection']).resolve()
    if expected != Path(account['path']).expanduser().resolve():
        raise ValueError('Kalenderpfad und vdirsyncer-Ziel stimmen nicht überein.')
    if account['collection'] not in pair['collections']:
        raise ValueError('Die Sammlung ist nicht für diesen Sync konfiguriert.')
    remote = expand_fetch_params(remote)
    base = remote['url'].split('/remote.php')[0]
    principal = base + '/remote.php/dav/principals/users/' + remote['username'] + '/'
    body = '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-user-address-set/></d:prop></d:propfind>'
    response = requests.request('PROPFIND', principal, data=body,
                                headers={'Depth': '0', 'Content-Type': 'application/xml'},
                                auth=(remote['username'], remote['password']), timeout=25)
    if response.status_code != 207:
        raise ValueError(f'Kalenderidentität nicht prüfbar (HTTP {response.status_code}).')
    root = ET.fromstring(response.content)
    recognized = [address(x.text) for p in root.iter('{urn:ietf:params:xml:ns:caldav}calendar-user-address-set')
                  for x in p if (x.text or '').lower().startswith('mailto:')]
    if identity not in recognized:
        raise ValueError('Nextcloud kennt diese Teilnehmeradresse nicht. Kein Import und kein Versand.')
    remote['calendar_url'] = base + '/remote.php/dav/calendars/' + quote(remote['username'], safe='') + '/' + quote(account['collection'], safe='') + '/'
    return remote


def normalize_remote_timestamp(remote, existing):
    """Conditional metadata-only write; vdirsyncer ignores LAST-MODIFIED hashes."""
    ns = 'urn:ietf:params:xml:ns:caldav'
    root = ET.Element(f'{{{ns}}}calendar-query')
    props = ET.SubElement(root, '{DAV:}prop')
    ET.SubElement(props, '{DAV:}getetag')
    ET.SubElement(props, f'{{{ns}}}calendar-data')
    filt = ET.SubElement(root, f'{{{ns}}}filter')
    outer = ET.SubElement(filt, f'{{{ns}}}comp-filter', name='VCALENDAR')
    inner = ET.SubElement(outer, f'{{{ns}}}comp-filter', name='VEVENT')
    uid = ET.SubElement(inner, f'{{{ns}}}prop-filter', name='UID')
    ET.SubElement(uid, f'{{{ns}}}text-match', collation='i;octet').text = str(master(existing)['UID'])
    auth = (remote['username'], remote['password'])
    response = requests.request('REPORT', remote['calendar_url'], data=ET.tostring(root),
                                headers={'Depth': '1', 'Content-Type': 'application/xml'}, auth=auth, timeout=25)
    if response.status_code != 207:
        raise RuntimeError('Das vorhandene Serverobjekt konnte nicht eindeutig gelesen werden.')
    matches = []
    for item in ET.fromstring(response.content).findall('{DAV:}response'):
        data = item.find(f'.//{{{ns}}}calendar-data')
        etag = item.find('.//{DAV:}getetag')
        href = item.find('{DAV:}href')
        if data is not None and etag is not None and href is not None:
            matches.append((data.text, etag.text, href.text))
    if len(matches) != 1:
        raise RuntimeError('Die Termin-ID ist auf dem Server nicht eindeutig.')
    data, etag, href = matches[0]
    if hash_item(data) != hash_item(existing.to_ical().decode()):
        raise RuntimeError('Der Servertermin wurde inzwischen verändert. Bitte neu öffnen.')
    url = urljoin(remote['calendar_url'], href)
    if urlsplit(url).netloc != urlsplit(remote['calendar_url']).netloc:
        raise RuntimeError('Unerwartetes Serverziel; keine Zugangsdaten weitergegeben.')
    normalized = icalendar.Calendar.from_ical(data)
    for event in normalized.walk('VEVENT'):
        if 'LAST-MODIFIED' not in event:
            event.add('LAST-MODIFIED', datetime.now(timezone.utc).replace(microsecond=0))
    response = requests.put(url, data=normalized.to_ical(), auth=auth,
                            headers={'If-Match': etag, 'Content-Type': 'text/calendar', 'x-nc-scheduling': 'false'},
                            timeout=25, allow_redirects=False)
    if response.status_code not in (201, 204):
        raise RuntimeError(f'Zeitstempelabgleich fehlgeschlagen (HTTP {response.status_code}); keine Statusänderung versucht.')


def find_existing(account, uid):
    result = []
    for path in Path(account['path']).expanduser().glob('*.ics'):
        cal = icalendar.Calendar.from_ical(path.read_bytes())
        if any(str(e.get('UID')) == uid for e in cal.walk('VEVENT')):
            result.append(cal)
    if len(result) > 1:
        raise ValueError('Mehrere Objekte mit derselben UID; bitte zuerst den Konflikt lösen.')
    return result[0] if result else None


def import_calendar(cal, account, khalconf):
    with tempfile.TemporaryDirectory(prefix='neomutt-calendar-') as scratch:
        file = Path(scratch) / 'invitation.ics'
        file.write_bytes(cal.to_ical())
        file.chmod(0o600)
        run(['khal', '-c', str(khalconf), 'import', '-a', account['calendar'], '--batch', str(file)])


def respond(cal, identity, status, account, vdirconf, khalconf):
    # Validate all invitation fields BEFORE any synchronization or write.
    prepare(cal, identity, status)
    print('1/2 Kalender wird mit Nextcloud abgeglichen …', flush=True)
    remote = validate_server(account, identity, vdirconf)
    sync = ['vdirsyncer', '-c', str(vdirconf), 'sync', account['pair'] + '/' + account['collection']]
    run(sync)
    existing = find_existing(account, str(master(cal)['UID']))
    force_reply = False
    if existing:
        old, incoming = master(existing), master(cal)
        if address(old['ORGANIZER']) != address(incoming['ORGANIZER']):
            raise ValueError('Die Termin-ID gehört bereits zu einem anderen Organisator.')
        if int(old.get('SEQUENCE', 0)) > int(incoming.get('SEQUENCE', 0)):
            raise ValueError('Die Einladung ist älter als der vorhandene Termin. Keine Änderung.')
        if int(old.get('SEQUENCE', 0)) == int(incoming.get('SEQUENCE', 0)):
            oldstamp, newstamp = old.get('DTSTAMP'), incoming.get('DTSTAMP')
            if oldstamp and newstamp and oldstamp.dt >= newstamp.dt:
                cal = existing
        previous = own_attendee(old, identity)
        # A fresh organizer revision can require an acknowledgement even when
        # the participant keeps the same status. RFC6638's one-shot parameter
        # requests that reply; Sabre removes it from the stored object.
        force_reply = bool(previous and str(previous.params.get('PARTSTAT')) == status and cal is not existing)
        if previous and str(previous.params.get('PARTSTAT')) == status and cal is existing:
            receipt = str(old['ORGANIZER'].params.get('SCHEDULE-STATUS', ''))
            if receipt.startswith(('1.1', '1.2')):
                return 'Bereits so beantwortet; keine weitere Antwort ausgelöst.'
            raise ValueError('Status bereits gesetzt, aber Serverantwort nicht belegt. Kein blinder Neuversand.')
        # NC35 compares the reply against the OLD object's LAST-MODIFIED.
        # A missing old timestamp suppresses replies (SCHEDULE-STATUS=1.0).
        # Normalize it with unchanged PARTSTAT before changing participation.
        if any('LAST-MODIFIED' not in event for event in existing.walk('VEVENT')):
            normalize_remote_timestamp(remote, existing)
            run(sync)
    prepared = prepare(cal, identity, status)
    if force_reply:
        for event in prepared.walk('VEVENT'):
            event['ORGANIZER'].params['SCHEDULE-FORCE-SEND'] = 'REPLY'
    print('2/2 Antwort wird gespeichert und synchronisiert; Versandstatus wird geprüft …', flush=True)
    import_calendar(prepared, account, khalconf)
    run(sync)
    # Nextcloud dispatches SMTP in afterWriteContent, after its PUT validator
    # may already have been computed. A second sync fetches the final receipt.
    run(sync)
    persisted = find_existing(account, str(master(cal)['UID']))
    if not persisted or not own_attendee(master(persisted), identity) or str(own_attendee(master(persisted), identity).params.get('PARTSTAT')) != status:
        raise RuntimeError('Der Teilnahmestatus konnte nach dem Sync nicht bestätigt werden.')
    receipt = str(master(persisted)['ORGANIZER'].params.get('SCHEDULE-STATUS', ''))
    if not receipt.startswith(('1.1', '1.2')):
        raise RuntimeError(f'Termin synchronisiert, Antwortversand nicht bestätigt (SCHEDULE-STATUS={receipt or "fehlt"}). Kein zusätzlicher Mailversand.')
    label = {'ACCEPTED': 'Angenommen', 'TENTATIVE': 'Unter Vorbehalt angenommen', 'DECLINED': 'Abgelehnt'}[status]
    return f'{label}: im Kalender gespeichert. Nextcloud hat die Antwort zum Versand angenommen.'


def main():
    global INTERACTIVE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', nargs='?', help='ICS/Mail-Datei; sonst vollständige Mail auf stdin')
    parser.add_argument('--email', default=os.environ.get('NEOMUTT_CALENDAR_EMAIL'), help='aktive NeoMutt-Absenderidentität')
    parser.add_argument('--response', choices=STATUSES)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_suffix('.json'))
    parser.add_argument('--vdir-config', type=Path, default=Path.home() / '.config/vdirsyncer/config')
    parser.add_argument('--khal-config', type=Path, default=Path.home() / '.config/khal/config')
    args = parser.parse_args()
    INTERACTIVE = not args.response
    data = Path(args.file).read_bytes() if args.file else sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Einladung zu groß (maximal 8 MiB).')
    cal = parse_invitation(data)
    accounts = json.loads(args.config.read_text())['accounts']
    identities = [a for a in accounts if own_attendee(master(cal), a)]
    if args.email:
        identity = args.email.casefold()
        if identity not in identities:
            raise ValueError('Die aktive Mailadresse ist kein konfigurierter Teilnehmer. Keine fremde Identität gewählt.')
    elif len(identities) == 1:
        identity = identities[0]
    else:
        raise ValueError('Keine eindeutige konfigurierte Identität. Bitte das passende Mailkonto auswählen.')
    event = master(cal)
    start = event.decoded('DTSTART')
    print(f'Termin: {event.get("SUMMARY", "(ohne Titel)")}\nBeginn: {start}\nOrganisator: {event["ORGANIZER"]}\nDu: {identity}\nKalender: {accounts[identity]["calendar"]}', flush=True)
    response = args.response
    if not response:
        with open('/dev/tty', 'r') as tty_in, open('/dev/tty', 'w') as tty_out:
            tty_out.write('1 Annehmen · 2 Unter Vorbehalt · 3 Ablehnen · Enter Abbrechen: ')
            tty_out.flush()
            response = {'1': 'accept', '2': 'tentative', '3': 'decline'}.get(tty_in.readline().strip())
        if not response:
            INTERACTIVE = False
            print('Abgebrochen; nichts verändert.')
            return
    lockdir = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'neomutt-calendar'
    lockdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (lockdir / 'reply.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(respond(cal, identity, STATUSES[response], accounts[identity], args.vdir_config, args.khal_config))


if __name__ == '__main__':
    exit_status = 0
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError, StopIteration, subprocess.TimeoutExpired, requests.RequestException) as error:
        print(f'Kalendereinladung: {error}', file=sys.stderr)
        exit_status = 1
    finally:
        if INTERACTIVE:
            try:
                with open('/dev/tty', 'r') as tty_in, open('/dev/tty', 'w') as tty_out:
                    tty_out.write('Enter zurück zur Anhangsliste: ')
                    tty_out.flush()
                    tty_in.readline()
            except OSError:
                pass
    sys.exit(exit_status)
