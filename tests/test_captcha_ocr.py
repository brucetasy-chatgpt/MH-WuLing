import importlib.util
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "tools" / "whotel_captcha.py"
SPEC = importlib.util.spec_from_file_location("whotel_captcha", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CaptchaOcrTests(unittest.TestCase):
    def test_known_captcha_is_recognized_by_consensus(self) -> None:
        image_bytes = (PROJECT_ROOT / "IMAGE" / "whotel-captcha.png").read_bytes()
        result = MODULE.recognize_captcha(image_bytes)

        self.assertTrue(result["accepted"])
        self.assertEqual("5tj5", result["text"])
        self.assertGreaterEqual(result["agreement"], 2)


if __name__ == "__main__":
    unittest.main()
