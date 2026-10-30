#!/usr/bin/env python3
"""Capture and recognize the Wuling hotel CAPTCHA without submitting a login."""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://whotel.wuling-farm.com.tw/whotel/"
ALNUM_ONLY = re.compile(r"[^0-9A-Za-z]")


class CaptchaError(RuntimeError):
    """Raised when capture or recognition cannot continue safely."""


def _png_bytes(image: Any) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def build_variants(image_bytes: bytes) -> list[tuple[str, bytes]]:
    """Return OCR variants; keep the browser screenshot unchanged on disk."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise CaptchaError("Pillow is missing. Run PS\\Setup.ps1 first.") from exc

    try:
        original = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        raise CaptchaError(f"The CAPTCHA image is not readable: {exc}") from exc

    width, height = original.size
    if width < 20 or height < 15:
        raise CaptchaError(f"CAPTCHA image is unexpectedly small: {width}x{height}")

    enlarged = original.resize((width * 4, height * 4), Image.Resampling.LANCZOS)
    gray = ImageOps.autocontrast(enlarged.convert("L"))
    inverted = ImageOps.invert(gray)
    threshold = gray.point(lambda pixel: 255 if pixel >= 180 else 0)

    return [
        ("original", image_bytes),
        ("upscale_4x", _png_bytes(enlarged)),
        ("gray_4x", _png_bytes(gray)),
        ("invert_4x", _png_bytes(inverted)),
        ("threshold_180_4x", _png_bytes(threshold)),
    ]


def recognize_captcha(
    image_bytes: bytes,
    *,
    expected_length: int | None = None,
    min_length: int = 4,
    max_length: int = 6,
    min_confidence: float = 0.85,
) -> dict[str, Any]:
    """Recognize with several renderings and reject uncertain disagreement."""
    try:
        import ddddocr
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise CaptchaError("ddddocr is missing. Run PS\\Setup.ps1 first.") from exc

    engine = ddddocr.DdddOcr(show_ad=False)
    candidates: list[dict[str, Any]] = []

    for variant_name, variant_bytes in build_variants(image_bytes):
        raw = engine.classification(variant_bytes, probability=True)
        text = ALNUM_ONLY.sub("", str(raw.get("text", "")))
        confidence = float(raw.get("confidence", 0.0))
        valid_length = (
            len(text) == expected_length
            if expected_length is not None
            else min_length <= len(text) <= max_length
        )
        candidates.append(
            {
                "variant": variant_name,
                "text": text,
                "confidence": round(confidence, 4),
                "valid_length": valid_length,
            }
        )

    votes: dict[str, list[float]] = defaultdict(list)
    for candidate in candidates:
        if candidate["valid_length"] and candidate["text"]:
            votes[candidate["text"]].append(float(candidate["confidence"]))

    if not votes:
        length_description = (
            f"exactly {expected_length}"
            if expected_length is not None
            else f"between {min_length} and {max_length}"
        )
        return {
            "accepted": False,
            "text": "",
            "confidence": 0.0,
            "agreement": 0,
            "reason": f"No OCR result contained {length_description} characters.",
            "candidates": candidates,
        }

    best_text, best_scores = max(
        votes.items(),
        key=lambda item: (len(item[1]), sum(item[1]), max(item[1])),
    )
    mean_confidence = sum(best_scores) / len(best_scores)
    agreement = len(best_scores)
    accepted = agreement >= 2 and mean_confidence >= min_confidence

    reason = (
        "Multiple OCR variants agree."
        if accepted
        else "OCR confidence or agreement is too low; enter the CAPTCHA manually."
    )
    return {
        "accepted": accepted,
        "text": best_text,
        "confidence": round(mean_confidence, 4),
        "agreement": agreement,
        "reason": reason,
        "candidates": candidates,
    }


def _click_guest_login(page: Any) -> None:
    locator = page.get_by_text("遊客登入", exact=True)
    if locator.count() == 0:
        return
    for index in range(locator.count() - 1, -1, -1):
        candidate = locator.nth(index)
        if candidate.is_visible():
            candidate.click()
            page.wait_for_timeout(700)
            return


def _find_captcha_locator(page: Any, selector: str | None, timeout_ms: int) -> Any:
    if selector:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=timeout_ms)
        return locator

    deadline_steps = max(1, timeout_ms // 250)
    best: tuple[int, Any] | None = None
    for _ in range(deadline_steps):
        images = page.locator("img")
        for index in range(images.count()):
            image = images.nth(index)
            if not image.is_visible():
                continue
            box = image.bounding_box()
            if not box:
                continue
            width = float(box["width"])
            height = float(box["height"])
            if width < 40 or height < 20 or width > 400 or height > 180:
                continue

            attributes = " ".join(
                filter(
                    None,
                    [
                        image.get_attribute("src"),
                        image.get_attribute("id"),
                        image.get_attribute("class"),
                        image.get_attribute("alt"),
                        image.get_attribute("title"),
                    ],
                )
            ).lower()
            nearby_text = image.evaluate(
                "el => (el.parentElement && el.parentElement.innerText || '').slice(0, 300)"
            )

            score = 0
            if any(word in attributes for word in ("captcha", "verify", "check", "rand")):
                score += 20
            if any(word in nearby_text for word in ("辨識碼", "驗證碼")):
                score += 12
            if 70 <= width <= 250 and 25 <= height <= 100:
                score += 6
            if 1.4 <= width / height <= 5.0:
                score += 3

            if best is None or score > best[0]:
                best = (score, image)

        if best and best[0] >= 9:
            return best[1]
        page.wait_for_timeout(250)

    if best and best[0] >= 6:
        return best[1]
    raise CaptchaError("Could not locate the CAPTCHA image. Use --selector to specify it.")


def capture_captcha(
    *,
    url: str,
    output_path: Path,
    selector: str | None,
    headed: bool,
    channel: str | None,
    timeout_ms: int,
) -> bytes:
    """Open the guest login and capture the rendered CAPTCHA element."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise CaptchaError("Playwright is missing. Run PS\\Setup.ps1 first.") from exc

    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {"headless": not headed}
        if channel:
            launch_options["channel"] = channel
        try:
            browser = playwright.chromium.launch(**launch_options)
        except Exception as exc:
            raise CaptchaError(
                "Chromium could not start. Run PS\\Setup.ps1 to install the matching browser."
            ) from exc

        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                device_scale_factor=1,
                locale="zh-TW",
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                _click_guest_login(page)
                captcha = _find_captcha_locator(page, selector, timeout_ms)
                image_bytes = captcha.screenshot(type="png")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(image_bytes)
                return image_bytes
            except CaptchaError:
                raise
            except Exception as exc:
                raise CaptchaError(f"Could not capture the CAPTCHA: {exc}") from exc
        finally:
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture and safely recognize the Wuling hotel CAPTCHA."
    )
    parser.add_argument("--input", type=Path, help="Recognize an existing image only.")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "IMAGE" / "whotel-captcha-latest.png",
        help="Path for the captured image.",
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--selector", help="Override CAPTCHA CSS selector.")
    parser.add_argument("--headed", action="store_true", help="Show the Chromium window.")
    parser.add_argument(
        "--channel",
        help="Optional browser channel such as chrome; bundled Chromium is the default.",
    )
    parser.add_argument("--timeout-ms", type=int, default=20_000)
    parser.add_argument(
        "--expected-length",
        type=int,
        help="Require one exact length instead of the default accepted range.",
    )
    parser.add_argument("--min-length", type=int, default=4)
    parser.add_argument("--max-length", type=int, default=6)
    parser.add_argument("--min-confidence", type=float, default=0.85)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.input:
            image_path = args.input.resolve()
            image_bytes = image_path.read_bytes()
        else:
            image_path = args.output.resolve()
            image_bytes = capture_captcha(
                url=args.url,
                output_path=image_path,
                selector=args.selector,
                headed=args.headed,
                channel=args.channel,
                timeout_ms=args.timeout_ms,
            )

        result = recognize_captcha(
            image_bytes,
            expected_length=args.expected_length,
            min_length=args.min_length,
            max_length=args.max_length,
            min_confidence=args.min_confidence,
        )
        result["image"] = str(image_path)

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"OCR: {result['text'] or '<none>'}")
            print(
                f"Accepted: {result['accepted']} "
                f"(confidence={result['confidence']}, agreement={result['agreement']})"
            )
            print(result["reason"])
            print(f"Image: {image_path}")
        return 0 if result["accepted"] else 2
    except (CaptchaError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
