"""Regression coverage for localized product language values at ASR boundaries."""

from __future__ import annotations

import unittest

from app.asr_language import normalize_asr_language


class AsrLanguageTest(unittest.TestCase):
    """UI labels, locale variants, and mixed-language mode must remain provider-safe."""

    def test_product_labels_and_locales_map_to_provider_codes(self) -> None:
        # These are the exact values used by current/legacy language selectors. Testing them as a
        # table keeps future localized copy changes from silently becoming provider enum values.
        cases = {
            "中文普通话": "zh",
            "英文": "en",
            "中英混合": "auto",
            "zh-CN": "zh",
            "en-US": "en",
        }

        for product_value, expected in cases.items():
            with self.subTest(product_value=product_value):
                self.assertEqual(normalize_asr_language(product_value), expected)

    def test_unknown_human_label_uses_auto_detection_but_iso_code_is_preserved(self) -> None:
        # Human-readable labels are unsafe enums for DashScope, whereas compact language codes may
        # represent supported languages that are not yet exposed in this product's three-option UI.
        self.assertEqual(normalize_asr_language("其他语言"), "auto")
        self.assertEqual(normalize_asr_language("yue"), "yue")


if __name__ == "__main__":
    unittest.main()
