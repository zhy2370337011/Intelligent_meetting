"""Normalize product language labels before they reach an ASR provider."""

from __future__ import annotations

import re


def normalize_asr_language(value: object, default: str = "zh") -> str:
    """Return the provider language code for UI labels, locale codes, and legacy records.

    Product forms historically persisted labels such as ``中文普通话``. DashScope expects a
    provider code such as ``zh`` and rejects the label with HTTP 400. Normalization belongs at the
    provider boundary as well as in the form, because old meeting snapshots and non-browser API
    clients can still contain those labels after the UI has been corrected.

    ``auto`` deliberately means language detection: synchronous Qwen3-ASR omits its language
    option for this value, while the realtime bridge can pass the documented auto mode. Unknown
    human-readable labels also fall back to auto instead of repeatedly sending an invalid enum;
    compact ISO-style codes are preserved so supported languages beyond the current UI remain
    available to API clients.
    """

    raw = str(value or "").strip()
    if not raw:
        return default

    key = raw.casefold().replace("_", "-")
    aliases = {
        "中文普通话": "zh",
        "普通话": "zh",
        "中文": "zh",
        "mandarin": "zh",
        "英文": "en",
        "英语": "en",
        "english": "en",
        "中英混合": "auto",
        "中文英文混合": "auto",
        "自动检测": "auto",
        "mixed": "auto",
        "auto": "auto",
    }
    if key in aliases:
        return aliases[key]
    if key.startswith("zh-"):
        return "zh"
    if key.startswith("en-"):
        return "en"
    # Preserve compact provider/ISO codes (for example yue, ja, ko) without accepting arbitrary
    # display sentences that the provider will reject as an invalid enum value.
    if re.fullmatch(r"[a-z]{2,3}", key):
        return key
    return "auto"
