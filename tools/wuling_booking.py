#!/usr/bin/env python3
"""Search Wuling hotel rooms and optionally complete a booking.

Rehearsal mode is read-only. Live mode always performs the same anonymous
availability check first and exits immediately when all matching rooms are sold
out, before reading credentials or starting a browser.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://whotel.wuling-farm.com.tw/whotel/"
COMMAND_PATTERN = re.compile(
    r"^\s*(?:預定|預訂)\s*武陵國民賓館\s*"
    r"(?P<people>\d+)\s*人房\s*[，,]?\s*"
    r"(?P<day>20\d{6})\s*[。.]?\s*$"
)
ROC_MONTH_PATTERN = re.compile(r"民國\s*(\d+)\s*年\s*(\d+)\s*月")
ROC_DATE_PATTERN = re.compile(r"民國\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")
SITE_ACTION_DELAY_MIN_MS = 200
SITE_ACTION_DELAY_MAX_MS = 900


class BookingError(RuntimeError):
    """Raised when a booking stage cannot continue safely."""


@dataclass(frozen=True)
class BookingRequest:
    raw: str
    hotel: str
    people: int
    stay_date: str


@dataclass(frozen=True)
class Room:
    name: str
    code: str | None
    price: int | None
    remaining: int
    available: bool
    status_text: str


class _RoomCardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._font_color = ""
        self._blue_text: list[str] = []
        self._red_text: list[str] = []
        self.inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "font":
            self._font_color = values.get("color", "").lower()
        elif tag.lower() == "input":
            self.inputs.append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "font":
            self._font_color = ""

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._font_color == "blue":
            self._blue_text.append(text)
        elif self._font_color == "red":
            self._red_text.append(text)

    @property
    def name(self) -> str:
        return self._blue_text[0] if self._blue_text else ""

    @property
    def price(self) -> int | None:
        text = " ".join(self._red_text)
        match = re.search(r"NT\s*\$?\s*([0-9,]+)", text, re.IGNORECASE)
        return int(match.group(1).replace(",", "")) if match else None


class AvailabilityClient:
    """Anonymous, read-only client for calendar and room inventory."""

    def __init__(self, timeout: int = 20) -> None:
        cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )
        self.timeout = timeout
        self.headers = {"User-Agent": "MH-WuLing-Rehearsal/1.0"}

    def _get_text(self, relative_url: str) -> str:
        url = urllib.parse.urljoin(BASE_URL, relative_url)
        request = urllib.request.Request(url, headers=self.headers)
        with self.opener.open(request, timeout=self.timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")

    def _get_json_html(self, relative_url: str) -> str:
        payload = json.loads(self._get_text(relative_url))
        if not isinstance(payload, str):
            raise BookingError(f"Unexpected response from {relative_url}")
        return payload

    def search(self, stay_date: date, people: int) -> dict[str, Any]:
        self._get_text("")
        calendar_html = self._get_json_html("func_select.php?func=1")
        time.sleep(0.8)
        current_year, current_month = parse_roc_month(calendar_html)
        month_delta = (stay_date.year - current_year) * 12 + stay_date.month - current_month
        if abs(month_delta) > 24:
            raise BookingError("Requested month is outside the supported 24-month window.")

        direction = 3 if month_delta > 0 else 2
        for _ in range(abs(month_delta)):
            calendar_html = self._get_json_html(f"func_select.php?func={direction}")
            time.sleep(0.8)

        shown_year, shown_month = parse_roc_month(calendar_html)
        if (shown_year, shown_month) != (stay_date.year, stay_date.month):
            return {
                "status": "date_unavailable",
                "calendar": f"{shown_year:04d}-{shown_month:02d}",
                "rooms": [],
                "matching_rooms": [],
                "selected": None,
            }

        room_html = self._get_json_html(
            f"change_date.php?number={stay_date.day:02d}"
        )
        response_date = parse_roc_date(room_html)
        if response_date != stay_date:
            raise BookingError(
                f"Inventory response date mismatch: expected {stay_date}, got {response_date}."
            )

        rooms = parse_room_cards(room_html)
        marker = people_marker(people)
        matching = [room for room in rooms if marker in room.name]
        available = [room for room in matching if room.available]
        selected = available[0] if available else None

        return {
            "status": "available" if selected else "sold_out",
            "calendar": f"{shown_year:04d}-{shown_month:02d}",
            "rooms": rooms,
            "matching_rooms": matching,
            "selected": selected,
        }


def search_with_browser(stay_date: date, people: int) -> dict[str, Any]:
    """Anonymous Chromium fallback when the host rejects Python HTTP sockets."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BookingError("Playwright is missing. Run PS\\Setup.ps1 first.") from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="zh-TW")
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20_000)
            calendar_html = page.evaluate(
                "async () => await (await fetch('func_select.php?func=1')).json()"
            )
            page.wait_for_timeout(1_000)
            current_year, current_month = parse_roc_month(calendar_html)
            month_delta = (
                (stay_date.year - current_year) * 12
                + stay_date.month
                - current_month
            )
            if abs(month_delta) > 24:
                raise BookingError(
                    "Requested month is outside the supported 24-month window."
                )
            direction = 3 if month_delta > 0 else 2
            for _ in range(abs(month_delta)):
                calendar_html = page.evaluate(
                    "async direction => await "
                    "(await fetch('func_select.php?func=' + direction)).json()",
                    direction,
                )
                page.wait_for_timeout(1_000)
            shown_year, shown_month = parse_roc_month(calendar_html)
            if (shown_year, shown_month) != (stay_date.year, stay_date.month):
                return {
                    "status": "date_unavailable",
                    "calendar": f"{shown_year:04d}-{shown_month:02d}",
                    "rooms": [],
                    "matching_rooms": [],
                    "selected": None,
                }
            room_html = page.evaluate(
                "async day => await "
                "(await fetch('change_date.php?number=' + day)).json()",
                f"{stay_date.day:02d}",
            )
            response_date = parse_roc_date(room_html)
            if response_date != stay_date:
                raise BookingError(
                    f"Inventory response date mismatch: expected {stay_date}, "
                    f"got {response_date}."
                )
            rooms = parse_room_cards(room_html)
            marker = people_marker(people)
            matching = [room for room in rooms if marker in room.name]
            available = [room for room in matching if room.available]
            selected = available[0] if available else None
            return {
                "status": "available" if selected else "sold_out",
                "calendar": f"{shown_year:04d}-{shown_month:02d}",
                "rooms": rooms,
                "matching_rooms": matching,
                "selected": selected,
            }
        finally:
            browser.close()


def search_inventory(stay_date: date, people: int) -> dict[str, Any]:
    try:
        return AvailabilityClient().search(stay_date, people)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        try:
            return search_with_browser(stay_date, people)
        except BookingError:
            raise
        except Exception as exc:
            raise BookingError(f"Inventory lookup failed: {exc}") from exc


def parse_request(command: str) -> BookingRequest:
    match = COMMAND_PATTERN.fullmatch(command)
    if not match:
        raise BookingError(
            "Command format must be: 預定武陵國民賓館 4人房，20261114"
        )
    people = int(match.group("people"))
    if people < 1 or people > 10:
        raise BookingError("Room occupancy must be between 1 and 10 people.")
    try:
        stay_date = datetime.strptime(match.group("day"), "%Y%m%d").date()
    except ValueError as exc:
        raise BookingError("The requested date is invalid.") from exc
    if stay_date < date.today():
        raise BookingError("The requested date is in the past.")
    return BookingRequest(
        raw=command,
        hotel="武陵國民賓館",
        people=people,
        stay_date=stay_date.isoformat(),
    )


def parse_roc_month(calendar_html: str) -> tuple[int, int]:
    text = strip_tags(calendar_html)
    match = ROC_MONTH_PATTERN.search(text)
    if not match:
        raise BookingError("Could not determine the calendar month.")
    return int(match.group(1)) + 1911, int(match.group(2))


def parse_roc_date(room_html: str) -> date:
    text = strip_tags(room_html)
    match = ROC_DATE_PATTERN.search(text)
    if not match:
        raise BookingError("Could not verify the inventory response date.")
    return date(int(match.group(1)) + 1911, int(match.group(2)), int(match.group(3)))


def strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def parse_room_cards(room_html: str) -> list[Room]:
    cards = re.findall(
        r"(?is)<div\s+class=['\"]tt32['\"].*?</table>\s*</div>", room_html
    )
    rooms: list[Room] = []
    for card in cards:
        parser = _RoomCardParser()
        parser.feed(card)
        if not parser.name:
            continue
        button = next(
            (
                item
                for item in parser.inputs
                if item.get("type", "").lower() == "button"
            ),
            {},
        )
        value = button.get("value", "")
        remaining_match = re.search(r"尚餘\s*(\d+)\s*間", value)
        remaining = int(remaining_match.group(1)) if remaining_match else 0
        code = button.get("id") or None
        available = bool(code and remaining > 0 and "已售罄" not in value)
        rooms.append(
            Room(
                name=parser.name,
                code=code,
                price=parser.price,
                remaining=remaining,
                available=available,
                status_text=value or "無可訂按鈕",
            )
        )
    return rooms


def people_marker(people: int) -> str:
    chinese = {
        1: "一人",
        2: "雙人",
        3: "三人",
        4: "四人",
        5: "五人",
        6: "6人",
        7: "七人",
        8: "八人",
        9: "九人",
        10: "十人",
    }
    return chinese.get(people, f"{people}人")


def load_config(path: Path) -> dict[str, str]:
    if not path.exists():
        raise BookingError(f"Config not found: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().lower()] = value.strip()
    return values


def _is_logged_in(page: Any) -> bool:
    login_name = page.locator("#wlogin_name")
    return login_name.count() > 0 and "未登入" not in login_name.inner_text()


def _browser_login(page: Any, config: dict[str, str]) -> bool:
    from whotel_captcha import recognize_captcha

    phone = config.get("phone", "")
    password = config.get("pwd", "")
    if not phone or not password:
        raise BookingError("Phone or pwd is missing from Secret.ini.")

    wait_site_action_delay(page)
    page.evaluate("func_click(4)")
    page.locator("#captcha").wait_for(state="visible", timeout=10_000)
    for _ in range(3):
        image_bytes = page.locator("#captcha").screenshot(type="png")
        ocr = recognize_captcha(image_bytes)
        if not ocr["accepted"]:
            page.eval_on_selector(
                "#captcha", "image => image.src = image.src + '?' + Date.now()"
            )
            wait_site_action_delay(page)
            continue
        page.locator("#whotelloginid").fill(phone)
        page.locator("#whotelpassword").fill(password)
        page.locator("#captcha_value").fill(ocr["text"])
        with page.expect_response(
            lambda response: "func_select.php?func=41" in response.url,
            timeout=15_000,
        ):
            page.locator("input[onclick*='func_click(41)']").click()
        wait_site_action_delay(page)
        if _is_logged_in(page):
            return True
        page.evaluate("func_click(4)")
        page.locator("#captcha").wait_for(state="visible", timeout=10_000)
    return False


def _browser_open_target_date(page: Any, stay_date: date) -> None:
    # The site explicitly rejects clicks issued too quickly. Use real DOM clicks
    # and keep a small server-friendly interval between every calendar action.
    wait_site_action_delay(page)
    date_nav = page.locator("a[onclick*='func_click(1)']")
    if date_nav.count() == 0:
        # Normal persistent-assistant jobs stay on the current same-origin page.
        # Fall back to the home page only if the shared navigation is unavailable.
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20_000)
        date_nav = page.locator("a[onclick*='func_click(1)']")
        date_nav.wait_for(state="visible", timeout=10_000)
        wait_site_action_delay(page)
    with page.expect_response(
        lambda response: "func_select.php?func=1" in response.url,
        timeout=15_000,
    ) as response_info:
        date_nav.click()
    _finish_site_response(response_info, "開啟日曆")
    page.locator("#calendar_area").wait_for(state="visible", timeout=10_000)
    current_year, current_month = _wait_for_calendar_month(page)
    wait_site_action_delay(page)
    month_delta = (stay_date.year - current_year) * 12 + stay_date.month - current_month
    direction = 3 if month_delta > 0 else 2
    for _ in range(abs(month_delta)):
        if direction == 3:
            expected_year = current_year + (1 if current_month == 12 else 0)
            expected_month = 1 if current_month == 12 else current_month + 1
        else:
            expected_year = current_year - (1 if current_month == 1 else 0)
            expected_month = 12 if current_month == 1 else current_month - 1
        endpoint = f"func_select.php?func={direction}"
        with page.expect_response(
            lambda response, endpoint=endpoint: endpoint in response.url,
            timeout=15_000,
        ) as response_info:
            page.locator(f"[onclick*='func_click({direction})']").click()
        _finish_site_response(response_info, "切換日曆月份")
        _wait_for_calendar_month(page, expected_year, expected_month)
        current_year, current_month = expected_year, expected_month
        wait_site_action_delay(page)
    selector = f"#calendar_day{stay_date.day:02d}"
    if page.locator(selector).count() == 0:
        raise BookingError("目標日期目前不在可選日曆中，操作已停止。")
    with page.expect_response(
        lambda response: "change_date.php?number=" in response.url,
        timeout=15_000,
    ) as response_info:
        page.locator(selector).click()
    _finish_site_response(response_info, "選擇住宿日期")
    _wait_for_inventory_date(page, stay_date)
    wait_site_action_delay(page)


def wait_site_action_delay(page: Any) -> int:
    """Pause for a randomized 0.2-0.9 seconds and return the chosen duration."""
    delay_ms = random.randint(SITE_ACTION_DELAY_MIN_MS, SITE_ACTION_DELAY_MAX_MS)
    page.wait_for_timeout(delay_ms)
    return delay_ms


def _finish_site_response(response_info: Any, action: str) -> None:
    """Wait for a site action response and reject throttled or failed requests."""
    response = response_info.value
    response.finished()
    if not response.ok:
        raise BookingError(
            f"{action}失敗（HTTP {response.status}）；網站可能要求放慢操作速度。"
        )


def _wait_for_calendar_month(
    page: Any,
    expected_year: int | None = None,
    expected_month: int | None = None,
    timeout_ms: int = 10_000,
) -> tuple[int, int]:
    """Wait until the calendar DOM is parsed and, when supplied, is the expected month."""
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        try:
            value = parse_roc_month(page.locator("#calendar_area").inner_html())
        except (BookingError, ValueError):
            value = None
        if value is not None and (
            expected_year is None
            or value == (expected_year, expected_month)
        ):
            return value
        page.wait_for_timeout(100)
    if expected_year is None:
        raise BookingError("無法確認日曆月份，操作已停止。")
    raise BookingError(
        f"日曆未切換至 {expected_year:04d}-{expected_month:02d}，操作已停止。"
    )


def _wait_for_inventory_date(
    page: Any, stay_date: date, timeout_ms: int = 10_000
) -> None:
    """Wait for the room DOM to identify the selected date instead of reading stale HTML."""
    area = page.locator("#show_room_area")
    area.wait_for(state="visible", timeout=timeout_ms)
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        try:
            if parse_roc_date(area.inner_html()) == stay_date:
                return
        except (BookingError, ValueError):
            pass
        page.wait_for_timeout(100)
    raise BookingError("房型日期無法確認，操作已停止。")


def execute_live_booking(
    request: BookingRequest,
    selected: Room,
    config_path: Path,
    headed: bool,
) -> dict[str, Any]:
    """Login and submit only after the anonymous availability gate passed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BookingError("Playwright is missing. Run PS\\Setup.ps1 first.") from exc

    config = load_config(config_path)
    stay_date = date.fromisoformat(request.stay_date)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        try:
            context = browser.new_context(locale="zh-TW", viewport={"width": 1280, "height": 900})
            session_id = config.get("whotel_session", "")
            if session_id:
                context.add_cookies(
                    [
                        {
                            "name": "PHPSESSID",
                            "value": session_id,
                            "domain": "whotel.wuling-farm.com.tw",
                            "path": "/",
                            "secure": True,
                        }
                    ]
                )
            page = context.new_page()
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20_000)
            if not _is_logged_in(page) and not _browser_login(page, config):
                return {"status": "login_failed", "submitted": False}

            _browser_open_target_date(page, stay_date)
            room_area = page.locator("#show_room_area")
            room_area.wait_for(state="visible", timeout=10_000)
            live_rooms = parse_room_cards(room_area.inner_html())
            live_selected = next(
                (room for room in live_rooms if room.code == selected.code), None
            )
            if live_selected is None or not live_selected.available:
                return {"status": "sold_out", "submitted": False}
            room_button = page.locator(f"#{live_selected.code}")

            with page.expect_response(
                lambda response: "room_order.php?roomtype=" in response.url,
                timeout=15_000,
            ):
                room_button.click()
            wait_site_action_delay(page)

            checkout_link = page.locator("#search_order999999")
            if checkout_link.count() == 0:
                return {"status": "checkout_blocked", "submitted": False}
            with page.expect_response(
                lambda response: "room_order.php?roomtype=999999" in response.url,
                timeout=15_000,
            ):
                checkout_link.click()
            wait_site_action_delay(page)

            checkout = page.locator("#agree_and_order")
            if checkout.count() == 0:
                return {"status": "checkout_blocked", "submitted": False}
            if selected.name not in page.locator("body").inner_text():
                return {"status": "selection_mismatch", "submitted": False}

            checkboxes = page.locator("#show_room_area input[type='checkbox']")
            for index in range(checkboxes.count()):
                checkbox = checkboxes.nth(index)
                if checkbox.is_visible() and checkbox.is_enabled() and not checkbox.is_checked():
                    checkbox.check()
            if checkout.is_disabled():
                return {"status": "checkout_blocked", "submitted": False}

            with page.expect_response(
                lambda response: "func_select.php?func=5" in response.url,
                timeout=20_000,
            ) as response_info:
                checkout.click()
            wait_site_action_delay(page)
            response_text = response_info.value.text()
            try:
                response_text = json.loads(response_text)
            except json.JSONDecodeError:
                pass
            result_text = strip_tags(str(response_text))
            order_match = re.search(
                r"(?:訂單編號|訂單號碼)\s*[:：]?\s*([A-Za-z0-9-]+)", result_text
            )
            success = any(
                marker in result_text
                for marker in ("訂房成功", "預訂成功", "訂房完成", "訂單編號", "訂單號碼")
            )
            return {
                "status": "booked" if success else "submit_unknown",
                "submitted": True,
                "order_number": order_match.group(1) if order_match else None,
            }
        finally:
            browser.close()


def verify_existing_booking(
    request: BookingRequest,
    config_path: Path,
    headed: bool,
) -> dict[str, Any]:
    """Read the authenticated order list without adding or submitting a room."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BookingError("Playwright is missing. Run PS\\Setup.ps1 first.") from exc

    config = load_config(config_path)
    stay_date = date.fromisoformat(request.stay_date)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        try:
            context = browser.new_context(
                locale="zh-TW", viewport={"width": 1280, "height": 900}
            )
            session_id = config.get("whotel_session", "")
            if session_id:
                context.add_cookies(
                    [
                        {
                            "name": "PHPSESSID",
                            "value": session_id,
                            "domain": "whotel.wuling-farm.com.tw",
                            "path": "/",
                            "secure": True,
                        }
                    ]
                )
            page = context.new_page()
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20_000)
            if not _is_logged_in(page) and not _browser_login(page, config):
                return {"status": "verify_login_failed", "submitted": False}

            with page.expect_response(
                lambda response: "func_select.php?func=6" in response.url,
                timeout=15_000,
            ) as response_info:
                page.evaluate("func_click(6)")
            response_text = response_info.value.text()
            try:
                order_html = json.loads(response_text)
            except json.JSONDecodeError:
                order_html = response_text
            plain_text = strip_tags(str(order_html))
            date_markers = (
                stay_date.isoformat(),
                stay_date.strftime("%Y/%m/%d"),
                f"民國{stay_date.year - 1911}年{stay_date.month}月{stay_date.day}日",
                f"{stay_date.year - 1911}/{stay_date.month:02d}/{stay_date.day:02d}",
            )
            date_match = any(marker in plain_text for marker in date_markers)
            people_match = people_marker(request.people) in plain_text
            order_numbers = re.findall(
                r"\bid=['\"]oid([^'\"]+)['\"]", str(order_html), re.IGNORECASE
            )
            verified = date_match and people_match
            return {
                "status": "verified_booked" if verified else "booking_not_found",
                "submitted": False,
                "date_match": date_match,
                "room_match": people_match,
                "order_numbers": order_numbers,
            }
        finally:
            browser.close()


def _serializable(value: Any) -> Any:
    if isinstance(value, Room):
        return asdict(value)
    if isinstance(value, list):
        return [_serializable(item) for item in value]
    if isinstance(value, dict):
        return {key: _serializable(item) for key, item in value.items()}
    return value


def save_result(result: dict[str, Any], log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"booking-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(
        json.dumps(_serializable(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def run(command: str, mode: str, config_path: Path, headed: bool) -> dict[str, Any]:
    request = parse_request(command)
    stay_date = date.fromisoformat(request.stay_date)
    search = search_inventory(stay_date, request.people)
    matching_rooms = search["matching_rooms"]
    selected = search["selected"]

    result: dict[str, Any] = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "request": asdict(request),
        "status": search["status"],
        "submitted": False,
        "matching_rooms": matching_rooms,
        "selected": selected,
    }

    # Mandatory short circuit: no login, OCR, cart, or follow-up when sold out.
    if not selected:
        result["status"] = "sold_out"
        result["message"] = "指定日期的目標房型全部售罄。"
        return result

    if mode == "rehearsal":
        result["status"] = "rehearsal_ready"
        result["message"] = "預演完成：找到可訂房型，未登入且未送出訂單。"
        return result

    live_result = execute_live_booking(request, selected, config_path, headed)
    result.update(live_result)
    if result["status"] == "booked":
        result["message"] = "訂房已送出並取得成功狀態。"
    elif result["status"] == "sold_out":
        result["message"] = "正式送出前庫存已售罄，未送出訂單。"
    else:
        result["message"] = f"訂房未確認成功：{result['status']}"
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wuling hotel booking command runner.")
    parser.add_argument("--request", required=True, help="Natural-language booking command.")
    parser.add_argument(
        "--mode", choices=("rehearsal", "live"), default="rehearsal"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "Secret" / "Secret.ini",
    )
    parser.add_argument("--log-dir", type=Path, default=PROJECT_ROOT / "LOG")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print the full result.")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only check the authenticated order list; never create an order.",
    )
    return parser.parse_args()


def format_status(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "sold_out":
        return "售罄"
    if status == "rehearsal_ready":
        selected = result.get("selected")
        room_name = selected.name if isinstance(selected, Room) else "可訂房型"
        return f"預演可訂：{room_name}（未送出訂單）"
    if status == "booked":
        order_number = result.get("order_number")
        return f"已下定，訂單編號：{order_number}" if order_number else "已下定"
    if status == "verified_booked":
        return "已下定，訂單查詢已確認"
    return f"訂房狀態：{status}"


def main() -> int:
    args = parse_args()
    try:
        if args.mode == "live" and not args.verify_only:
            raise BookingError(
                "一次性 Live 模式已停用；請執行 PS\\Start-WulingAssistant.ps1。"
            )
        if args.verify_only:
            request = parse_request(args.request)
            result = {
                "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "mode": "verify_only",
                "request": asdict(request),
                **verify_existing_booking(
                    request, args.config.resolve(), args.headed
                ),
            }
        else:
            result = run(args.request, args.mode, args.config.resolve(), args.headed)
        log_path = save_result(result, args.log_dir.resolve())
        result["log"] = str(log_path)
        if args.json:
            print(json.dumps(_serializable(result), ensure_ascii=False, indent=2))
        else:
            print(format_status(result))
        return 0 if result["status"] in {
            "booked",
            "verified_booked",
            "rehearsal_ready",
            "sold_out",
        } else 2
    except (BookingError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
