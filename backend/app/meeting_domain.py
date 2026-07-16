"""Domain rules for immutable meeting processing configuration and transcript revisions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


def _canonicalize_strings(values: Any) -> list[str]:
    """Trim string values and retain each nonblank value's first deterministic occurrence.

    Snapshot lists are persisted and later reused at an ASR provider boundary, so accepting
    whitespace-only entries or duplicate values would make a historical configuration ambiguous.
    The caller's sequence order is intentionally preserved: it is both deterministic and avoids
    silently changing a user-selected keyword priority.
    """

    if not isinstance(values, (list, tuple)):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if item and item not in seen:
            normalized.append(item)
            seen.add(item)
    return normalized


def _canonicalize_scalar(value: Any, default: str = "") -> str:
    """Normalize a form/config scalar without turning ``None`` into the literal text ``None``."""

    normalized = str(value or "").strip()
    return normalized or default


def build_processing_snapshot(request: Any, store: Any, *, mode: str) -> dict[str, Any]:
    """Freeze the processing choices that were effective when a meeting started.

    The returned object deliberately contains plain copied values instead of references to
    request models or store records. Configuration libraries and templates are mutable global
    settings, while a meeting must retain the exact values that informed its transcription.
    """

    library_ids = _canonicalize_strings(getattr(request, "keywordLibraryIds", None))
    libraries = store.keyword_libraries
    effective_words = _canonicalize_strings(
        [
            word
            for library_id in library_ids
            for word in libraries.get(library_id, {}).get("words", [])
        ]
    )
    template_id = _canonicalize_scalar(getattr(request, "templateId", ""))

    return {
        "transcriptionMode": mode,
        "language": _canonicalize_scalar(getattr(request, "language", "")),
        "audioSource": _canonicalize_scalar(getattr(request, "audioSource", "")),
        "enableDiarization": bool(getattr(request, "enableDiarization", False)),
        "participantNames": _canonicalize_strings(getattr(request, "participantNames", None)),
        # Group IDs are durable foreign-key-like snapshot values. Normalize the scalar here just
        # like list IDs so a form value padded with whitespace does not become a different group.
        "voiceprintGroupId": _canonicalize_scalar(getattr(request, "voiceprintGroupId", ""), "vg-all"),
        "optimizationProfile": deepcopy(getattr(request, "optimizationProfile", None) or {}),
        # Only confirmation supplied at creation can affect this immutable policy. Later smart
        # suggestions remain meeting metadata until an explicit future re-apply workflow exists.
        "confirmedSmartTerms": _canonicalize_strings(getattr(request, "confirmedSmartTerms", None)),
        "keywordLibraryIds": library_ids,
        "effectiveVocabulary": effective_words,
        "sensitiveRuleVersion": store.config_revision("sensitive_rules"),
        # Sensitive rules are deliberately absent from ASR inputs.  The old flat word list had no
        # display/AI/export scope, so gateway-level masking made stored transcript text irreversible
        # before Task 4 could apply the frozen target-specific policy.  Detailed rules are frozen
        # separately by ``sensitive_policy`` and are consumed only at display, AI, and export edges.
        "sensitiveWords": [],
        "templateId": template_id,
        "templateSnapshot": store.template_snapshot(template_id),
        "notes": _canonicalize_scalar(getattr(request, "notes", "")),
        "attachments": deepcopy(getattr(request, "attachments", None) or []),
        # 文档优化记录必须由用户在创建/导入时显式选择。冻结 ID 与词表来源可避免后来上传的
        # 无关文档悄悄改变一场已开始会议的 ASR 上下文。
        "documentKeywordDocumentIds": _canonicalize_strings(
            getattr(request, "documentKeywordDocumentIds", None)
        ),
    }


def get_meeting_asr_inputs(meeting: dict[str, Any]) -> dict[str, Any]:
    """Return detached, frozen ASR values from one meeting's processing snapshot.

    This is the sole reader for recognition-time configuration. Returning empty values for old
    records that predate the snapshot avoids a hidden fallback to mutable global dictionaries;
    newly created realtime and import meetings always contain the fields below.
    """

    snapshot = meeting.get("processingConfig") or {}
    return {
        "effectiveVocabulary": _canonicalize_strings(snapshot.get("effectiveVocabulary")),
        # Keep the public key for compatibility with adapters that still accept the argument, but
        # never return persisted legacy words to a production recognition call.  ASR source text
        # must reach Task 3 normalization unmasked; target policy belongs after durable storage.
        "sensitiveWords": [],
        "language": str(snapshot.get("language") or ""),
        "enableDiarization": snapshot.get("enableDiarization"),
    }


def bump_transcript_revision(
    meeting: dict[str, Any], *, reason: str, segment_ids: list[str]
) -> dict[str, Any]:
    """Record one durable transcript mutation and its precise source segments.

    A revision describes persisted final transcript content, not transient UI state. Callers
    therefore invoke this only after a final segment has been stored, or after an existing
    segment's text/speaker identity has genuinely changed. One operation may affect several
    import or rename segments but still creates one coherent revision for downstream artifacts.
    """

    meeting["transcriptRevision"] = int(meeting.get("transcriptRevision", 0)) + 1
    meeting["transcriptRevisionReason"] = reason
    # Source IDs are durable invalidation metadata. Canonicalizing here protects every revision
    # producer (realtime, import, edits, and speaker-wide rename) from recording blank or
    # duplicate entries when an upstream ASR provider omits or pads an identifier.
    meeting["transcriptRevisionSegmentIds"] = _canonicalize_strings(segment_ids)
    meeting["transcriptUpdatedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return meeting
