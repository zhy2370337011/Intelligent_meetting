"""Meeting-scoped sensitive-word masking for display, AI, and export consumers.

This module deliberately has no store or route dependency.  A transcript remains the durable
source text, while callers obtain a detached policy result for one target immediately before it
is rendered, sent to an AI provider, or written into an export artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Iterable, Mapping


POLICY_TARGETS = frozenset({"display", "ai", "export"})


@dataclass(frozen=True)
class PolicyResult:
    """The transformed text plus deterministic evidence of the rules that changed it."""

    text: str
    hits: tuple[dict[str, Any], ...]
    rule_version: str


def freeze_sensitive_rule_snapshot(rules: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Copy global rules into a stable, JSON-safe meeting snapshot.

    Global sensitive-rule administration is mutable.  Meetings instead persist this detached
    canonical list and version at creation time, so later edits cannot silently reprocess a
    historical display, AI, or export request with different policy content.
    """

    canonical_rules = [_canonical_rule(rule, index) for index, rule in enumerate(rules) if isinstance(rule, Mapping)]
    canonical_rules.sort(key=_rule_order_key)
    version = _rule_version(canonical_rules)
    return {"rules": canonical_rules, "ruleVersion": version}


def policy_snapshot_rules(snapshot: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return a copied rule list from persisted policy metadata without leaking mutable state."""

    if not isinstance(snapshot, Mapping):
        return []
    rules = snapshot.get("rules")
    if not isinstance(rules, list):
        return []
    return [dict(rule) for rule in rules if isinstance(rule, Mapping)]


def apply_sensitive_policy(text: str, rules: Iterable[Mapping[str, Any]], target: str) -> PolicyResult:
    """Apply only the enabled rules scoped to one explicit consumer target.

    Matches are collected from the original text, ordered by position and stable rule priority,
    and then rendered once.  This prevents a short rule from partially consuming an overlapping
    longer phrase, and prevents one replacement from creating a second accidental match.
    """

    if target not in POLICY_TARGETS:
        raise ValueError(f"Unsupported sensitive policy target: {target}")

    source_text = str(text or "")
    canonical_rules = [_canonical_rule(rule, index) for index, rule in enumerate(rules) if isinstance(rule, Mapping)]
    canonical_rules.sort(key=_rule_order_key)
    rule_version = _rule_version(canonical_rules)
    candidates: list[tuple[int, int, dict[str, Any], str]] = []

    for rule in canonical_rules:
        if not _rule_applies(rule, target):
            continue
        flags = 0 if rule["caseSensitive"] else re.IGNORECASE
        pattern = re.compile(re.escape(rule["word"]), flags)
        for match in pattern.finditer(source_text):
            candidates.append((match.start(), match.end(), rule, match.group(0)))

    # A leftmost-longest selection is deterministic and makes rule overlap auditable.  The
    # canonical id/index tie breaker keeps two same-length rules stable across process restarts.
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), _rule_order_key(item[2])))
    selected: list[tuple[int, int, dict[str, Any], str]] = []
    occupied_until = 0
    for candidate in candidates:
        if candidate[0] < occupied_until:
            continue
        selected.append(candidate)
        occupied_until = candidate[1]

    rendered: list[str] = []
    hits: list[dict[str, Any]] = []
    cursor = 0
    for start, end, rule, matched_text in selected:
        replacement = _replacement_text(rule, matched_text)
        rendered.append(source_text[cursor:start])
        rendered.append(replacement)
        hits.append(
            {
                "ruleId": rule["id"],
                "word": rule["word"],
                "target": target,
                "start": start,
                "end": end,
                "replacement": replacement,
            }
        )
        cursor = end
    rendered.append(source_text[cursor:])
    return PolicyResult(text="".join(rendered), hits=tuple(hits), rule_version=rule_version)


def _canonical_rule(rule: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Normalize legacy and current rule fields into the policy's narrow immutable contract."""

    word = str(rule.get("word") or "").strip()
    scope = str(rule.get("applyScope") or rule.get("scope") or "all").strip()
    replacement = str(rule.get("replacementMode") or rule.get("displayMode") or rule.get("replacement") or "stars").strip()
    legacy_identity = {
        "word": word,
        "scope": scope,
        "replacement": replacement.lower(),
        "replacementText": str(rule.get("replacementText") or rule.get("replaceWith") or ""),
        "enabled": bool(rule.get("enabled", True)),
        "caseSensitive": bool(rule.get("caseSensitive", False)),
        "language": str(rule.get("language") or "all").strip().lower(),
        "priority": int(rule.get("priority") or 0),
    }
    return {
        # Durable IDs remain authoritative.  For id-less legacy records, derive identity from
        # canonical rule content instead of input position so a semantically identical reordered
        # list creates the same frozen version and can be replayed deterministically.
        "id": str(rule.get("id") or f"legacy-rule-{sha256(json.dumps(legacy_identity, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()[:16]}"),
        "word": word,
        "scope": scope,
        "replacement": replacement.lower(),
        "replacementText": str(rule.get("replacementText") or rule.get("replaceWith") or ""),
        "enabled": bool(rule.get("enabled", True)),
        "caseSensitive": bool(rule.get("caseSensitive", False)),
        "language": str(rule.get("language") or "all").strip().lower(),
        "priority": int(rule.get("priority") or 0),
    }


def _rule_order_key(rule: Mapping[str, Any]) -> tuple[int, int, str]:
    """Provide one total order for snapshot serialization and overlap tie breaking."""

    return (-int(rule.get("priority") or 0), -len(str(rule.get("word") or "")), str(rule.get("id") or ""))


def _rule_version(canonical_rules: list[dict[str, Any]]) -> str:
    """Hash the effective frozen content rather than volatile storage timestamps."""

    payload = json.dumps(canonical_rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


def _rule_applies(rule: Mapping[str, Any], target: str) -> bool:
    """Check enablement, target scope, and the rule's declared word language before matching."""

    word = str(rule.get("word") or "")
    return bool(word and rule.get("enabled", True) and target in _scope_targets(str(rule.get("scope") or "")) and _language_matches(word, str(rule.get("language") or "all")))


def _scope_targets(scope: str) -> frozenset[str]:
    """Accept legacy Chinese labels while storing behavior in the three canonical target names."""

    normalized = scope.strip().lower().replace(" ", "")
    if not normalized or normalized in {"all", "全部", "所有", "全局"}:
        return POLICY_TARGETS
    targets: set[str] = set()
    if "display" in normalized or "展示" in scope or "显示" in scope:
        targets.add("display")
    if re.search(r"(^|[,/|、])ai($|[,/|、])", normalized) or "ai输入" in scope.lower() or "模型" in scope:
        targets.add("ai")
    if "export" in normalized or "导出" in scope:
        targets.add("export")
    return frozenset(targets)


def _language_matches(word: str, language: str) -> bool:
    """Prevent a rule labelled for one writing system from applying to another by accident."""

    normalized = language.lower().replace("_", "-")
    if normalized in {"", "all", "any", "*", "global"}:
        return True
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", word))
    has_latin = bool(re.search(r"[a-zA-Z]", word))
    if normalized.startswith("zh"):
        return has_cjk
    if normalized.startswith("en"):
        return has_latin and not has_cjk
    # Unknown product language labels remain compatible with existing matching behavior instead
    # of unexpectedly disabling a stored rule merely because a new locale is introduced.
    return True


def _replacement_text(rule: Mapping[str, Any], matched_text: str) -> str:
    """Render the established stars/hide/space modes or an explicit literal replacement value."""

    mode = str(rule.get("replacement") or "stars").lower()
    if mode in {"stars", "star", "mask", "asterisk"}:
        return "*" * len(matched_text)
    if mode in {"hide", "hidden", "remove"}:
        return ""
    if mode in {"space", "blank", "whitespace"}:
        return " " * len(matched_text)
    return str(rule.get("replacementText") or mode)
