"""Regression coverage for observed late GitHub schedule runs."""
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import os
from pathlib import Path
import tempfile
from test_digest import payload
import unittest
import digest as d
from test_digest import ENV


def t(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


class CycleTests(unittest.TestCase):
    def guard(self, current, previous=None, step=None, now=None, previous_event='schedule'):
        current_run = {'id': 20, 'workflow_id': 77, 'event': 'schedule', 'created_at': current}
        runs = [current_run]
        if previous:
            runs.append({'id': 19, 'event': previous_event, 'created_at': previous})
        responses = [current_run, {'total_count': len(runs), 'workflow_runs': runs}]
        if previous:
            responses.append({'total_count': 1, 'jobs': [{'steps': [step or {
                'name': d.DELIVERY_STEP, 'started_at': previous, 'conclusion': 'success'}]}]})
        now = t(now or current)
        return d.guard_github(now.astimezone(d.SHANGHAI).date().isoformat(),
                              {**ENV, 'GITHUB_EVENT_NAME': 'schedule'}, Mock(side_effect=responses),
                              now=now, with_cycle=True)

    def test_midnight_delay_does_not_reserve_following_evening(self):
        self.assertEqual(self.guard('2026-10-11T12:00:00Z', '2026-10-10T17:01:59Z'),
                         ('scheduled', '2026-10-11'))

    def test_second_observed_midnight_delay_next_evening(self):
        self.assertEqual(self.guard('2026-10-12T12:00:00Z', '2026-10-11T17:25:00Z'),
                         ('scheduled', '2026-10-12'))

    def test_late_run_uses_previous_cycle_actual_fetch_date_unchanged(self):
        self.assertEqual(self.guard('2026-10-11T17:25:00Z'), ('scheduled', '2026-10-11'))

    def test_same_cycle_blocked_even_failed_or_unknown(self):
        for conclusion in ['failure', 'cancelled', 'success', None]:
            with self.subTest(conclusion=conclusion), self.assertRaises(d.SafeError):
                self.guard('2026-10-11T17:25:00Z', '2026-10-11T12:00:00Z',
                           {'name': d.DELIVERY_STEP, 'started_at': '2026-10-11T12:01:00Z',
                            'conclusion': conclusion})

    def test_queue_crossing_twenty_oclock_stops(self):
        with self.assertRaises(d.SafeError):
            self.guard('2026-10-11T11:59:00Z', now='2026-10-11T12:01:00Z')

    def test_history_bound_to_creation_not_late_step(self):
        with self.assertRaises(d.SafeError):
            self.guard('2026-10-11T12:05:00Z', '2026-10-11T11:58:00Z',
                {'name': d.DELIVERY_STEP, 'started_at': '2026-10-11T12:00:01Z', 'conclusion': 'success'})

    def test_manual_history_independent(self):
        self.assertEqual(self.guard('2026-10-11T12:05:00Z', '2026-10-11T12:01:00Z', previous_event='workflow_dispatch'),
                         ('scheduled', '2026-10-11'))

    def test_missing_creation_time_fails_closed(self):
        with self.assertRaises(d.SafeError):
            self.guard(None, now='2026-10-11T12:05:00Z')

    def test_boundary_and_custom_morning_hour(self):
        for raw, expected in [('2026-10-11T11:59:59Z', '2026-10-10'),
                              ('2026-10-11T12:00:00Z', '2026-10-11'),
                              ('2026-10-11T16:00:00Z', '2026-10-11')]:
            self.assertEqual(d.scheduled_cycle(t(raw)), expected)
        self.assertEqual(d.scheduled_cycle(t('2026-10-12T02:00:00Z'), 10), '2026-10-12')


class DeliveryBoundaryTests(unittest.TestCase):
    def test_stop_remaining_chunks_at_twenty_oclock(self):
        p = payload()
        p['messages'] = ['first', 'second']
        before, after = t('2026-10-10T11:59:59Z'), t('2026-10-10T12:00:00Z')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV), \
             patch.object(d, 'validate_delivery'), \
             patch.object(d, 'guard_github', return_value=('scheduled', '2026-10-09')), \
             patch.object(d, 'datetime') as clock, patch.object(d.time, 'sleep'), \
             patch.object(d, 'send_message') as send:
            clock.now.side_effect = [before, before, after, after]
            with self.assertRaises(d.SafeError):
                d.deliver(p, tmp, before)
            self.assertEqual(send.call_count, 1)
            self.assertTrue((Path(tmp)/'2026-10-09.scheduled.json').exists())

    def test_missing_historical_creation_fails_closed(self):
        current = {'id': 20, 'event':'schedule', 'workflow_id':77, 'created_at':'2026-10-11T12:05:00Z'}
        for bad in [None, 'bad', '2026-10-11T12:00:00']:
            get = Mock(side_effect=[current, {'total_count':2, 'workflow_runs':[current,
                {'id':19, 'event':'schedule', 'created_at':bad}]},
                {'total_count':1, 'jobs':[{'steps':[{'name':d.DELIVERY_STEP,
                    'started_at':'2026-10-11T12:00:00Z','conclusion':'success'}]}]}])
            with self.subTest(bad=bad), self.assertRaises(d.SafeError):
                d.guard_github('2026-10-11', {**ENV,'GITHUB_EVENT_NAME':'schedule'}, get)
