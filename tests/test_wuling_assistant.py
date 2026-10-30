import json
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import wuling_assistant as assistant
import wuling_booking as booking


class FakeResponse:
    def __init__(self, body, *, ok=True, url=booking.BASE_URL):
        self.body = body
        self.ok = ok
        self.url = url

    def text(self):
        return self.body

    def json(self):
        return self.body


def auth_page(home, orders):
    page = MagicMock()
    page.request.get.side_effect = [FakeResponse(home), FakeResponse(orders)]
    return page


class LoginTests(unittest.TestCase):
    def test_fresh_authenticated_responses_are_accepted(self):
        page = auth_page("<a id='wlogin_name'>會員</a>", "<p>訂單查詢</p>")
        assistant.verify_login(page)
        self.assertEqual(2, page.request.get.call_count)

    def test_visitor_and_stale_order_session_are_rejected(self):
        visitor = auth_page("<a id='wlogin_name'>未登入</a>", "<p>訂單</p>")
        with self.assertRaises(assistant.AuthError):
            assistant.verify_login(visitor)
        self.assertEqual(1, visitor.request.get.call_count)

        expired = auth_page("<a id='wlogin_name'>會員</a>", "<p>請先登入後才能查詢訂單</p>")
        with self.assertRaises(assistant.AuthError):
            assistant.verify_login(expired)

    def test_unexpected_order_response_fails_closed(self):
        page = auth_page("<a id='wlogin_name'>會員</a>", "<p>暫時無法服務</p>")
        with self.assertRaises(assistant.AuthError):
            assistant.verify_login(page)

    def test_transient_transport_failure_is_retried_once(self):
        page = MagicMock()
        page.request.get.side_effect = [
            TimeoutError("temporary"),
            FakeResponse("<a id='wlogin_name'>會員</a>"),
            FakeResponse("<p>訂單查詢</p>"),
        ]

        assistant.verify_login(page)

        self.assertEqual(3, page.request.get.call_count)
        self.assertGreaterEqual(page.wait_for_timeout.call_count, 3)


class BookingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.request = booking.BookingRequest("測試", "武陵國民賓館", 4, "2099-12-31")
        self.page = MagicMock()

    def test_temporary_hold_is_classified_as_pending_payment(self):
        result = assistant.classify_submission_response(
            "<table><tr><td>預訂結果</td><td>暫時保留</td></tr></table>"
            "<p>請於10分鐘內以信用卡完成訂金支付</p>"
            "<button>下一步 >> 信用卡付款</button>"
        )

        self.assertEqual("held_pending_payment", result["status"])
        self.assertTrue(result["task_success"])
        self.assertTrue(result["submitted"])
        self.assertEqual(10, result["payment_deadline_minutes"])

    def test_temporary_hold_does_not_pause_following_jobs(self):
        state = assistant.ServiceState()
        store = MagicMock()
        job = {
            "id": "JOB-20990101-000000-ABCDEF12",
            "kind": "booking",
            "payload": {"mode": "live", "people": 4, "date": "20991231"},
        }
        result = {
            "status": "held_pending_payment",
            "task_success": True,
            "submitted": True,
        }
        with patch.object(assistant, "book", return_value=result), \
             patch.object(booking, "save_result", return_value=Path("booking.json")):
            assistant._execute_job(self.page, job, store, state)

        self.assertIsNone(state.snapshot()["pause_reason"])

    def test_auth_failure_prevents_inventory_and_order_actions(self):
        with patch.object(assistant, "verify_login", side_effect=assistant.AuthError("失效")), \
             patch.object(booking, "_browser_open_target_date") as open_date:
            with self.assertRaises(assistant.AuthError):
                assistant.book(self.page, self.request)
        open_date.assert_not_called()
        self.page.locator.assert_not_called()

    def test_previous_submitted_log_blocks_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "booking-prior.json"
            path.write_text(json.dumps({
                "request": booking.asdict(self.request), "submitted": True,
            }), encoding="utf-8")
            self.assertTrue(assistant.existing_submission(self.request, Path(directory)))

    def test_auth_expiring_before_add_prevents_click(self):
        room_html = (
            "<p>民國188年12月31日</p>"
            "<div class='tt32'><table><tr><td><font color='blue'>本棟四人房</font>"
            "<font color='red'>NT 6790 起</font></td></tr>"
            "<tr><td><input type='button' id='AF2' value='預訂本房型 (尚餘1間)'>"
            "</td></tr></table></div>"
        )
        self.page.locator.return_value.inner_html.return_value = room_html
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(assistant, "verify_login", side_effect=[None, assistant.AuthError("失效")]), \
                 patch.object(assistant, "guard_path", return_value=Path(directory) / "guard"), \
                 patch.object(assistant, "existing_submission", return_value=False), \
                 patch.object(booking, "_browser_open_target_date"):
                with self.assertRaises(assistant.AuthError):
                    assistant.book(self.page, self.request)
        self.page.expect_response.assert_not_called()

    def test_checkout_requires_exact_room_date_and_one_item(self):
        selected = booking.Room("本棟四人房", "AF2", 6790, 1, True, "可訂")
        area = self.page.locator.return_value
        area.count.return_value = 1
        area.inner_text.return_value = "2099-12-31 本棟四人房"
        area.locator.return_value.count.return_value = 1
        self.assertTrue(assistant.checkout_matches(self.page, self.request, selected))
        area.locator.return_value.count.return_value = 2
        self.assertFalse(assistant.checkout_matches(self.page, self.request, selected))

    def test_session_expiring_before_submission_never_clicks_checkout(self):
        room_html = (
            "<p>民國188年12月31日</p>"
            "<div class='tt32'><table><tr><td><font color='blue'>本棟四人房</font>"
            "<font color='red'>NT 6790 起</font></td></tr>"
            "<tr><td><input type='button' id='AF2' value='預訂本房型 (尚餘1間)'>"
            "</td></tr></table></div>"
        )
        room_area, room_button, checkout_link, checkout = [MagicMock() for _ in range(4)]
        room_area.inner_html.return_value = room_html
        checkout_link.count.return_value = 1
        checkout.count.return_value = 1
        checkout.is_disabled.return_value = False
        self.page.locator.side_effect = lambda selector: {
            "#show_room_area": room_area,
            "#AF2": room_button,
            "#search_order999999": checkout_link,
            "#agree_and_order": checkout,
        }[selector]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(assistant, "verify_login", side_effect=[None, None, None, assistant.AuthError("失效")]), \
                 patch.object(assistant, "guard_path", return_value=Path(directory) / "guard"), \
                 patch.object(assistant, "existing_submission", return_value=False), \
                 patch.object(assistant, "checkout_matches", return_value=True), \
                 patch.object(booking, "_browser_open_target_date"):
                with self.assertRaises(assistant.AuthError):
                    assistant.book(self.page, self.request)
        room_button.click.assert_called_once()
        checkout_link.click.assert_called_once()
        checkout.click.assert_not_called()

    def test_standby_never_selects_date_before_calibrated_open_time(self):
        target = datetime(2099, 1, 1, 8, 30, tzinfo=timezone.utc)
        before = target - timedelta(seconds=1)
        calls = MagicMock()
        page = MagicMock()
        result = {"status": "sold_out", "submitted": False}
        with patch.object(assistant, "verify_login"), \
             patch.object(assistant, "guard_path", return_value=Path("missing")), \
             patch.object(assistant, "existing_submission", return_value=False), \
             patch.object(assistant, "calibrate_site_clock", return_value={
                 "offset_seconds": 0.0, "round_trip_ms": 1.0,
                 "server_date": "date", "measured_at": "now", "samples": 3,
             }), \
             patch.object(assistant, "_wait_until_site_time"), \
             patch.object(
                 assistant, "_site_now",
                 side_effect=[before, before, before, target, target],
             ), \
             patch.object(
                 booking, "_browser_prepare_target_month", calls.prepare
             ), \
             patch.object(
                 booking, "_browser_target_date_available", return_value=True
             ), \
             patch.object(
                 booking, "_browser_select_target_date", calls.select
             ), \
             patch.object(assistant, "book", return_value=result):
            actual = assistant.book_at_open(
                page,
                self.request,
                open_at=target,
                standby_seconds=15,
                refresh_interval_ms=1500,
            )

        self.assertEqual(
            [call.prepare(page, datetime(2099, 12, 31).date()),
             call.prepare(page, datetime(2099, 12, 31).date()),
             call.prepare(page, datetime(2099, 12, 31).date()),
             call.select(page, datetime(2099, 12, 31).date())],
            calls.mock_calls,
        )
        self.assertEqual(2, actual["timing"]["refresh_attempts"])

    def test_standby_backs_off_when_site_requests_slower_actions(self):
        target = datetime(2099, 1, 1, 8, 30, tzinfo=timezone.utc)
        page = MagicMock()
        with patch.object(assistant, "verify_login"), \
             patch.object(assistant, "guard_path", return_value=Path("missing")), \
             patch.object(assistant, "existing_submission", return_value=False), \
             patch.object(assistant, "calibrate_site_clock", return_value={
                 "offset_seconds": 0.0, "round_trip_ms": 1.0,
                 "server_date": "date", "measured_at": "now", "samples": 3,
             }), \
             patch.object(assistant, "_wait_until_site_time"), \
             patch.object(
                 assistant, "_site_now",
                 side_effect=[target, target, target, target],
             ), \
             patch.object(
                 booking, "_browser_prepare_target_month",
                 side_effect=[None, booking.BookingError("HTTP 429，請放慢"), None],
             ), \
             patch.object(
                 booking, "_browser_target_date_available", return_value=True
             ), \
             patch.object(booking, "_browser_select_target_date"), \
             patch.object(assistant.secrets, "randbelow", return_value=0), \
             patch.object(
                 assistant, "book",
                 return_value={"status": "sold_out", "submitted": False},
             ):
            actual = assistant.book_at_open(
                page,
                self.request,
                open_at=target,
                standby_seconds=15,
                refresh_interval_ms=1500,
            )

        page.wait_for_timeout.assert_called_once_with(3000)
        self.assertEqual(1, actual["timing"]["throttle_backoffs"])
        self.assertEqual(3000, actual["timing"]["last_backoff_ms"])


class SiteClockTests(unittest.TestCase):
    def test_http_date_offset_uses_round_trip_midpoint(self):
        started = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
        received = started + timedelta(milliseconds=200)
        server = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)

        offset = assistant._estimate_site_clock_offset(
            server, started, received
        )

        self.assertAlmostEqual(0.4, offset, places=6)

    def test_open_at_requires_explicit_timezone(self):
        with self.assertRaisesRegex(ValueError, "時區"):
            assistant._parse_open_at("2026-11-01T08:30:00")
        parsed = assistant._parse_open_at("2026-11-01T08:30:00+08:00")
        self.assertEqual(timedelta(hours=8), parsed.utcoffset())


class AssistantLifecycleTests(unittest.TestCase):
    def test_payment_tab_stays_visible_while_background_tab_is_prepared(self):
        payment_page = MagicMock()
        next_page = MagicMock()
        payment_page.context.new_page.return_value = next_page

        result = assistant._prepare_next_booking_page(payment_page)

        self.assertIs(next_page, result)
        payment_page.context.new_page.assert_called_once_with()
        payment_page.bring_to_front.assert_called_once_with()

    def test_browser_closes_only_after_service_stop(self):
        context = MagicMock()
        page = MagicMock()
        context.pages = [page]
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = MagicMock()
        manager.__enter__.return_value = playwright
        api = types.ModuleType("playwright.sync_api")
        api.sync_playwright = lambda: manager

        class FakeServer:
            def __init__(self, address, token, store, pending, state, stop_event):
                self.server_address = (address[0], 54321)
                stop_event.set()

            def serve_forever(self, **_kwargs):
                return None

            def shutdown(self):
                return None

            def server_close(self):
                return None

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules, {"playwright.sync_api": api}), \
             patch.object(assistant, "verify_login") as verify, \
             patch.object(assistant, "AssistantHttpServer", FakeServer):
            root = Path(directory)
            self.assertEqual(
                0,
                assistant.run_assistant(
                    profile=root / "profile", service_dir=root / "service", port=0
                ),
            )
        self.assertEqual(1, verify.call_count)
        context.close.assert_called_once()
        self.assertFalse(playwright.chromium.launch_persistent_context.call_args.kwargs["headless"])

    def test_failed_browser_launch_preserves_existing_runtime(self):
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.side_effect = RuntimeError(
            "profile already in use"
        )
        manager = MagicMock()
        manager.__enter__.return_value = playwright
        api = types.ModuleType("playwright.sync_api")
        api.sync_playwright = lambda: manager

        class FakeServer:
            def __init__(self, address, token, store, pending, state, stop_event):
                self.server_address = (address[0], 54322)

            def serve_forever(self, **_kwargs):
                return None

            def shutdown(self):
                return None

            def server_close(self):
                return None

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules, {"playwright.sync_api": api}), \
             patch.object(assistant, "AssistantHttpServer", FakeServer):
            root = Path(directory)
            service = root / "service"
            service.mkdir()
            runtime = service / "runtime.json"
            original = {"pid": 123, "port": 18765, "token": "existing"}
            runtime.write_text(json.dumps(original), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "profile already in use"):
                assistant.run_assistant(
                    profile=root / "profile", service_dir=service, port=0
                )

            self.assertEqual(original, json.loads(runtime.read_text(encoding="utf-8")))


class QueueTests(unittest.TestCase):
    def test_paused_queue_still_selects_status_job(self):
        pending = assistant.PendingJobs()
        pending.put("JOB-BOOKING", "booking")
        pending.put("JOB-STATUS", "status")
        self.assertEqual(("JOB-STATUS", "status"), pending.get(False, timeout=0))
        self.assertEqual(("JOB-BOOKING", "booking"), pending.get(True, timeout=0))

    def test_future_booking_does_not_block_ready_work(self):
        pending = assistant.PendingJobs()
        pending.put(
            "JOB-FUTURE", "booking", assistant.time.monotonic() + 60
        )
        pending.put("JOB-NOW", "booking")

        self.assertEqual(("JOB-NOW", "booking"), pending.get(True, timeout=0))
        self.assertIsNone(pending.get(True, timeout=0))

    def test_recovery_requeues_rehearsal_but_never_live(self):
        with tempfile.TemporaryDirectory() as directory:
            store = assistant.JobStore(Path(directory))
            rehearsal = store.create("booking", {
                "mode": "rehearsal", "people": 4, "date": "20991231",
            })
            live = store.create("booking", {
                "mode": "live", "people": 6, "date": "20991231",
            })
            store.update(rehearsal["id"], state="running")
            store.update(live["id"], state="running")
            recovered, live_interrupted = store.recover()
            self.assertTrue(live_interrupted)
            self.assertEqual([rehearsal["id"]], [item["id"] for item in recovered])
            self.assertEqual("queued", store.get(rehearsal["id"])["state"])
            self.assertEqual("interrupted", store.get(live["id"])["state"])
            self.assertEqual(
                "submit_unknown", store.get(live["id"])["result"]["status"]
            )

    def test_recovery_safely_requeues_live_job_still_in_standby(self):
        with tempfile.TemporaryDirectory() as directory:
            store = assistant.JobStore(Path(directory))
            job = store.create("booking", {
                "mode": "live", "people": 2, "date": "20991231",
                "open_at": "2099-11-01T08:30:00+08:00",
                "standby_seconds": 15, "refresh_interval_ms": 1500,
            })
            store.update(job["id"], state="running", phase="standby")

            recovered, live_interrupted = store.recover()

            self.assertFalse(live_interrupted)
            self.assertEqual([job["id"]], [item["id"] for item in recovered])
            self.assertEqual("queued", store.get(job["id"])["state"])

    def test_only_queued_job_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = assistant.JobStore(Path(directory))
            queued = store.create("status", {})
            self.assertEqual("cancelled", store.cancel(queued["id"])["state"])
            running = store.create("status", {})
            store.update(running["id"], state="running")
            with self.assertRaises(RuntimeError):
                store.cancel(running["id"])

    def test_login_check_never_clears_unknown_submission_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            store = assistant.JobStore(Path(directory))
            state = assistant.ServiceState()
            state.set(pause_reason="submission_unknown")
            failed_check = store.create("status", {})
            with patch.object(
                assistant, "verify_login", side_effect=assistant.AuthError("失效")
            ):
                assistant._execute_job(MagicMock(), failed_check, store, state)
            self.assertEqual("submission_unknown", state.snapshot()["pause_reason"])

            successful_check = store.create("status", {})
            with patch.object(assistant, "verify_login"):
                assistant._execute_job(MagicMock(), successful_check, store, state)
            self.assertEqual("submission_unknown", state.snapshot()["pause_reason"])


class LocalApiTests(unittest.TestCase):
    def test_scheduled_live_booking_validates_standby_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            store = assistant.JobStore(Path(directory))
            pending = assistant.PendingJobs()
            state = assistant.ServiceState()
            stop_event = __import__("threading").Event()
            token = "test-token"
            server = assistant.AssistantHttpServer(
                ("127.0.0.1", 0), token, store, pending, state, stop_event
            )
            thread = __import__("threading").Thread(
                target=server.serve_forever, daemon=True
            )
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/v1/jobs"

            def request_for(standby):
                body = json.dumps({
                    "kind": "booking", "mode": "live", "people": 2,
                    "date": "20991231",
                    "open_at": "2099-11-01T08:30:00+08:00",
                    "standby_seconds": standby,
                    "refresh_interval_ms": 1500,
                }).encode("utf-8")
                return urllib.request.Request(
                    url, data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Wuling-Token": token,
                    },
                )

            try:
                with urllib.request.urlopen(request_for(15), timeout=3) as response:
                    created = json.load(response)
                self.assertEqual(15, created["payload"]["standby_seconds"])
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request_for(9), timeout=3)
                self.assertEqual(400, caught.exception.code)
                caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_concurrent_submissions_are_accepted_with_unique_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = assistant.JobStore(Path(directory))
            pending = assistant.PendingJobs()
            state = assistant.ServiceState()
            stop_event = __import__("threading").Event()
            token = "test-token"
            server = assistant.AssistantHttpServer(
                ("127.0.0.1", 0), token, store, pending, state, stop_event
            )
            thread = __import__("threading").Thread(
                target=server.serve_forever, daemon=True
            )
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def submit(index):
                body = json.dumps({
                    "kind": "booking",
                    "mode": "rehearsal",
                    "people": 4,
                    "date": f"209912{index + 1:02d}",
                }).encode("utf-8")
                request = urllib.request.Request(
                    base + "/v1/jobs", data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Wuling-Token": token,
                    },
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    return json.load(response)["id"]

            try:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    identifiers = list(executor.map(submit, range(8)))
                self.assertEqual(8, len(set(identifiers)))
                self.assertEqual(8, len(store.list()))
                unauthorized = urllib.request.Request(base + "/v1/jobs")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(unauthorized, timeout=3)
                self.assertEqual(401, caught.exception.code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
