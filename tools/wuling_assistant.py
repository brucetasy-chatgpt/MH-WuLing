#!/usr/bin/env python3
"""Persistent Wuling browser worker with a local authenticated job API."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import sys
import threading
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import wuling_booking as booking


PROFILE = booking.PROJECT_ROOT / ".wuling-browser-profile"
GUARDS = booking.PROJECT_ROOT / "Secret" / "booking-guards"
LOG_DIR = booking.PROJECT_ROOT / "LOG"
SERVICE_DIR = booking.PROJECT_ROOT / ".wuling-assistant"
DEFAULT_PORT = 18765


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(_json_value(value), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class JobStore:
    """Thread-safe, crash-visible storage for assistant jobs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.jobs_dir = root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        if not re.fullmatch(r"JOB-[0-9A-Z-]+", job_id):
            raise ValueError("工作編號格式錯誤。")
        return self.jobs_dir / f"{job_id}.json"

    def create(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind not in {"booking", "status"}:
            raise ValueError("不支援的工作類型。")
        with self._lock:
            job_id = (
                datetime.now().strftime("JOB-%Y%m%d-%H%M%S-")
                + uuid.uuid4().hex[:8].upper()
            )
            job = {
                "id": job_id,
                "kind": kind,
                "payload": _json_value(payload),
                "state": "queued",
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
            }
            _atomic_json_write(self._path(job_id), job)
            return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            path = self._path(job_id)
            if not path.exists():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.update(_json_value(changes))
            _atomic_json_write(self._path(job_id), job)
            return job

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            jobs: list[dict[str, Any]] = []
            for path in self.jobs_dir.glob("JOB-*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    jobs.append(value)
            jobs.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
            return jobs[:limit]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.get("state") != "queued":
                raise RuntimeError("只有尚未執行的工作可以取消。")
            return self.update(
                job_id, state="cancelled", finished_at=_now(),
                result={"status": "cancelled"},
            )

    def recover(self) -> tuple[list[dict[str, Any]], bool]:
        """Recover queued work; never retry an interrupted live booking."""
        recovered: list[dict[str, Any]] = []
        live_interrupted = False
        for job in reversed(self.list(limit=10_000)):
            state = job.get("state")
            if state == "running":
                payload = job.get("payload") or {}
                if job.get("kind") == "booking" and payload.get("mode") == "live":
                    job = self.update(
                        job["id"], state="interrupted", finished_at=_now(),
                        result={"status": "submit_unknown"},
                        error="程式在正式訂房執行期間中斷；未自動重試。",
                    )
                    live_interrupted = True
                else:
                    job = self.update(
                        job["id"], state="queued", started_at=None,
                        error="前次執行中斷，已安全重新排隊。",
                    )
            if job.get("state") == "queued":
                recovered.append(job)
        return recovered, live_interrupted


class PendingJobs:
    """FIFO queue that can still service login checks while bookings are paused."""

    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []
        self._condition = threading.Condition()

    def put(self, job_id: str, kind: str) -> None:
        with self._condition:
            self._items.append((job_id, kind))
            self._condition.notify_all()

    def get(self, allow_booking: bool, timeout: float = 0.25) -> tuple[str, str] | None:
        with self._condition:
            if not self._items:
                self._condition.wait(timeout)
            if not self._items:
                return None
            if allow_booking:
                return self._items.pop(0)
            for index, item in enumerate(self._items):
                if item[1] == "status":
                    return self._items.pop(index)
            self._condition.wait(timeout)
            return None

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()


class ServiceState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.current_job_id: str | None = None
        self.pause_reason: str | None = None
        self.login_status = "unknown"
        self.login_verified_at: str | None = None
        self.stop_requested = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "stopping" if self.stop_requested else "running",
                "current_job_id": self.current_job_id,
                "pause_reason": self.pause_reason,
                "login_status": self.login_status,
                "login_verified_at": self.login_verified_at,
                "pid": os.getpid(),
            }

    def set(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self, key, value)


class AssistantHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        store: JobStore,
        pending: PendingJobs,
        state: ServiceState,
        stop_event: threading.Event,
    ) -> None:
        self.token = token
        self.store = store
        self.pending = pending
        self.state = state
        self.stop_event = stop_event
        super().__init__(address, AssistantRequestHandler)


class AssistantRequestHandler(BaseHTTPRequestHandler):
    server: AssistantHttpServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, value: Any) -> None:
        body = json.dumps(_json_value(value), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Wuling-Token", "")
        return bool(supplied) and secrets.compare_digest(supplied, self.server.token)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send(HTTPStatus.UNAUTHORIZED, {"error": "未授權。"})
        return False

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("要求長度格式錯誤。") from exc
        if length < 0 or length > 16_384:
            raise ValueError("要求內容過大。")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(value, dict):
            raise ValueError("JSON 必須是物件。")
        return value

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/v1/health":
                self._send(HTTPStatus.OK, self.server.state.snapshot())
                return
            if path == "/v1/jobs":
                self._send(HTTPStatus.OK, {"jobs": self.server.store.list()})
                return
            match = re.fullmatch(r"/v1/jobs/(JOB-[0-9A-Z-]+)", path)
            if match:
                job = self.server.store.get(match.group(1))
                if job is None:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "找不到工作。"})
                else:
                    self._send(HTTPStatus.OK, job)
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "找不到端點。"})
        except Exception:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "服務讀取失敗。"})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/v1/jobs":
                data = self._read_json()
                kind = str(data.get("kind", ""))
                payload: dict[str, Any] = {}
                if kind == "booking":
                    mode = str(data.get("mode", "")).lower()
                    people = data.get("people")
                    day = str(data.get("date", ""))
                    if mode not in {"rehearsal", "live"}:
                        raise ValueError("Mode 必須是 Rehearsal 或 Live。")
                    if isinstance(people, bool) or not isinstance(people, int) or not 1 <= people <= 10:
                        raise ValueError("People 必須介於 1 到 10。")
                    try:
                        stay = datetime.strptime(day, "%Y%m%d").date()
                    except ValueError as exc:
                        raise ValueError("Date 必須是有效的 YYYYMMDD。") from exc
                    if stay < date.today():
                        raise ValueError("Date 不可早於今天。")
                    payload = {"mode": mode, "people": people, "date": day}
                elif kind != "status":
                    raise ValueError("Kind 必須是 booking 或 status。")
                job = self.server.store.create(kind, payload)
                self.server.pending.put(job["id"], kind)
                self._send(HTTPStatus.ACCEPTED, job)
                return
            match = re.fullmatch(r"/v1/jobs/(JOB-[0-9A-Z-]+)/cancel", path)
            if match:
                job = self.server.store.cancel(match.group(1))
                self._send(HTTPStatus.OK, job)
                return
            if path == "/v1/resume":
                data = self._read_json()
                pause_reason = self.server.state.snapshot()["pause_reason"]
                if pause_reason == "submission_unknown":
                    if data.get("acknowledge_unknown_submission") is not True:
                        raise ValueError("必須明確確認已人工核對不明送單結果。")
                else:
                    raise ValueError("目前沒有可由人工確認解除的暫停狀態。")
                self.server.state.set(pause_reason=None)
                self.server.pending.wake()
                self._send(HTTPStatus.OK, self.server.state.snapshot())
                return
            if path == "/v1/stop":
                self.server.state.set(stop_requested=True)
                self.server.stop_event.set()
                self.server.pending.wake()
                self._send(HTTPStatus.ACCEPTED, {"status": "stopping"})
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "找不到端點。"})
        except KeyError:
            self._send(HTTPStatus.NOT_FOUND, {"error": "找不到工作。"})
        except (ValueError, RuntimeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "服務寫入失敗。"})


class AuthError(booking.BookingError):
    """Authentication could not be verified with the server."""


class _LoginNameParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.found = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.depth:
            self.depth += 1
        elif dict(attrs).get("id") == "wlogin_name":
            self.found = True
            self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(data)


def _same_origin(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == "whotel.wuling-farm.com.tw"


def _verify_login_once(page: Any) -> None:
    home = page.request.get(booking.BASE_URL, timeout=15_000)
    if not home.ok or not _same_origin(home.url):
        raise AuthError("無法驗證網站登入回應。")
    parser = _LoginNameParser()
    parser.feed(home.text())
    name = " ".join(parser.parts).strip()
    if not parser.found or not name or re.search(r"未登入|遊客登入|登入", name):
        raise AuthError("網站未確認登入，請在瀏覽器中登入後輸入「狀態」。")

    booking.wait_site_action_delay(page)
    response = page.request.get(
        urljoin(booking.BASE_URL, "func_select.php?func=6"), timeout=15_000
    )
    if not response.ok or not _same_origin(response.url):
        raise AuthError("登入檢查請求失敗，暫停操作。")
    content = response.json()
    if not isinstance(content, str):
        raise AuthError("登入檢查回應格式異常，暫停操作。")
    plain = html.unescape(booking.strip_tags(content))
    if re.search(r"未登入|請先登入|登入後", plain):
        raise AuthError("登入已失效，請重新登入。")
    if "訂單" not in plain and not re.search(r"\bid\s*=\s*['\"]oid", content, re.I):
        raise AuthError("網站回應不足以確認登入，暫停操作。")


def verify_login(page: Any) -> None:
    """Check fresh authenticated responses; retry one transient transport failure."""
    for attempt in range(2):
        try:
            booking.wait_site_action_delay(page)
            _verify_login_once(page)
            booking.wait_site_action_delay(page)
            return
        except AuthError:
            raise
        except Exception as exc:
            if attempt == 0:
                booking.wait_site_action_delay(page)
                continue
            raise AuthError("無法向網站確認登入，暫停操作。") from exc


def existing_submission(request: booking.BookingRequest, log_dir: Path = LOG_DIR) -> bool:
    """Block retry when earlier project records show a submitted matching request."""
    for path in log_dir.glob("booking-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        prior = data.get("request", {})
        if (
            data.get("submitted") is True
            and prior.get("hotel") == request.hotel
            and prior.get("stay_date") == request.stay_date
            and prior.get("people") == request.people
        ):
            return True
    return False


def guard_path(request: booking.BookingRequest, guard_dir: Path = GUARDS) -> Path:
    return guard_dir / f"{request.stay_date}-{request.people}.json"


def checkout_matches(page: Any, request: booking.BookingRequest, selected: booking.Room) -> bool:
    """Refuse to submit unless checkout identifies one room and the target date."""
    area = page.locator("#content_area")
    if area.count() != 1:
        return False
    content = area.inner_text()
    stay = date.fromisoformat(request.stay_date)
    dates = (
        stay.isoformat(),
        stay.strftime("%Y/%m/%d"),
        f"{stay.year}年{stay.month}月{stay.day}日",
        f"民國{stay.year - 1911}年{stay.month}月{stay.day}日",
        f"{stay.year - 1911}/{stay.month:02d}/{stay.day:02d}",
    )
    delete_buttons = area.locator("input[value='刪除[X]']")
    return (
        selected.name in content
        and any(marker in content for marker in dates)
        and delete_buttons.count() == 1
    )


def book(page: Any, request: booking.BookingRequest, rehearsal: bool = False) -> dict[str, Any]:
    """Use the already authenticated page; never automatically retry a submission."""
    verify_login(page)
    if not rehearsal and (guard_path(request).exists() or existing_submission(request)):
        return {"status": "previous_submission", "submitted": False}

    # Keep the persistent browser on its current page. Calendar navigation now
    # waits for exact month/date DOM updates and falls back home only when needed.
    booking._browser_open_target_date(page, date.fromisoformat(request.stay_date))
    area = page.locator("#show_room_area")
    area.wait_for(state="visible", timeout=10_000)
    area_html = area.inner_html()
    if booking.parse_roc_date(area_html) != date.fromisoformat(request.stay_date):
        raise booking.BookingError("房型日期無法確認，操作已停止。")
    rooms = booking.parse_room_cards(area_html)
    matching = [room for room in rooms if booking.people_marker(request.people) in room.name]
    if not matching:
        raise booking.BookingError("無法確認目標房型，操作已停止。")
    selected = next((room for room in matching if room.available), None)
    if selected is None:
        return {"status": "sold_out", "submitted": False}
    if rehearsal:
        return {"status": "rehearsal_ready", "submitted": False, "selected": selected}

    verify_login(page)
    with page.expect_response(
        lambda response: "room_order.php?roomtype=" in response.url,
        timeout=15_000,
    ):
        page.locator(f"#{selected.code}").click()
    booking.wait_site_action_delay(page)
    verify_login(page)
    checkout_link = page.locator("#search_order999999")
    if checkout_link.count() != 1:
        return {"status": "checkout_blocked", "submitted": False}
    with page.expect_response(
        lambda response: "room_order.php?roomtype=999999" in response.url,
        timeout=15_000,
    ):
        checkout_link.click()
    booking.wait_site_action_delay(page)
    checkout = page.locator("#agree_and_order")
    if checkout.count() != 1 or not checkout_matches(page, request, selected):
        return {"status": "checkout_unverified", "submitted": False, "selected": selected}

    # Do not silently accept terms or guess how to complete the checkout form.
    if checkout.is_disabled():
        return {"status": "checkout_blocked", "submitted": False, "selected": selected}
    verify_login(page)
    if not checkout_matches(page, request, selected):
        return {"status": "checkout_unverified", "submitted": False, "selected": selected}

    GUARDS.mkdir(parents=True, exist_ok=True)
    marker = guard_path(request)
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(
                {"request": booking.asdict(request), "state": "submission_started",
                 "created_at": datetime.now().astimezone().isoformat(timespec="seconds")},
                stream, ensure_ascii=False,
            )
    except FileExistsError:
        return {"status": "previous_submission", "submitted": False}

    try:
        with page.expect_response(
            lambda response: "func_select.php?func=5" in response.url,
            timeout=20_000,
        ) as response_info:
            checkout.click()
        response = response_info.value
        if not response.ok or not _same_origin(response.url):
            return {"status": "submit_unknown", "submitted": True, "selected": selected}
        try:
            result = response.json()
        except Exception:
            return {"status": "submit_unknown", "submitted": True, "selected": selected}
        return {**classify_submission_response(result), "selected": selected}
    except Exception:
        return {"status": "submit_unknown", "submitted": True, "selected": selected}


def classify_submission_response(result: Any) -> dict[str, Any]:
    """Classify a same-origin submission response without claiming payment success."""
    text = html.unescape(booking.strip_tags(result)) if isinstance(result, str) else ""
    order = re.search(r"(?:訂單編號|訂單號碼)\s*[:：]\s*([A-Za-z0-9-]+)", text)
    if "暫時保留" in text and ("信用卡付款" in text or "10分鐘內" in text):
        return {
            "status": "held_pending_payment",
            "task_success": True,
            "submitted": True,
            "order_number": order.group(1) if order else None,
            "payment_deadline_minutes": 10,
        }
    success = any(word in text for word in ("訂房成功", "預訂成功", "訂房完成"))
    return {
        "status": "booked_unverified" if success and order else "submit_unknown",
        "submitted": True,
        "order_number": order.group(1) if order else None,
    }


def _print_result(result: dict[str, Any]) -> None:
    status = result["status"]
    messages = {
        "sold_out": "售罄",
        "previous_submission": "已有送出或結果未明的紀錄，請先核對訂單，未重複下訂。",
        "checkout_blocked": "結帳頁尚未可送出，請檢查瀏覽器；未送出訂單。",
        "checkout_unverified": "無法核對結帳日期、房型或數量，未送出訂單；請檢查瀏覽器。",
        "held_pending_payment": "訂房任務成功：房間已暫時保留；請人工完成付款，助手將繼續下一筆工作。",
        "submit_unknown": "已嘗試送出，結果未確認；請先查詢訂單，勿重複下訂。",
        "booked_unverified": "網站回報成功，請至訂單查詢核對日期、房型及付款期限。",
    }
    if status == "rehearsal_ready":
        room = result["selected"]
        print(f"預演可訂：{room.name}，NT${room.price} 起，尚餘 {room.remaining} 間。")
    else:
        print(messages.get(status, f"狀態：{status}"))
    if result.get("order_number"):
        print(f"網站回傳的訂單編號：{result['order_number']}")


def _execute_job(
    page: Any,
    job: dict[str, Any],
    store: JobStore,
    state: ServiceState,
) -> None:
    job_id = job["id"]
    kind = job["kind"]
    state.set(current_job_id=job_id)
    store.update(job_id, state="running", started_at=_now(), error=None)
    print(f"{job_id} 開始執行。", flush=True)
    try:
        if kind == "status":
            try:
                verify_login(page)
            except AuthError as exc:
                pause_reason = state.snapshot()["pause_reason"] or "authentication"
                state.set(
                    login_status="login_required", pause_reason=pause_reason
                )
                result = {"status": "login_required", "message": str(exc)}
            else:
                snapshot = state.snapshot()
                changes: dict[str, Any] = {
                    "login_status": "authenticated",
                    "login_verified_at": _now(),
                }
                if snapshot["pause_reason"] == "authentication":
                    changes["pause_reason"] = None
                state.set(**changes)
                result = {"status": "authenticated", "message": "網站已確認登入。"}
            store.update(
                job_id, state="completed", finished_at=_now(), result=result,
            )
            print(result["message"], flush=True)
            return

        payload = job["payload"]
        rehearsal = payload["mode"] == "rehearsal"
        command = (
            f"預定武陵國民賓館 {payload['people']}人房，{payload['date']}"
        )
        request = booking.parse_request(command)
        result = book(page, request, rehearsal=rehearsal)
        record = {
            "checked_at": _now(),
            "job_id": job_id,
            "mode": "assistant_rehearsal" if rehearsal else "assistant_live",
            "request": booking.asdict(request),
            **result,
        }
        log_path = booking.save_result(record, LOG_DIR)
        result["log"] = str(log_path)
        store.update(
            job_id, state="completed", finished_at=_now(), result=result,
        )
        if result.get("status") == "submit_unknown":
            state.set(pause_reason="submission_unknown")
        else:
            state.set(login_status="authenticated", login_verified_at=_now())
        _print_result(result)
    except AuthError as exc:
        pause_reason = state.snapshot()["pause_reason"] or "authentication"
        state.set(login_status="login_required", pause_reason=pause_reason)
        result = {"status": "login_required", "message": str(exc)}
        store.update(
            job_id, state="completed", finished_at=_now(), result=result,
        )
        print(str(exc), flush=True)
    except booking.BookingError as exc:
        store.update(
            job_id, state="failed", finished_at=_now(),
            error=str(exc), result={"status": "error", "message": str(exc)},
        )
        print(f"{job_id} 操作停止：{exc}", flush=True)
    except Exception as exc:
        store.update(
            job_id, state="failed", finished_at=_now(),
            error=f"{type(exc).__name__}: {exc}",
            result={"status": "error", "message": "網站或瀏覽器操作失敗。"},
        )
        print(f"{job_id} 網站或瀏覽器操作失敗。", flush=True)
    finally:
        state.set(current_job_id=None)


def _prepare_next_booking_page(payment_page: Any) -> Any:
    """Keep the successful hold visible and prepare a background tab for more work."""
    next_page = payment_page.context.new_page()
    payment_page.bring_to_front()
    return next_page


def _write_runtime_file(
    runtime_file: Path, port: int, token: str, state: ServiceState
) -> None:
    _atomic_json_write(runtime_file, {
        "version": 1,
        "host": "127.0.0.1",
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "started_at": _now(),
        **state.snapshot(),
    })
    try:
        runtime_file.chmod(0o600)
    except OSError:
        pass


def _remove_own_runtime_file(runtime_file: Path, token: str) -> None:
    try:
        value = json.loads(runtime_file.read_text(encoding="utf-8"))
        if value.get("token") == token and value.get("pid") == os.getpid():
            runtime_file.unlink(missing_ok=True)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass


def run_assistant(
    profile: Path = PROFILE,
    service_dir: Path = SERVICE_DIR,
    port: int = DEFAULT_PORT,
) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 Playwright，請先執行 .\\PS\\Setup.ps1。", file=sys.stderr)
        return 1
    profile.mkdir(parents=True, exist_ok=True)
    service_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(service_dir)
    pending = PendingJobs()
    state = ServiceState()
    stop_event = threading.Event()
    recovered, live_interrupted = store.recover()
    if live_interrupted:
        state.set(pause_reason="submission_unknown")
    for job in recovered:
        pending.put(job["id"], job["kind"])
    token = secrets.token_urlsafe(32)
    try:
        server = AssistantHttpServer(
            ("127.0.0.1", port), token, store, pending, state, stop_event
        )
    except OSError as exc:
        print(f"無法啟動本機命令服務（連接埠 {port}）：{exc}", file=sys.stderr)
        return 1
    actual_port = int(server.server_address[1])
    runtime_file = service_dir / "runtime.json"
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="wuling-local-api",
        kwargs={"poll_interval": 0.1},
        daemon=True,
    )
    server_thread.start()
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=False, locale="zh-TW",
                viewport={"width": 1280, "height": 900},
            )
            # Publish the CLI endpoint only after Chromium owns the profile.
            # A second launch that loses the profile race must never overwrite the
            # runtime metadata of the assistant that is already running.
            _write_runtime_file(runtime_file, actual_port, token, state)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(booking.BASE_URL, wait_until="domcontentloaded", timeout=20_000)
                print("瀏覽器已啟動；請於視窗登入。", flush=True)
                print(
                    "命令介面：.\\PS\\Invoke-WulingAssistant.ps1 -Action Status",
                    flush=True,
                )
                try:
                    verify_login(page)
                    state.set(login_status="authenticated", login_verified_at=_now())
                    print("網站已確認登入。", flush=True)
                except AuthError:
                    try:
                        config = booking.load_config(
                            booking.PROJECT_ROOT / "Secret" / "Secret.ini"
                        )
                        if booking._browser_login(page, config):
                            verify_login(page)
                            state.set(
                                login_status="authenticated",
                                login_verified_at=_now(),
                            )
                            print("網站已確認登入。", flush=True)
                        else:
                            state.set(
                                login_status="login_required",
                                pause_reason=state.snapshot()["pause_reason"]
                                or "authentication",
                            )
                            print("自動登入未成功，請在瀏覽器中手動登入。", flush=True)
                    except Exception:
                        state.set(
                            login_status="login_required",
                            pause_reason=state.snapshot()["pause_reason"]
                            or "authentication",
                        )
                        print("請在瀏覽器中登入，再執行 -Action Status。", flush=True)

                while not stop_event.is_set():
                    allow_booking = state.snapshot()["pause_reason"] is None
                    item = pending.get(allow_booking=allow_booking)
                    if item is None:
                        continue
                    job_id, _kind = item
                    job = store.get(job_id)
                    if job is None or job.get("state") != "queued":
                        continue
                    _execute_job(page, job, store, state)
                    completed = store.get(job_id)
                    if (
                        completed is not None
                        and completed.get("result", {}).get("status")
                        == "held_pending_payment"
                    ):
                        payment_page = page
                        try:
                            page = _prepare_next_booking_page(payment_page)
                            print(
                                "已保留人工付款頁籤；助手將在背景頁籤繼續下一筆工作。",
                                flush=True,
                            )
                        except Exception:
                            page = payment_page
                            state.set(pause_reason="submission_unknown")
                            print(
                                "無法建立下一筆工作的背景頁籤；佇列已暫停，人工付款頁仍保留。",
                                flush=True,
                            )
                print("收到停止命令；正在安全關閉助手。", flush=True)
            finally:
                context.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            _remove_own_runtime_file(runtime_file, token)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="持續執行的武陵訂房助手")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--profile", type=Path, default=PROFILE)
    parser.add_argument("--service-dir", type=Path, default=SERVICE_DIR)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port 必須介於 0 到 65535。")
    try:
        return run_assistant(
            profile=args.profile.resolve(),
            service_dir=args.service_dir.resolve(),
            port=args.port,
        )
    except Exception as exc:
        print(f"無法啟動助手：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
