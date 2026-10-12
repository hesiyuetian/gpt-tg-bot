from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import ashare as a
import digest as d
from test_digest import ENV

NOW = datetime(2026, 10, 12, 2, 0, 10, tzinfo=timezone.utc)
CAL = d.load_json(a.CALENDAR)


def line(symbol, **overrides):
    f = [''] * 60
    values = {1: '示例证券', 2: symbol[2:], 3: '10.10', 4: '10.00', 5: '10.05', 6: '200000',
              30: '20261012100000', 31: '0.10', 32: '1.00', 33: '10.2', 34: '9.9',
              36: '200000', 37: '20200', 49: '1.5', 57: '20200.55'}
    values.update({int(k): v for k, v in overrides.items()})
    for k, v in values.items():
        f[k] = str(v)
    return f'v_{symbol}="' + '~'.join(f) + '";'


def raw():
    return '\n'.join(line(s) for s in a.INDICES + a.WATCHLIST)


def payload():
    return a.build_payload(raw(), NOW, CAL)


class CalendarTests(unittest.TestCase):
    def test_weekends_and_makeup_workdays_closed(self):
        for day in ['2026-10-10', '2026-02-14', '2026-02-28', '2026-09-20']:
            self.assertFalse(a.trading_day(date.fromisoformat(day), CAL))

    def test_official_holidays_closed(self):
        for day in CAL['closed_dates']:
            self.assertFalse(a.trading_day(date.fromisoformat(day), CAL))

    def test_october_ninth_and_twelfth_open(self):
        for day in ['2026-10-09', '2026-10-12']:
            self.assertTrue(a.trading_day(date.fromisoformat(day), CAL))

    def test_unknown_year_and_malformed_calendar_fail_closed(self):
        for day, cal in [(date(2027,1,4), CAL), (date(2026,10,12), {}),
                         (date(2026,10,12), {**CAL, 'closed_dates': ['bad']})]:
            with self.assertRaises(d.SafeError):
                a.trading_day(day, cal)

    def test_closed_day_and_late_schedule_skip(self):
        for now in [NOW + timedelta(days=5), NOW + timedelta(minutes=21), NOW - timedelta(minutes=1)]:
            self.assertEqual(a.build_payload('', now, CAL)['status'], 'skip')


class QuoteTests(unittest.TestCase):
    def test_fields_and_units(self):
        q = a.parse_quotes(line('sh600519'), a.WATCHLIST, NOW)['sh600519']
        self.assertEqual(q.price, 10.1)
        self.assertEqual(q.amount_wan, 20200.55)
        self.assertEqual(q.volume_raw, 200000)
        self.assertEqual(q.volume_ratio, 1.5)
        self.assertEqual(q.at.hour, 10)

    def test_rounded_amount_fallback(self):
        q = a.parse_quotes(line('sh600519', **{'57': ''}), a.WATCHLIST, NOW)['sh600519']
        self.assertEqual(q.amount_wan, 20200)

    def test_old_future_malformed_and_wrong_symbol_quotes_omitted(self):
        for fields in [{'30':'20261009100000'}, {'30':'20261012100100'}, {'30':'bad'},
                       {'2':'000001'}, {'3':'nan'}, {'31':'9'}, {'32':'99'},
                       {'3':'0'}, {'36':'0'}, {'57':'-3'}, {'49':'-1'}]:
            with self.subTest(fields=fields):
                self.assertEqual(a.parse_quotes(line('sh600519', **fields), a.WATCHLIST, NOW), {})

    def test_zero_volume_ratio_is_unavailable(self):
        q = a.parse_quotes(line('sh600519', **{'49':'0'}), a.WATCHLIST, NOW)['sh600519']
        self.assertIsNone(q.volume_ratio)

    def test_duplicate_security_fails_closed(self):
        with self.assertRaises(d.SafeError):
            a.parse_quotes(line('sh600519') * 2, a.WATCHLIST, NOW)

    def test_missing_any_index_blocks_whole_brief(self):
        for symbol in a.INDICES:
            with self.assertRaises(d.SafeError):
                a.build_payload(raw().replace(line(symbol), ''), NOW, CAL)

    def test_insufficient_pool_coverage_blocks(self):
        with self.assertRaises(d.SafeError):
            a.build_payload('\n'.join(line(s) for s in a.INDICES + a.WATCHLIST[:15]), NOW, CAL)

    def test_stale_stock_omitted_with_coverage_disclosure(self):
        p = a.build_payload(raw().replace(line(a.WATCHLIST[0]), line(a.WATCHLIST[0], **{'30':'20261011100000'})), NOW, CAL)
        self.assertEqual(p['coverage'], 19)
        self.assertNotIn(a.WATCHLIST[0], p['candidate_symbols'])

    def test_bound_five_and_disclose_scope_risk_and_timestamp(self):
        p = payload()
        self.assertEqual(len(p['candidate_symbols']), 5)
        for text in ['固定20只', '不是完整研究或全市场推荐', '不保证收益', '10:00:00', '腾讯量比']:
            self.assertIn(text, p['message'])
        self.assertLess(d.utf16_length(p['message']), 3900)

    def test_empty_pool_signals_never_force_candidates(self):
        p = a.build_payload(raw().replace('~1.5~', '~0~'), NOW, CAL)
        self.assertEqual(p['candidate_symbols'], [])
        self.assertIn('保持空名单', p['message'])

    def test_st_and_low_amount_filtered(self):
        modified = raw()
        for s in a.WATCHLIST:
            modified = modified.replace(line(s), line(s, **{'1':'ST示例'}))
        self.assertEqual(a.build_payload(modified, NOW, CAL)['candidate_symbols'], [])


class SendingTests(unittest.TestCase):
    def env(self):
        return {**ENV, 'ASHARE_ENABLED':'true', 'GITHUB_EVENT_NAME':'schedule'}

    def test_good_payload_validates_without_sending(self):
        a.validate_send(payload(), NOW, CAL, self.env())

    def test_disabled_fixture_stale_and_group_rejected(self):
        for p, now, env in [(payload(), NOW, ENV), ({**payload(), 'fixture':True}, NOW, self.env()),
                            (payload(), NOW+timedelta(seconds=121), self.env()),
                            (payload(), NOW, {**self.env(), 'TELEGRAM_CHAT_ID':'-42'})]:
            with self.subTest(), self.assertRaises(d.SafeError):
                a.validate_send(p, now, CAL, env)

    def test_quotes_expire_even_when_payload_new(self):
        p = payload(); p['quote_times'][0] = (NOW-timedelta(seconds=181)).isoformat()
        with self.assertRaises(d.SafeError):
            a.validate_send(p, NOW, CAL, self.env())

    def test_manual_never_sends(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.env()), \
             patch.object(d, 'guard_github', return_value=('manual-test','2026-10-12')), patch.object(d, 'send_message') as send:
            with self.assertRaises(d.SafeError):
                a.deliver(payload(), CAL, tmp, NOW)
            send.assert_not_called()

    def test_uncertain_delivery_reserved_and_not_retried(self):
        p = payload()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.env()), \
             patch.object(d, 'guard_github', return_value=('scheduled','2026-10-12')), \
             patch.object(a, 'datetime') as clock, patch.object(d, 'send_message', side_effect=d.SafeError('uncertain')) as send:
            clock.now.return_value = NOW
            for _ in range(2):
                with self.assertRaises(d.SafeError):
                    a.deliver(p, CAL, tmp, NOW)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(json.loads((Path(tmp)/'2026-10-12.ashare.json').read_text())['status'], 'reserved')

    def test_freshness_rechecked_after_history_lookup(self):
        p = payload()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.env()), \
             patch.object(d, 'guard_github', return_value=('scheduled','2026-10-12')), \
             patch.object(a, 'datetime') as clock, patch.object(d, 'send_message') as send:
            clock.now.return_value = NOW + timedelta(minutes=4)
            with self.assertRaises(d.SafeError):
                a.deliver(p, CAL, tmp, NOW)
            send.assert_not_called()
