import contextlib
from datetime import datetime, timezone, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch
import urllib.error

import digest as d

FIXTURE = Path(__file__).parent / "fixtures/trending.html"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
ENV = {"DIGEST_ENABLED": "true", "DIGEST_LANGUAGE_MODE": "original-approved",
       "TELEGRAM_BOT_TOKEN": "12345:" + "x" * 25, "TELEGRAM_CHAT_ID": "987654321",
       "GITHUB_ACTIONS": "true", "GITHUB_RUN_ATTEMPT": "1",
       "GITHUB_REPOSITORY": "example/digest", "GITHUB_RUN_ID": "20",
       "GITHUB_EVENT_NAME": "workflow_dispatch"}


def fixture_projects():
    return d.parse_trending(FIXTURE.read_text())


def payload():
    return d.make_payload(fixture_projects(), NOW)


class ParserTests(unittest.TestCase):
    def test_sort_by_daily_not_total_stars(self):
        ps = fixture_projects()
        self.assertEqual([p.stars_today for p in ps], [1234, 7, 1])
        self.assertEqual(ps[0].repository, "example/high")

    def test_nested_text_and_entities(self):
        ps = fixture_projects()
        self.assertEqual(ps[0].description, "Build small tools.")
        self.assertEqual(ps[1].description, "A tiny & useful CLI.")
        self.assertEqual(ps[2].description, "")
        self.assertEqual(ps[2].language, "未标注")

    def test_empty_page_fails_closed(self):
        with self.assertRaises(d.SafeError):
            d.parse_trending("<html><h1>Sign in</h1></html>")

    def test_missing_daily_stars_never_invents_zero(self):
        with self.assertRaises(d.SafeError):
            d.parse_trending(FIXTURE.read_text().replace("7 stars today", "7 stars this week"))

    def test_duplicate_repo_fails(self):
        with self.assertRaises(d.SafeError):
            d.parse_trending(FIXTURE.read_text().replace("example/low", "example/high"))

    def test_external_repo_link_fails(self):
        with self.assertRaises(d.SafeError):
            d.parse_trending(FIXTURE.read_text().replace('href="/example/high"', 'href="https://evil.test/owner/repo"'))

    def test_limit_20_and_ties_deterministic(self):
        html = "".join(f'<article class="Box-row"><h2><a href="/o/r{i:02}">repo</a></h2><span class="float-sm-right">{i} stars today</span></article>' for i in range(25))
        ps = d.parse_trending(html)
        self.assertEqual(len(ps), 20)
        self.assertEqual((ps[0].stars_today, ps[-1].stars_today), (24, 5))

    def test_untrusted_control_characters_removed(self):
        self.assertEqual(d.clean("a\nb\u202ec\x00"), "a bc")


class FormattingTests(unittest.TestCase):
    def test_china_date_boundary(self):
        p = d.make_payload(fixture_projects(), datetime(2026, 10, 10, 17, tzinfo=timezone.utc))
        self.assertEqual(p["date"], "2026-10-11")

    def test_original_descriptions_explicit(self):
        p = payload()
        self.assertFalse(p["chinese_complete"])
        self.assertIn("原文简介", "".join(p["messages"]))
        self.assertIn("本次 3 个", p["messages"][0])

    def test_overrides_bound_to_current_description(self):
        overrides = {p.repository: {"description_sha256": d.description_hash(p.description), "zh": "可核对的中文简介"} for p in fixture_projects() if p.description}
        p = d.make_payload(fixture_projects(), NOW, overrides)
        self.assertTrue(p["chinese_complete"])
        overrides["example/high"]["description_sha256"] = "stale"
        self.assertFalse(d.make_payload(fixture_projects(), NOW, overrides)["chinese_complete"])

    def test_english_override_does_not_claim_chinese(self):
        p = fixture_projects()[0]
        overrides = {p.repository: {"description_sha256": d.description_hash(p.description), "zh": "English only"}}
        self.assertFalse(d.make_payload([p], NOW, overrides)["chinese_complete"])

    def test_chunking_handles_emoji_long_blocks(self):
        blocks = ["😀" * 6000, "中文" * 7000, "last"]
        chunks = d.chunk_blocks(blocks)
        self.assertGreater(len(chunks), 4)
        self.assertTrue(all(0 < d.utf16_length(c) <= d.MAX_MESSAGE for c in chunks))
        # Every source character is retained; separators are the only additions.
        self.assertEqual("".join(chunks).replace("\n", ""), "".join(blocks))

    def test_large_descriptions_produce_valid_numbered_messages(self):
        ps = [d.Project(f"o/r{i}", "😀<b> & hello " * 1000, 100, "C++") for i in range(20)]
        p = d.make_payload(ps, NOW)
        self.assertGreater(len(p["messages"]), 1)
        self.assertTrue(all(d.utf16_length(x) <= 4096 for x in p["messages"]))
        self.assertIn("…", "".join(p["messages"]))

    def test_fixture_cannot_send(self):
        p = d.make_payload(fixture_projects(), NOW, fixture=True)
        with self.assertRaises(d.SafeError):
            d.validate_delivery(p, NOW, ENV)


class GateTests(unittest.TestCase):
    def test_defaults_disabled(self):
        with self.assertRaises(d.SafeError):
            d.validate_delivery(payload(), NOW, {})

    def test_chinese_requirement_blocks_originals(self):
        with self.assertRaises(d.SafeError):
            d.validate_delivery(payload(), NOW, {**ENV, "DIGEST_LANGUAGE_MODE": "chinese-required"})

    def test_explicit_original_mode_valid(self):
        d.validate_delivery(payload(), NOW, ENV)

    def test_original_mode_is_user_selected_default(self):
        env = {k: v for k, v in ENV.items() if k != "DIGEST_LANGUAGE_MODE"}
        d.validate_delivery(payload(), NOW, env)

    def test_stale_future_and_wrong_day_blocked(self):
        for now in [NOW + timedelta(hours=1), NOW - timedelta(minutes=2), NOW + timedelta(days=1)]:
            with self.subTest(now=now), self.assertRaises(d.SafeError):
                d.validate_delivery(payload(), now, ENV)

    def test_group_or_channel_rejected(self):
        for chat in ["-123", "@channel", "", "0"]:
            with self.subTest(chat=chat), self.assertRaises(d.SafeError):
                d.validate_delivery(payload(), NOW, {**ENV, "TELEGRAM_CHAT_ID": chat})

    def test_oversized_message_rejected(self):
        p = payload()
        p["messages"] = ["😀" * 2050]
        with self.assertRaises(d.SafeError):
            d.validate_delivery(p, NOW, ENV)

    def test_bad_token_errors_do_not_echo_secret(self):
        secret = "my-secret-value"
        with self.assertRaises(d.SafeError) as ctx:
            d.validate_delivery(payload(), NOW, {**ENV, "TELEGRAM_BOT_TOKEN": secret})
        self.assertNotIn(secret, str(ctx.exception))


class DedupTests(unittest.TestCase):
    def make_get(self, step=None, total_count=2, current_event="workflow_dispatch", previous_event="workflow_dispatch"):
        jobs = [{"steps": [step] if step else []}]
        return Mock(side_effect=[{"workflow_id": 77, "event": current_event},
            {"total_count": total_count, "workflow_runs": [{"id": 20, "event": current_event}, {"id": 19, "event": previous_event}]},
            {"total_count": 1, "jobs": jobs}])

    def test_previous_same_day_attempt_blocks_even_failure(self):
        for event in ["schedule", "workflow_dispatch"]:
            for conclusion in ["success", "failure", "cancelled", None]:
                step = {"name": d.DELIVERY_STEP, "conclusion": conclusion, "started_at": "2026-10-10T11:00:00Z"}
                with self.subTest(event=event, conclusion=conclusion), self.assertRaises(d.SafeError):
                    d.guard_github("2026-10-10", {**ENV, "GITHUB_EVENT_NAME": event}, self.make_get(step, current_event=event, previous_event=event))

    def test_manual_test_and_schedule_have_separate_daily_reservations(self):
        step = {"name": d.DELIVERY_STEP, "conclusion": "success", "started_at": "2026-10-10T02:40:20Z"}
        for current, previous, kind in [("schedule", "workflow_dispatch", "scheduled"), ("workflow_dispatch", "schedule", "manual-test")]:
            with self.subTest(current=current):
                get = self.make_get(step, current_event=current, previous_event=previous)
                self.assertEqual(d.guard_github("2026-10-10", {**ENV, "GITHUB_EVENT_NAME": current}, get), kind)
                self.assertIn("filter=all", get.call_args.args[0])

    def test_unknown_current_or_previous_event_fails_closed(self):
        for event in [None, "push", "pull_request", ""]:
            with self.subTest(current=event), self.assertRaises(d.SafeError):
                d.guard_github("2026-10-10", {**ENV, "GITHUB_EVENT_NAME": event}, self.make_get(current_event=event))
            with self.subTest(previous=event), self.assertRaises(d.SafeError):
                d.guard_github("2026-10-10", ENV, self.make_get(previous_event=event))

    def test_context_and_history_event_mismatches_fail_closed(self):
        with self.assertRaises(d.SafeError):
            d.guard_github("2026-10-10", {**ENV, "GITHUB_EVENT_NAME": "schedule"}, self.make_get())
        get = Mock(side_effect=[{"workflow_id": 77, "event": "workflow_dispatch"},
            {"total_count": 1, "workflow_runs": [{"id": 20, "event": "schedule"}]}])
        with self.assertRaises(d.SafeError):
            d.guard_github("2026-10-10", ENV, get)

    def test_other_class_incomplete_history_still_fails_closed(self):
        step = {"name": d.DELIVERY_STEP, "conclusion": "failure", "status": "completed"}
        with self.assertRaises(d.SafeError):
            d.guard_github("2026-10-10", ENV, self.make_get(step, previous_event="schedule"))

    def test_inconsistent_run_or_job_counts_fail_closed(self):
        current = {"workflow_id": 77, "event": "workflow_dispatch"}
        runs = [{"id": 20, "event": "workflow_dispatch"}, {"id": 19, "event": "schedule"}]
        for responses in [
            [current, {"total_count": 1, "workflow_runs": runs}],
            [current, {"total_count": 2, "workflow_runs": runs}, {"total_count": 1, "jobs": []}],
        ]:
            with self.subTest(responses=responses), self.assertRaises(d.SafeError):
                d.guard_github("2026-10-10", ENV, Mock(side_effect=responses))

    def test_previous_skipped_step_allows(self):
        d.guard_github("2026-10-10", ENV, self.make_get({"name": d.DELIVERY_STEP, "conclusion": "skipped"}))

    def test_previous_day_allows(self):
        step = {"name": d.DELIVERY_STEP, "conclusion": "success", "started_at": "2026-10-09T12:00:00Z"}
        d.guard_github("2026-10-10", ENV, self.make_get(step))

    def test_rerun_and_non_actions_block(self):
        for event in ["schedule", "workflow_dispatch"]:
            for env in [{**ENV, "GITHUB_EVENT_NAME": event, "GITHUB_RUN_ATTEMPT": "2"}, {**ENV, "GITHUB_EVENT_NAME": event, "GITHUB_ACTIONS": "false"}]:
                get = Mock()
                with self.assertRaises(d.SafeError):
                    d.guard_github("2026-10-10", env, get)
                get.assert_not_called()

    def test_history_truncation_or_sync_delay_fails_closed(self):
        with self.assertRaises(d.SafeError):
            d.guard_github("2026-10-10", ENV, self.make_get(total_count=100))
        get = Mock(side_effect=[{"workflow_id": 77, "event": "workflow_dispatch"}, {"total_count": 0, "workflow_runs": []}])
        with self.assertRaises(d.SafeError):
            d.guard_github("2026-10-10", ENV, get)

    def test_malformed_history_fails_closed(self):
        step = {"name": d.DELIVERY_STEP, "conclusion": "success", "status": "completed"}
        with self.assertRaises(d.SafeError):
            d.guard_github("2026-10-10", ENV, self.make_get(step))

    def test_all_job_attempts_are_requested(self):
        get = self.make_get()
        d.guard_github("2026-10-10", ENV, get)
        self.assertIn("filter=all", get.call_args.args[0])


class TransportTests(unittest.TestCase):
    def test_success_one_request_plain_text(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true,"result":{"message_id":5}}'
        d.send_message(ENV["TELEGRAM_BOT_TOKEN"], ENV["TELEGRAM_CHAT_ID"], "<untrusted>&_text", opener)
        opener.open.assert_called_once()
        req = opener.open.call_args.args[0]
        body = json.loads(req.data)
        self.assertNotIn("parse_mode", body)
        self.assertEqual(body["text"], "<untrusted>&_text")
        self.assertTrue(body["link_preview_options"]["is_disabled"])
        self.assertNotIn("allow_paid_broadcast", body)

    def test_uncertain_send_never_retries_or_leaks(self):
        token = ENV["TELEGRAM_BOT_TOKEN"]
        for error in [TimeoutError(token), urllib.error.URLError("https://api.telegram.org/bot" + token), ValueError(token)]:
            opener = MagicMock()
            opener.open.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(d.SafeError) as ctx:
                d.send_message(token, ENV["TELEGRAM_CHAT_ID"], "text", opener)
            opener.open.assert_called_once()
            self.assertNotIn(token, str(ctx.exception))
            self.assertNotIn(ENV["TELEGRAM_CHAT_ID"], str(ctx.exception))

    def test_rate_limit_never_blindly_retries(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("secret-url", 429, "rate limit", {}, None)
        with self.assertRaises(d.SafeError):
            d.send_message(ENV["TELEGRAM_BOT_TOKEN"], ENV["TELEGRAM_CHAT_ID"], "text", opener)
        opener.open.assert_called_once()

    def test_failed_api_result_is_not_success(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":false,"description":"private details"}'
        with self.assertRaises(d.SafeError) as ctx:
            d.send_message(ENV["TELEGRAM_BOT_TOKEN"], ENV["TELEGRAM_CHAT_ID"], "text", opener)
        self.assertNotIn("private details", str(ctx.exception))

    def test_partial_failure_stops_remaining_chunks(self):
        p = payload()
        p["messages"] = ["one", "two", "three"]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV, clear=True), \
             patch.object(d, "validate_delivery"), patch.object(d, "guard_github", return_value="manual-test"), \
             patch.object(d, "datetime") as dt, patch.object(d.time, "sleep"), \
             patch.object(d, "send_message", side_effect=[None, d.SafeError("uncertain")]) as send:
            dt.now.return_value = NOW
            with self.assertRaises(d.SafeError):
                d.deliver(p, tmp, NOW)
            self.assertEqual(send.call_count, 2)
            state = json.loads((Path(tmp) / "2026-10-10.manual-test.json").read_text())
            self.assertEqual(state["delivery_class"], "manual-test")
            self.assertEqual(state["confirmed_chunks"], 1)
            self.assertEqual(state["status"], "sending")
            # Local reservation also blocks an immediate restart.
            with self.assertRaises(d.SafeError):
                d.deliver(p, tmp, NOW)
            self.assertEqual(send.call_count, 2)

    def test_local_reservations_are_separate_and_each_class_blocks_repeats(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV, clear=True), \
             patch.object(d, "validate_delivery"), \
             patch.object(d, "guard_github", side_effect=["manual-test", "scheduled", "manual-test", "scheduled"]), \
             patch.object(d, "datetime") as dt, patch.object(d, "send_message") as send, \
             contextlib.redirect_stdout(io.StringIO()):
            dt.now.return_value = NOW
            d.deliver(payload(), tmp, NOW)
            d.deliver(payload(), tmp, NOW)
            self.assertEqual(send.call_count, 2)
            for kind in ["manual-test", "scheduled"]:
                state = json.loads((Path(tmp) / f"2026-10-10.{kind}.json").read_text())
                self.assertEqual(state["delivery_class"], kind)
                self.assertEqual(state["status"], "complete")
                with self.assertRaises(d.SafeError):
                    d.deliver(payload(), tmp, NOW)
            self.assertEqual(send.call_count, 2)

    def test_legacy_unclassified_local_reservation_blocks_both_classes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV, clear=True), \
             patch.object(d, "validate_delivery"), \
             patch.object(d, "guard_github", side_effect=["manual-test", "scheduled"]), \
             patch.object(d, "send_message") as send:
            (Path(tmp) / "2026-10-10.json").write_text('{"status":"sending"}')
            for kind in ["manual-test", "scheduled"]:
                with self.subTest(kind=kind), self.assertRaises(d.SafeError):
                    d.deliver(payload(), tmp, NOW)
            send.assert_not_called()

    def test_get_retries_read_only_network_failures(self):
        with patch.object(d.OPENER, "open", side_effect=TimeoutError()) as request, patch.object(d.time, "sleep"):
            with self.assertRaises(d.SafeError):
                d.get_bytes(d.SOURCE)
            self.assertEqual(request.call_count, 3)

    def test_redirects_are_not_followed(self):
        with self.assertRaises(d.SafeError):
            d.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test")

    def test_build_fixture_no_network_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True), \
             patch.object(d.OPENER, "open") as request, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(["build", "--input-html", str(FIXTURE), "--out", tmp]), 0)
            p = json.loads((Path(tmp) / "digest.json").read_text())
            self.assertTrue(p["fixture"])
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
