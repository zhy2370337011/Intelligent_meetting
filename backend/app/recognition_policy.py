"""Meeting-scoped recognition vocabulary and final-text normalization rules.

The policy is assembled at the recognition boundary instead of letting each ASR transport query
configuration independently.  This keeps realtime contextual biasing and offline hotwords aligned
while still allowing their transcript records, requests, and lifecycle state to remain separate.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Mapping


_PROFILE_KEYS = {
    "library": ("enableKeywordLibraries", "keywordLibraries", "libraries"),
    "manual": ("enableManualKeywords", "manualKeywords", "manual"),
    "document": ("enableDocumentKeywords", "documentKeywords", "document"),
    "smart": ("enableSmartKeywords", "smartKeywords", "smart"),
    "replacement": ("enableReplacementRules", "replacementRules", "replacement"),
}

# A versioned, self-contained payload lets recognition keep historical semantics without reading
# mutable configuration rows after meeting creation. Increment this only for an incompatible shape.
_RECOGNITION_POLICY_SNAPSHOT_VERSION = 1

# 这些词描述的是产品内部能力或菜单，而不是会议中的人名、机构名、项目名。把它们作为
# DashScope corpus 会明显提高模型“照着提示词念一遍”的概率。此处仅影响实时上下文；导入
# 转写仍继续使用完整冻结词表，避免两个业务入口互相改变行为。
_REALTIME_GENERIC_CONTEXT_TERMS = frozenset(
    {
        "智能转写",
        "声纹注册",
        "强制对齐",
        "语篇规整",
        "会议纪要",
        "AI摘要",
        "摘要",
        "待办",
        "标记",
    }
)


@dataclass(frozen=True)
class EffectiveVocabulary:
    """Immutable, traceable recognition inputs for exactly one meeting.

    ``MappingProxyType`` prevents accidental mutation through a frozen dataclass's nested mapping.
    ``rule_ids`` is additive audit metadata: replacement rules expose their old public
    ``wrongWord -> correctWord`` mapping while final segments still identify the source rule.
    """

    words: tuple[str, ...]
    replacement_rules: Mapping[str, str]
    sources: frozenset[str]
    snapshot_hash: str
    rule_ids: Mapping[str, str]


@dataclass(frozen=True)
class NormalizedText:
    """Result of applying forced replacements only after ASR finalization."""

    raw_text: str
    text: str
    normalization_edits: tuple[dict[str, str], ...]


def _canonical_terms(values: Any) -> list[str]:
    """Trim text terms and retain first occurrence order for deterministic provider input."""

    if not isinstance(values, (list, tuple)):
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = str(value or "").strip()
        if term and term not in seen:
            terms.append(term)
            seen.add(term)
    return terms


def _profile_enabled(profile: Any, source: str) -> bool:
    """Return whether a frozen profile enables one source, defaulting legacy snapshots to enabled.

    Task 1 persisted an empty optimization profile for clients that predate these feature switches.
    An omitted switch must preserve that accepted behavior; only an explicit ``False`` disables a
    source.  Several key aliases are accepted so existing clients can migrate without a route fork.
    """

    if not isinstance(profile, dict):
        return True
    for key in _PROFILE_KEYS[source]:
        if key in profile:
            return bool(profile[key])
    return True


def _meeting_scope_matches(record: Mapping[str, Any], meeting_id: str) -> bool:
    """Accept global records and records explicitly scoped to this meeting, never another one."""

    scoped_ids = record.get("meetingIds")
    if isinstance(scoped_ids, (list, tuple)):
        return meeting_id in {str(value).strip() for value in scoped_ids}
    scoped_id = str(record.get("meetingId") or "").strip()
    if scoped_id:
        return scoped_id == meeting_id
    scope = str(record.get("scope") or "").strip()
    return not scope or scope.lower() in {"all", "global", "all_meetings", "全部", "全部会议"} or scope == meeting_id


def _language_matches(record: Mapping[str, Any], language: str) -> bool:
    """Filter manually maintained terms by the frozen meeting language when a record declares one."""

    record_language = str(record.get("language") or "").strip().lower()
    meeting_language = str(language or "").strip().lower()
    return not record_language or record_language in {"all", "auto"} or not meeting_language or record_language == meeting_language


def _attachment_document_ids(meeting: Mapping[str, Any], snapshot: Mapping[str, Any]) -> set[str]:
    """Read document IDs only from explicit meeting attachments or persisted extraction links."""

    values: list[Any] = []
    values.extend(snapshot.get("attachments") or [])
    values.extend(snapshot.get("documentKeywordDocumentIds") or [])
    values.extend(meeting.get("documentKeywordDocumentIds") or [])
    document_ids: set[str] = set()
    for value in values:
        if isinstance(value, str):
            candidate = value.strip()
        elif isinstance(value, dict):
            candidate = str(value.get("documentId") or value.get("id") or "").strip()
        else:
            candidate = ""
        if candidate:
            document_ids.add(candidate)
    return document_ids


def _confirmed_smart_terms(meeting: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[str]:
    """Return only terms a user confirmed for this meeting, never transient suggestions."""

    records = list(meeting.get("smartKeywordTerms") or []) + list(snapshot.get("confirmedSmartTerms") or [])
    terms: list[str] = []
    for record in records:
        if isinstance(record, str):
            # Strings are accepted only from the explicitly named confirmed snapshot field above.
            if record in (snapshot.get("confirmedSmartTerms") or []):
                terms.append(record)
        elif isinstance(record, dict) and record.get("confirmed") is True:
            terms.append(str(record.get("term") or record.get("word") or ""))
    return _canonical_terms(terms)


def _make_effective_vocabulary(
    words: Any,
    replacement_rules: Mapping[str, str],
    sources: Any,
    rule_ids: Mapping[str, str],
) -> EffectiveVocabulary:
    """Create the canonical immutable value shared by live capture and frozen snapshot reads."""

    effective_words = tuple(_canonical_terms(words))
    canonical_rules = {
        str(wrong_word).strip(): str(correct_word).strip()
        for wrong_word, correct_word in replacement_rules.items()
        if str(wrong_word).strip() and str(correct_word).strip()
    }
    canonical_rule_ids = {wrong_word: str(rule_ids.get(wrong_word) or "") for wrong_word in canonical_rules}
    canonical_sources = frozenset(str(source).strip() for source in sources if str(source).strip())
    canonical_payload = {
        "words": effective_words,
        "replacementRules": sorted(canonical_rules.items()),
        "ruleIds": sorted(canonical_rule_ids.items()),
        "sources": sorted(canonical_sources),
    }
    snapshot_hash = sha256(
        json.dumps(canonical_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return EffectiveVocabulary(
        words=effective_words,
        replacement_rules=MappingProxyType(canonical_rules),
        sources=canonical_sources,
        snapshot_hash=snapshot_hash,
        rule_ids=MappingProxyType(canonical_rule_ids),
    )


def _frozen_policy_from_snapshot(meeting: Mapping[str, Any]) -> EffectiveVocabulary | None:
    """Read a complete policy snapshot without consulting mutable store collections.

    The stored hash is provenance rather than a trust boundary: it is recomputed from canonical
    frozen content so a malformed historical row cannot claim an incorrect deterministic hash.
    """

    processing_config = meeting.get("processingConfig")
    snapshot = processing_config.get("recognitionPolicy") if isinstance(processing_config, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("version") != _RECOGNITION_POLICY_SNAPSHOT_VERSION:
        return None
    replacement_rules: dict[str, str] = {}
    rule_ids: dict[str, str] = {}
    for rule in snapshot.get("replacementRules") or []:
        if not isinstance(rule, dict):
            continue
        wrong_word = str(rule.get("from") or "").strip()
        correct_word = str(rule.get("to") or "").strip()
        if wrong_word and correct_word:
            replacement_rules[wrong_word] = correct_word
            rule_ids[wrong_word] = str(rule.get("ruleId") or "")
    return _make_effective_vocabulary(snapshot.get("words"), replacement_rules, snapshot.get("sources") or [], rule_ids)


def _build_live_effective_vocabulary(meeting: Mapping[str, Any], store: Any) -> EffectiveVocabulary:
    """Assemble creation-time inputs from enabled, in-scope records before freezing them.

    Library words come from Task 1's immutable meeting snapshot.  The remaining sources require
    both their frozen profile switch and a concrete meeting scope/attachment, ensuring an unrelated
    document, proposal, or administrator edit cannot leak into this meeting's ASR prompt.
    """

    snapshot = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
    profile = snapshot.get("optimizationProfile") or {}
    meeting_id = str(meeting.get("id") or "").strip()
    language = str(snapshot.get("language") or meeting.get("language") or "").strip()
    words: list[str] = []
    sources: set[str] = set()
    replacement_rules: dict[str, str] = {}
    rule_ids: dict[str, str] = {}

    if _profile_enabled(profile, "library"):
        library_words = _canonical_terms(snapshot.get("effectiveVocabulary"))
        if library_words:
            words.extend(library_words)
            sources.add("library")

    if _profile_enabled(profile, "manual"):
        manual_words = [
            word
            for _record_id, record in sorted(store.manual_keywords.items())
            if isinstance(record, dict)
            and record.get("enabled", True)
            and _meeting_scope_matches(record, meeting_id)
            and _language_matches(record, language)
            for word in record.get("words", [])
        ]
        if _canonical_terms(manual_words):
            words.extend(_canonical_terms(manual_words))
            sources.add("manual")

    if _profile_enabled(profile, "document"):
        attached_ids = _attachment_document_ids(meeting, snapshot)
        document_words: list[str] = []
        for document_id in sorted(attached_ids):
            document = store._get("optimization_documents", document_id)
            if not isinstance(document, dict) or document.get("status") != "completed":
                continue
            # 新版文档候选必须由用户确认；没有 confirmed 字段的历史记录保持兼容，避免升级后
            # 已经冻结到旧会议的词表突然失效。
            if document.get("confirmed") is False:
                continue
            if not _meeting_scope_matches(document, meeting_id):
                continue
            document_words.extend(document.get("keywords") or document.get("extractedTerms") or [])
        if _canonical_terms(document_words):
            words.extend(_canonical_terms(document_words))
            sources.add("document")

    if _profile_enabled(profile, "smart"):
        smart_words = _confirmed_smart_terms(meeting, snapshot)
        if smart_words:
            words.extend(smart_words)
            sources.add("smart")

    if _profile_enabled(profile, "replacement"):
        for rule_id, rule in sorted(store.replacement_rules.items()):
            if not isinstance(rule, dict) or not rule.get("enabled", True) or not _meeting_scope_matches(rule, meeting_id):
                continue
            wrong_word = str(rule.get("wrongWord") or "").strip()
            correct_word = str(rule.get("correctWord") or "").strip()
            if wrong_word and correct_word:
                replacement_rules[wrong_word] = correct_word
                rule_ids[wrong_word] = str(rule.get("id") or rule_id)
        if replacement_rules:
            sources.add("replacement")

    return _make_effective_vocabulary(words, replacement_rules, sources, rule_ids)


def build_effective_vocabulary(meeting: Mapping[str, Any], store: Any) -> EffectiveVocabulary:
    """Return the immutable policy for a meeting, preferring persisted frozen content.

    New meetings always receive ``recognitionPolicy`` during creation. The live branch supports
    old records only until their first processing request persists an honest legacy backfill.
    """

    frozen_policy = _frozen_policy_from_snapshot(meeting)
    return frozen_policy if frozen_policy is not None else _build_live_effective_vocabulary(meeting, store)


def freeze_recognition_policy_snapshot(
    meeting: dict[str, Any],
    store: Any,
    *,
    legacy_backfill: bool = False,
) -> EffectiveVocabulary:
    """Write the effective policy onto one meeting and return its immutable value.

    A legacy record has no recoverable creation-time source state, so first recognition is the
    honest freeze point. New meetings use this immediately after their ID is allocated so scoped
    records can be selected exactly once and later processing performs no policy-table scan.
    """

    existing = _frozen_policy_from_snapshot(meeting)
    if existing is not None:
        return existing
    policy = _build_live_effective_vocabulary(meeting, store)
    processing_config = deepcopy(meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {})
    processing_config["recognitionPolicy"] = {
        "version": _RECOGNITION_POLICY_SNAPSHOT_VERSION,
        "words": list(policy.words),
        "replacementRules": [
            {"from": wrong_word, "to": policy.replacement_rules[wrong_word], "ruleId": policy.rule_ids.get(wrong_word, "")}
            for wrong_word in sorted(policy.replacement_rules)
        ],
        "sources": sorted(policy.sources),
        "snapshotHash": policy.snapshot_hash,
        "frozenAt": "legacy_first_processing" if legacy_backfill else "meeting_creation",
    }
    meeting["processingConfig"] = processing_config
    return policy


def filter_realtime_context_items(
    meeting: Mapping[str, Any],
    values: Any,
) -> tuple[str, ...]:
    """Remove complete meeting-identity items from a realtime context source list.

    Meeting records can carry the same identity through three historical fields, and secondary
    sources such as policy words, participant names, known speakers, or browser context can repeat
    any one of them. Filtering each complete item at the final composition boundary prevents that
    metadata from reaching ASR without deleting a title substring from legitimate spoken context.
    """

    # Keep every non-empty historical identity independently instead of selecting the first one.
    # A migrated record may have a user-facing meetingName, a legacy title, and an imported fileName
    # with different values; all three are metadata and all three must remain outside realtime ASR.
    identity_values = {
        str(meeting.get(field_name) or "").strip()
        for field_name in ("meetingName", "title", "fileName")
        if str(meeting.get(field_name) or "").strip()
    }
    # Browser continuity context is one newline-delimited block of prior transcript segments, while
    # policy terms and known speakers arrive as individual list entries. Split every source entry
    # into logical lines before canonicalization so an identity line can be removed without dropping
    # adjacent normal transcript text from the same browser block.
    source_values = values if isinstance(values, (list, tuple)) else []
    logical_items: list[str] = []
    for value in source_values:
        logical_items.extend(str(value or "").splitlines())
    # ``_canonical_terms`` provides deterministic trimming/deduplication. Equality remains on each
    # complete logical item only: "recording.wav" is removed, while a useful sentence such as
    # "discussed recording.wav parsing" is preserved after harmless outer-whitespace trimming.
    return tuple(item for item in _canonical_terms(logical_items) if item not in identity_values)


def build_realtime_context(
    meeting: Mapping[str, Any],
    policy: EffectiveVocabulary,
    *,
    maximum_characters: int = 1200,
    include_title: bool = False,
) -> str:
    """Build a bounded recognition corpus, excluding meeting identity from realtime by default.

    ``include_title`` is an explicit compatibility switch for import/general recognition callers.
    Streaming callers must retain the default because feeding a meeting title or imported filename
    to realtime ASR can cause the provider to emit that metadata as if somebody had spoken it.
    """

    snapshot = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
    title = str(meeting.get("meetingName") or meeting.get("title") or meeting.get("fileName") or "").strip()
    participants = _canonical_terms(snapshot.get("participantNames") or meeting.get("participantNames"))
    # Keep the title entirely outside the candidate list unless a non-realtime caller opts in. This
    # is stronger than removing it after truncation and guarantees no prefix of a long title leaks
    # into the provider context while participant names and frozen policy words remain available.
    context_terms = [*participants, *policy.words]
    if include_title:
        context_terms.insert(0, title)
    # Realtime's safe default filters identities even when they reappear through participant names
    # or frozen policy words. Explicit legacy callers retain the old corpus exactly when opting in.
    items = (
        _canonical_terms(context_terms)
        if include_title
        else list(filter_realtime_context_items(meeting, context_terms))
    )
    if not include_title:
        # 只在实时入口剔除完整的泛功能词，绝不做子串删除。例如“智能转写项目组”可能是
        # 用户真实的组织名，不能因为包含“智能转写”四个字就被误伤。
        items = [item for item in items if item not in _REALTIME_GENERIC_CONTEXT_TERMS]
    return "\n".join(items)[: max(0, int(maximum_characters))]


def _normalize_realtime_echo_token(value: Any) -> str:
    """只移除上下文回声比较中无语义的空白和首尾标点。"""

    compact = re.sub(r"\s+", "", str(value or ""))
    return compact.strip("。！？!?；;，,、.．…：:\"'“”‘’（）()[]【】")


def is_realtime_context_echo(
    text: str,
    meeting: Mapping[str, Any],
    context_terms: Any = (),
) -> bool:
    """判断 final 是否只是会议元数据/上下文词的机械回显。

    规则故意保持保守：完整会议标题始终过滤；多个 corpus 词被分号、逗号或换行串起时
    过滤；单个泛功能词也过滤。单个人名或专有名词可能确实是用户说出的回答，因此不会
    因为它单独出现就丢弃；包含正常谓语/句子内容的文本同样保留。
    """

    normalized_text = _normalize_realtime_echo_token(text)
    if not normalized_text:
        return False
    identity_values = {
        _normalize_realtime_echo_token(meeting.get(field_name))
        for field_name in ("meetingName", "title", "fileName")
        if _normalize_realtime_echo_token(meeting.get(field_name))
    }
    if normalized_text in identity_values:
        return True

    processing_config = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
    snapshot = processing_config.get("recognitionPolicy") if isinstance(processing_config.get("recognitionPolicy"), dict) else {}
    candidates = [
        *(processing_config.get("participantNames") or []),
        *(processing_config.get("effectiveVocabulary") or []),
        *(snapshot.get("words") or []),
        *(context_terms if isinstance(context_terms, (list, tuple, set, frozenset)) else []),
        *_REALTIME_GENERIC_CONTEXT_TERMS,
    ]
    normalized_candidates = {
        normalized for item in candidates if (normalized := _normalize_realtime_echo_token(item))
    }
    # ASR 常把 corpus 以中文分号串回。按明确的列表分隔符切分，而不是按句号拆句，避免把
    # “王忠介绍了 KingbaseES。”这样的真实自然语言误判成提示词列表。
    tokens = [
        normalized
        for part in re.split(r"[；;、，,\n\r]+", str(text or ""))
        if (normalized := _normalize_realtime_echo_token(part))
    ]
    if not tokens or any(token not in normalized_candidates for token in tokens):
        return False
    if len(tokens) >= 2:
        return True
    return tokens[0] in {_normalize_realtime_echo_token(item) for item in _REALTIME_GENERIC_CONTEXT_TERMS}


def filter_realtime_ai_segments(meeting: Mapping[str, Any]) -> dict[str, Any]:
    """返回剔除历史上下文回声的 AI 输入副本，绝不修改持久化会议。

    早期版本已经写入数据库的错误片段仍需保留审计与人工核对价值，因此这里深拷贝后只
    清理模型输入。导入转写不是本问题的来源，也必须保持独立，故仅处理 realtime 会议。
    """

    safe_meeting = deepcopy(dict(meeting))
    processing_config = safe_meeting.get("processingConfig") if isinstance(safe_meeting.get("processingConfig"), dict) else {}
    if str(processing_config.get("transcriptionMode") or "").strip().lower() != "realtime":
        return safe_meeting
    safe_meeting["segments"] = [
        segment
        for segment in (safe_meeting.get("segments") or [])
        if not is_realtime_context_echo(str(segment.get("text") or ""), safe_meeting)
    ]
    return safe_meeting


def apply_final_replacements(
    raw_text: str,
    replacement_rules: Mapping[str, str],
    rule_ids: Mapping[str, str],
) -> NormalizedText:
    """Apply forced replacements to a final segment and retain an itemized audit trail.

    This function deliberately does not mutate interim text.  The caller invokes it only at the
    final persistence boundary, preserving the provider output in ``rawText`` and making each
    resulting edit attributable to the exact enabled replacement-rule record.
    """

    preserved_raw_text = str(raw_text or "")
    normalized_text = preserved_raw_text
    edits: list[dict[str, str]] = []
    # Longest terms first avoids a shorter rule rewriting part of a longer configured typo before
    # that precise rule has a chance to match. Equal lengths fall back to lexical order for a stable audit.
    for wrong_word in sorted(replacement_rules, key=lambda value: (-len(value), value)):
        correct_word = str(replacement_rules[wrong_word])
        rule_id = str(rule_ids.get(wrong_word) or "")
        occurrences = normalized_text.count(wrong_word)
        if not occurrences:
            continue
        normalized_text = normalized_text.replace(wrong_word, correct_word)
        edits.extend({"from": wrong_word, "to": correct_word, "ruleId": rule_id} for _ in range(occurrences))
    return NormalizedText(
        raw_text=preserved_raw_text,
        text=normalized_text,
        normalization_edits=tuple(edits),
    )


def extract_document_terms(parsed_text: str, *, maximum_terms: int = 30) -> list[str]:
    """Extract deterministic keyword candidates from parsed document text without demo values.

    This intentionally lightweight parser is an explicit non-model extraction path.  It keeps
    candidate ordering stable by frequency, then lexical form, and recognizes both technical
    Latin tokens and useful CJK terms.  Empty or non-text documents produce no candidates rather
    than fabricated success.
    """

    text = str(parsed_text or "").strip()
    if not text:
        return []
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9+.#_-]{2,}|[\u4e00-\u9fff]{2,12}", text)
    normalized = [candidate.strip("-_#.") for candidate in candidates if candidate.strip("-_#.")]
    counts = Counter(normalized)
    ordered = sorted(counts, key=lambda term: (-counts[term], term.casefold(), term))
    return ordered[: max(0, int(maximum_terms))]
