import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "tools" / "wuling_booking.py"
SPEC = importlib.util.spec_from_file_location("wuling_booking", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BookingCommandTests(unittest.TestCase):
    def test_extracts_and_validates_numbered_shard_redirect(self) -> None:
        body = (
            '<meta http-equiv="refresh" '
            'content="1;url=https://whotel002.wuling-farm.com.tw/whotel">'
        )
        target = MODULE.site_redirect_url(body)
        self.assertEqual("https://whotel002.wuling-farm.com.tw/whotel", target)
        self.assertEqual(
            "https://whotel002.wuling-farm.com.tw/whotel/",
            MODULE.site_base_url(target),
        )

    def test_rejects_untrusted_meta_refresh_target(self) -> None:
        body = '<meta http-equiv="refresh" content="0;url=https://example.com/whotel">'
        with self.assertRaises(MODULE.BookingError):
            MODULE.site_redirect_url(body)

    def test_parses_requested_command(self) -> None:
        request = MODULE.parse_request("預定武陵國民賓館 4人房，20991231。")
        self.assertEqual(4, request.people)
        self.assertEqual("2099-12-31", request.stay_date)

    def test_rejects_invalid_command(self) -> None:
        with self.assertRaises(MODULE.BookingError):
            MODULE.parse_request("幫我訂房")

    def test_parses_available_and_sold_out_rooms(self) -> None:
        room_html = """
        <font color='blue'>民國115年11月13日</font>
        <div class='tt32'><table>
          <tr><td><font color='blue'>本棟四人房</font><font color='red'>NT 6790 起</font></td></tr>
          <tr><td><input type='button' value='預訂本房型 (尚餘8間)' id='AF2'></td></tr>
        </table></div>
        <div class='tt32'><table>
          <tr><td><font color='blue'>桃花莊四人房</font><font color='red'>NT 7350 起</font></td></tr>
          <tr><td><input type='button' value='本房型已售罄'></td></tr>
        </table></div>
        """
        rooms = MODULE.parse_room_cards(room_html)
        self.assertEqual(2, len(rooms))
        self.assertTrue(rooms[0].available)
        self.assertEqual(8, rooms[0].remaining)
        self.assertFalse(rooms[1].available)

    def test_parses_roc_response_date(self) -> None:
        self.assertEqual(
            date(2026, 11, 14),
            MODULE.parse_roc_date("<p>民國115年11月14日(星期六)</p>"),
        )

    def test_waits_until_calendar_dom_reaches_expected_month(self) -> None:
        page = MagicMock()
        area = page.locator.return_value
        area.inner_html.side_effect = [
            "<div>民國 115 年 12 月</div>",
            "<div>民國 116 年 1 月</div>",
        ]

        value = MODULE._wait_for_calendar_month(page, 2027, 1, timeout_ms=1_000)

        self.assertEqual((2027, 1), value)
        page.wait_for_timeout.assert_called_once_with(100)

    def test_site_action_delay_uses_requested_random_range(self) -> None:
        page = MagicMock()
        with patch.object(MODULE.random, "randint", return_value=650) as choose:
            delay = MODULE.wait_site_action_delay(page)

        self.assertEqual(650, delay)
        choose.assert_called_once_with(200, 900)
        page.wait_for_timeout.assert_called_once_with(650)

    def test_live_mode_short_circuits_before_browser_when_sold_out(self) -> None:
        class SoldOutClient:
            def search(self, stay_date, people):
                return {
                    "status": "sold_out",
                    "calendar": "2099-12",
                    "rooms": [],
                    "matching_rooms": [],
                    "selected": None,
                }

        original_client = MODULE.AvailabilityClient
        original_live = MODULE.execute_live_booking
        MODULE.AvailabilityClient = SoldOutClient
        MODULE.execute_live_booking = lambda *args, **kwargs: self.fail(
            "Live booking must not run for sold-out inventory."
        )
        try:
            result = MODULE.run(
                "預定武陵國民賓館 4人房，20991231。",
                "live",
                PROJECT_ROOT / "Secret" / "missing.ini",
                False,
            )
        finally:
            MODULE.AvailabilityClient = original_client
            MODULE.execute_live_booking = original_live

        self.assertEqual("sold_out", result["status"])
        self.assertFalse(result["submitted"])
        self.assertEqual("售罄", MODULE.format_status(result))


if __name__ == "__main__":
    unittest.main()
