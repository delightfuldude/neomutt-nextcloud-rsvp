import copy
from email.message import EmailMessage
import importlib.util
from pathlib import Path
import unittest
from unittest import mock
import tempfile
import json
import icalendar

SOURCE = Path(__file__).resolve().parents[1] / 'calendar-reply.py'
SPEC = importlib.util.spec_from_file_location('calendar_reply', SOURCE)
reply = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reply)

ICS = b'''BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Synthetic tests//EN\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
UID:synthetic@example.invalid\r
DTSTAMP:20261027T120000Z\r
DTSTART;TZID=Europe/Vienna:20261104T130000\r
DTEND;TZID=Europe/Vienna:20261104T140000\r
SEQUENCE:3\r
SUMMARY:Einladung mit langem\r
  Betreff\r
ORGANIZER:mailto:organizer@example.invalid\r
ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:me@example.invalid\r
ATTENDEE;PARTSTAT=TENTATIVE:mailto:other@example.invalid\r
END:VEVENT\r
END:VCALENDAR\r
'''


class CalendarReplyTest(unittest.TestCase):
    def test_only_own_participation_changes(self):
        cal = reply.parse_invitation(ICS)
        result = reply.prepare(cal, 'me@example.invalid', 'ACCEPTED')
        self.assertEqual(reply.own_attendee(reply.master(result), 'me@example.invalid').params['PARTSTAT'], 'ACCEPTED')
        self.assertEqual(reply.own_attendee(reply.master(result), 'other@example.invalid').params['PARTSTAT'], 'TENTATIVE')
        self.assertEqual(reply.own_attendee(reply.master(cal), 'me@example.invalid').params['PARTSTAT'], 'NEEDS-ACTION')

    def test_uid_sequence_timezone_and_recurrence_are_preserved(self):
        cal = reply.parse_invitation(ICS)
        reply.master(cal).add('RRULE', {'FREQ': 'WEEKLY', 'COUNT': 4})
        result = reply.prepare(cal, 'me@example.invalid', 'DECLINED')
        for key in ('UID', 'SEQUENCE', 'DTSTART', 'DTEND', 'RRULE'):
            self.assertEqual(reply.master(cal)[key], reply.master(result)[key])

    def test_base64_attachment_and_line_folding(self):
        mail = EmailMessage()
        mail.set_content('Einladung')
        mail.add_attachment(ICS, maintype='text', subtype='calendar', filename='invite.ics')
        parsed = reply.parse_invitation(mail.as_bytes())
        self.assertEqual(str(reply.master(parsed)['SUMMARY']), 'Einladung mit langem Betreff')

    def test_duplicate_mime_representations_are_not_two_events(self):
        mail = EmailMessage()
        mail.set_content('Einladung')
        for _ in range(2):
            mail.add_attachment(ICS, maintype='text', subtype='calendar', filename='invite.ics')
        self.assertEqual(str(reply.master(reply.parse_invitation(mail.as_bytes()))['UID']), 'synthetic@example.invalid')

    def test_wrong_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Teilnehmer'):
            reply.prepare(reply.parse_invitation(ICS), 'stranger@example.invalid', 'ACCEPTED')

    def test_cancel_reply_and_publish_are_not_requests(self):
        for method in ('CANCEL', 'REPLY', 'PUBLISH'):
            with self.subTest(method=method), self.assertRaises(ValueError):
                reply.parse_invitation(ICS.replace(b'METHOD:REQUEST', ('METHOD:' + method).encode()))

    def test_multiple_uids_are_rejected(self):
        cal = reply.parse_invitation(ICS)
        event = copy.deepcopy(reply.master(cal))
        event['UID'] = 'another@example.invalid'
        cal.add_component(event)
        with self.assertRaises(ValueError):
            reply.parse_invitation(cal.to_ical())

    def test_detached_exception_is_explicitly_rejected(self):
        cal = reply.parse_invitation(ICS)
        event = reply.master(cal)
        event.add('RECURRENCE-ID', event['DTSTART'].dt)
        with self.assertRaisesRegex(ValueError, 'Serienausnahme'):
            reply.parse_invitation(cal.to_ical())

    def test_client_managed_scheduling_is_not_silently_overridden(self):
        cal = reply.parse_invitation(ICS)
        reply.master(cal)['ORGANIZER'].params['SCHEDULE-AGENT'] = 'CLIENT'
        with self.assertRaisesRegex(ValueError, 'SCHEDULE-AGENT'):
            reply.prepare(cal, 'me@example.invalid', 'ACCEPTED')

    def test_attachment_uses_active_account_when_two_own_addresses_are_invited(self):
        with tempfile.TemporaryDirectory(prefix='calendar-identity-test-') as directory:
            root = Path(directory)
            source = root / 'invite.ics'
            source.write_bytes(ICS)
            config = root / 'reply.json'
            config.write_text(json.dumps({'accounts': {
                'me@example.invalid': {'calendar': 'personal'},
                'other@example.invalid': {'calendar': 'business'},
            }}))
            argv = ['calendar-reply.py', str(source), '--config', str(config), '--response', 'accept']
            with mock.patch.dict('os.environ', {'NEOMUTT_CALENDAR_EMAIL': 'other@example.invalid', 'XDG_STATE_HOME': str(root / 'state')}), \
                    mock.patch('sys.argv', argv), mock.patch.object(reply, 'respond', return_value='OK') as respond, \
                    mock.patch('builtins.print'):
                reply.main()
            self.assertEqual(respond.call_args.args[1], 'other@example.invalid')
            self.assertEqual(respond.call_args.args[3]['calendar'], 'business')

    def test_progress_is_visible_before_sync_and_import(self):
        invitation = reply.parse_invitation(ICS)
        persisted = reply.prepare(invitation, 'me@example.invalid', 'ACCEPTED')
        reply.master(persisted)['ORGANIZER'].params['SCHEDULE-STATUS'] = '1.1'
        operations = []
        def record_print(message, **kwargs):
            self.assertTrue(kwargs.get('flush'))
            operations.append(message)
        with mock.patch('builtins.print', side_effect=record_print), \
                mock.patch.object(reply, 'validate_server', return_value={}), \
                mock.patch.object(reply, 'run', side_effect=lambda args: operations.append(args[0])), \
                mock.patch.object(reply, 'find_existing', side_effect=[None, persisted]), \
                mock.patch.object(reply, 'import_calendar', side_effect=lambda *args: operations.append('import')):
            result = reply.respond(invitation, 'me@example.invalid', 'ACCEPTED',
                                   {'pair': 'personal', 'collection': 'events'}, Path('vdir.conf'), Path('khal.conf'))
        self.assertTrue(operations[0].startswith('1/2'))
        self.assertEqual(operations[1], 'vdirsyncer')
        self.assertTrue(operations[2].startswith('2/2'))
        self.assertEqual(operations[3:], ['import', 'vdirsyncer', 'vdirsyncer'])
        self.assertIn('Nextcloud hat die Antwort', result)

    def test_failed_identity_check_does_not_claim_import_or_send(self):
        with mock.patch('builtins.print') as output, \
                mock.patch.object(reply, 'validate_server', side_effect=ValueError('unknown identity')), \
                mock.patch.object(reply, 'run') as run, mock.patch.object(reply, 'import_calendar') as save:
            with self.assertRaises(ValueError):
                reply.respond(reply.parse_invitation(ICS), 'me@example.invalid', 'ACCEPTED', {}, Path('vdir'), Path('khal'))
        self.assertEqual(output.call_count, 1)
        run.assert_not_called()
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
