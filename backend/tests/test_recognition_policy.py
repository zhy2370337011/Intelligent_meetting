"""Contract tests for the meeting-scoped recognition policy.

These tests exercise the policy module directly so its source selection remains deterministic and
does not depend on a running ASR provider, the application singleton, or developer data.
"""

from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from app.recognition_policy import (
    apply_final_replacements,
    build_effective_vocabulary,
    build_realtime_context,
    filter_realtime_ai_segments,
    filter_realtime_context_items,
)
from app import main
from app.store import PersistentStore


class RecognitionPolicyTest(unittest.TestCase):
    """Verify every policy source and the final-text-only normalization boundary."""

    def setUp(self) -> None:
        """Create a private store because policy tests must never alter developer records."""

        self.temp_dir = TemporaryDirectory()
        self.store = PersistentStore(Path(self.temp_dir.name) / "recognition-policy.db", seed_defaults=False)
        # Route functions intentionally use the application singleton. Rebinding it for this test
        # keeps the import/finalization contract isolated without altering developer data.
        self.original_main_store = main.store
        self.original_asr_gateway = main.asr_gateway
        main.store = self.store
        self.store.create_config_item(
            "keyword_libraries",
            "kw",
            {"name": "meeting library", "words": ["National Committee", "KingbaseES"], "enabled": True},
        )
        # ``PersistentStore.create_meeting`` retains the legacy minutes surface, which requires one
        # template even though these policy tests never generate minutes.
        self.store.create_config_item("templates", "tpl", {"name": "policy template"})

    def tearDown(self) -> None:
        """Release the temporary SQLite file after each isolated policy scenario."""

        main.store = self.original_main_store
        main.asr_gateway = self.original_asr_gateway
        self.temp_dir.cleanup()

    def _meeting(self, profile: dict | None = None) -> dict:
        """Return one meeting with explicit source attachments and a frozen library snapshot."""

        library_id = next(iter(self.store.keyword_libraries))
        return {
            "id": "meeting-policy",
            "meetingName": "KingbaseES architecture review",
            "processingConfig": {
                "language": "en",
                "participantNames": ["Ada", "Grace"],
                "keywordLibraryIds": [library_id],
                # The library terms are frozen at meeting creation. A later global library edit must
                # not silently change recognition for this historical meeting.
                "effectiveVocabulary": ["National Committee", "KingbaseES"],
                "optimizationProfile": profile
                or {
                    "enableManualKeywords": True,
                    "enableDocumentKeywords": True,
                    "enableSmartKeywords": True,
                    "enableReplacementRules": True,
                },
                "attachments": [{"documentId": "doc-policy"}],
            },
            "smartKeywordTerms": [
                {"term": "Qwen3-ASR", "confirmed": True},
                {"term": "ignored proposal", "confirmed": False},
            ],
        }

    def test_legacy_recognition_policy_backfill_preserves_a_final_committed_during_snapshot_build(self) -> None:
        """The first ASR use of a legacy meeting must merge policy fields into the latest row."""

        meeting = main._create_meeting_with_frozen_recognition_policy(
            main.MeetingCreateRequest(meetingName="legacy recognition backfill", language="en"),
            mode="realtime",
        )
        processing_config = dict(meeting.get("processingConfig") or {})
        processing_config.pop("recognitionPolicy", None)
        meeting["processingConfig"] = processing_config
        self.store._save("meetings", meeting)
        detached_legacy_meeting = self.store.get_or_create_meeting(meeting["id"])
        real_freeze = main.freeze_recognition_policy_snapshot

        def persist_final_during_policy_build(target, policy_store, *, legacy_backfill=False):
            # Commit the transcript after the route's detached read.  A full-row compatibility
            # save would erase this segment; the atomic processing-config merge must retain it.
            self.store.add_realtime_segment(
                meeting["id"],
                {"id": "recognition-race-final", "text": "Policy snapshot race final.", "isFinal": True},
            )
            return real_freeze(target, policy_store, legacy_backfill=legacy_backfill)

        with patch.object(main, "freeze_recognition_policy_snapshot", side_effect=persist_final_during_policy_build):
            persisted, policy = main._recognition_policy_for_processing(detached_legacy_meeting)

        self.assertIn("recognition-race-final", [segment["id"] for segment in persisted["segments"]])
        self.assertTrue(policy.snapshot_hash)
        self.assertIsInstance(persisted["processingConfig"].get("recognitionPolicy"), dict)

    def test_policy_combines_enabled_meeting_scoped_sources(self) -> None:
        """All enabled sources contribute only their in-scope, explicit meeting records."""

        self.store.create_config_item(
            "manual_keywords",
            "mk",
            {"language": "en", "scope": "meeting-policy", "words": ["CAM++"], "enabled": True},
        )
        self.store.create_config_item(
            "manual_keywords",
            "mk",
            {"language": "zh", "scope": "meeting-policy", "words": ["out of language"], "enabled": True},
        )
        self.store.create_config_item(
            "optimization_documents",
            "doc",
            {
                "id": "doc-policy",
                "meetingIds": ["meeting-policy"],
                "status": "completed",
                "keywords": ["Policy Graph", "KingbaseES"],
            },
        )
        self.store.create_config_item(
            "replacement_rules",
            "rr",
            {"wrongWord": "voice print", "correctWord": "voiceprint", "enabled": True, "scope": "meeting-policy"},
        )

        policy = build_effective_vocabulary(self._meeting(), self.store)

        self.assertEqual(
            policy.words,
            ("National Committee", "KingbaseES", "CAM++", "Policy Graph", "Qwen3-ASR"),
        )
        self.assertEqual(policy.replacement_rules, {"voice print": "voiceprint"})
        self.assertEqual(policy.sources, frozenset({"library", "manual", "document", "smart", "replacement"}))

    def test_policy_excludes_disabled_or_out_of_scope_sources(self) -> None:
        """Source toggles, language, and meeting attachments are hard policy boundaries."""

        self.store.create_config_item(
            "manual_keywords",
            "mk",
            {"language": "en", "scope": "other-meeting", "words": ["foreign manual"], "enabled": True},
        )
        self.store.create_config_item(
            "optimization_documents",
            "doc",
            {"id": "doc-policy", "meetingIds": ["other-meeting"], "status": "completed", "keywords": ["foreign document"]},
        )
        self.store.create_config_item(
            "replacement_rules",
            "rr",
            {"wrongWord": "wrong", "correctWord": "right", "enabled": False, "scope": "meeting-policy"},
        )

        policy = build_effective_vocabulary(
            self._meeting(
                {
                    "enableManualKeywords": False,
                    "enableDocumentKeywords": True,
                    "enableSmartKeywords": False,
                    "enableReplacementRules": True,
                }
            ),
            self.store,
        )

        self.assertEqual(policy.words, ("National Committee", "KingbaseES"))
        self.assertEqual(policy.replacement_rules, {})
        self.assertEqual(policy.sources, frozenset({"library"}))

    def test_hash_is_deterministic_and_changes_with_effective_content(self) -> None:
        """Equivalent policy snapshots hash identically, while changed vocabulary is observable."""

        first = build_effective_vocabulary(self._meeting(), self.store)
        second = build_effective_vocabulary(self._meeting(), self.store)
        changed_meeting = self._meeting()
        changed_meeting["smartKeywordTerms"] = [{"term": "different confirmed term", "confirmed": True}]
        changed = build_effective_vocabulary(changed_meeting, self.store)

        self.assertEqual(first.snapshot_hash, second.snapshot_hash)
        self.assertNotEqual(first.snapshot_hash, changed.snapshot_hash)

    def test_creation_snapshot_freezes_manual_document_smart_and_replacement_sources(self) -> None:
        """Later source mutation must not change this meeting's policy or final normalization.

        This scenario deliberately changes each source through the same mutable records used by
        administrators after a meeting has been created.  The policy must still be reconstructed
        only from the creation-time snapshot, including the replacement rule needed for the
        final-only audit contract.
        """

        manual = self.store.create_config_item(
            "manual_keywords",
            "mk",
            {"language": "en", "scope": "global", "words": ["frozen manual"], "enabled": True},
        )
        document = self.store.create_config_item(
            "optimization_documents",
            "doc",
            {"id": "doc-frozen", "status": "completed", "keywords": ["frozen document"], "scope": "global"},
        )
        replacement = self.store.create_config_item(
            "replacement_rules",
            "rr",
            {"wrongWord": "old phrase", "correctWord": "frozen replacement", "enabled": True, "scope": "global"},
        )
        library_id = next(iter(self.store.keyword_libraries))
        meeting = main.create_meeting(
            main.MeetingCreateRequest(
                meetingName="frozen recognition sources",
                language="en",
                keywordLibraryIds=[library_id],
                attachments=[{"documentId": document["id"]}],
                confirmedSmartTerms=["frozen smart"],
                optimizationProfile={
                    "enableManualKeywords": True,
                    "enableDocumentKeywords": True,
                    "enableSmartKeywords": True,
                    "enableReplacementRules": True,
                },
            )
        )
        original_policy = build_effective_vocabulary(meeting, self.store)

        self.store.update_config_item("manual_keywords", manual["id"], {"words": ["mutated manual"], "enabled": False})
        self.store.update_config_item("optimization_documents", document["id"], {"keywords": ["mutated document"], "status": "failed"})
        self.store.delete_config_item("replacement_rules", replacement["id"])
        persisted = self.store.get_or_create_meeting(meeting["id"])
        persisted["smartKeywordTerms"] = [{"term": "mutated smart", "confirmed": True}]
        self.store._save("meetings", persisted)

        frozen_policy = build_effective_vocabulary(self.store.get_or_create_meeting(meeting["id"]), self.store)
        normalized = apply_final_replacements("old phrase", frozen_policy.replacement_rules, frozen_policy.rule_ids)

        self.assertEqual(frozen_policy, original_policy)
        self.assertEqual(
            frozen_policy.words,
            ("National Committee", "KingbaseES", "frozen manual", "frozen document", "frozen smart"),
        )
        self.assertEqual(normalized.raw_text, "old phrase")
        self.assertEqual(normalized.text, "frozen replacement")
        self.assertEqual(normalized.normalization_edits[0]["ruleId"], replacement["id"])

    def test_realtime_context_excludes_title_but_keeps_participants_and_policy_words(self) -> None:
        """Realtime biasing must not feed the meeting title back to the streaming recognizer."""

        meeting = self._meeting()
        meeting["meetingName"] = "快速会议 7-14 16:00"
        policy = build_effective_vocabulary(meeting, self.store)

        # The limit remains part of the public contract, while the content assertions prove that
        # removing the title does not accidentally remove useful participant and vocabulary bias.
        context = build_realtime_context(meeting, policy, maximum_characters=80)

        self.assertNotIn("快速会议 7-14 16:00", context)
        self.assertIn("Ada", context)
        self.assertIn("KingbaseES", context)
        self.assertLessEqual(len(context), 80)

    def test_realtime_context_can_explicitly_include_title_for_legacy_general_callers(self) -> None:
        """The opt-in switch preserves callers whose non-realtime corpus still needs the title."""

        meeting = self._meeting()
        meeting["meetingName"] = "快速会议 7-14 16:00"
        policy = build_effective_vocabulary(meeting, self.store)

        # Title inclusion is deliberately explicit: realtime callers use the safe default, while
        # import/general callers can retain the historical corpus without duplicating its builder.
        context = build_realtime_context(meeting, policy, include_title=True)

        self.assertIn("快速会议 7-14 16:00", context)

    def test_realtime_context_filters_all_identity_fields_from_participants_and_policy_words(self) -> None:
        """Identity metadata must not re-enter realtime context through secondary policy sources."""

        meeting = self._meeting()
        meeting.update(
            {
                "meetingName": "实时会议标题",
                "title": "历史标题字段",
                "fileName": "导入文件名.wav",
            }
        )
        meeting["processingConfig"]["participantNames"] = ["Ada", "历史标题字段"]
        meeting["processingConfig"]["effectiveVocabulary"] = [
            "实时会议标题",
            "导入文件名.wav",
            "保留的策略词",
        ]
        policy = build_effective_vocabulary(meeting, self.store)

        # Each identity value deliberately comes from a source other than the field where it was
        # declared. This catches regressions where the builder merely stops prepending one title.
        context = build_realtime_context(meeting, policy)

        self.assertNotIn("实时会议标题", context)
        self.assertNotIn("历史标题字段", context)
        self.assertNotIn("导入文件名.wav", context)
        self.assertIn("Ada", context)
        self.assertIn("保留的策略词", context)

    def test_realtime_context_keeps_names_but_drops_generic_product_feature_terms(self) -> None:
        """实时 corpus 应增强专名，不能把产品功能菜单当成可能被说出的正文。"""

        meeting = self._meeting()
        meeting["processingConfig"]["participantNames"] = ["王忠", "Ada"]
        meeting["processingConfig"]["effectiveVocabulary"] = [
            "KingbaseES",
            "智能转写",
            "声纹注册",
            "强制对齐",
        ]
        policy = build_effective_vocabulary(meeting, self.store)

        context = build_realtime_context(meeting, policy)

        self.assertIn("王忠", context)
        self.assertIn("KingbaseES", context)
        self.assertNotIn("智能转写", context)
        self.assertNotIn("声纹注册", context)
        self.assertNotIn("强制对齐", context)

    def test_realtime_final_made_only_of_context_terms_is_rejected_without_revision(self) -> None:
        """供应商把多个 corpus 词原样串回时，必须在唯一落库边界整体拒绝。"""

        processing_config = self._meeting()["processingConfig"]
        processing_config["transcriptionMode"] = "realtime"
        processing_config["participantNames"] = ["王忠"]
        processing_config["effectiveVocabulary"] = ["KingbaseES", "Qwen3-ASR"]
        meeting = self.store.create_meeting(
            "上下文回声测试",
            meeting_id="meeting-context-list-echo",
            processing_config=processing_config,
            process_status="processing",
        )
        main.freeze_recognition_policy_snapshot(meeting, self.store)
        self.store._save("meetings", meeting)
        revision_before = int(meeting.get("transcriptRevision", 0))

        finalized = main._finalize_realtime_transcript_event(
            meeting["id"],
            {
                "type": "transcript",
                "segment": {"text": "王忠；KingbaseES；Qwen3-ASR。", "startMs": 0, "endMs": 1200},
            },
            "session-context-echo",
        )
        persisted = self.store.get_or_create_meeting(meeting["id"])

        self.assertEqual(finalized["type"], "status")
        self.assertEqual(finalized["code"], "context_echo_filtered")
        self.assertEqual(persisted["segments"], [])
        self.assertEqual(int(persisted.get("transcriptRevision", 0)), revision_before)

    def test_realtime_ai_filter_is_non_destructive_and_preserves_real_sentences(self) -> None:
        """历史 corpus 回声只从 AI 输入副本剔除，原始审计记录不得被自动删除。"""

        meeting = self._meeting()
        meeting["processingConfig"]["transcriptionMode"] = "realtime"
        meeting["processingConfig"]["recognitionPolicy"] = {
            "version": 1,
            "words": ["王忠", "KingbaseES", "智能转写", "声纹注册", "强制对齐"],
            "replacementRules": [],
            "sources": [],
            "snapshotHash": "snapshot-test",
        }
        meeting["segments"] = [
            {"id": "echo-1", "text": "智能转写；声纹注册；强制对齐。"},
            {"id": "echo-2", "text": "王忠；KingbaseES。"},
            {"id": "spoken-1", "text": "王忠介绍了 KingbaseES 的迁移计划。"},
        ]

        ai_meeting = filter_realtime_ai_segments(meeting)

        self.assertEqual([item["id"] for item in ai_meeting["segments"]], ["spoken-1"])
        self.assertEqual(len(meeting["segments"]), 3, "过滤只能操作深拷贝，不能修改数据库读取对象")

    def test_summary_route_sends_cleaned_copy_to_ai_without_deleting_history(self) -> None:
        """摘要入口应统一使用清洗后的片段，但持久化会议仍保留历史原文。"""

        processing_config = self._meeting()["processingConfig"]
        processing_config["transcriptionMode"] = "realtime"
        meeting = self.store.create_meeting(
            "AI 输入清理",
            meeting_id="meeting-ai-cleaning",
            processing_config=processing_config,
            process_status="completed",
        )
        main.freeze_recognition_policy_snapshot(meeting, self.store)
        meeting["segments"] = [
            {"id": "echo", "speakerName": "发言人1", "text": "智能转写；声纹注册；强制对齐。"},
            {"id": "real", "speakerName": "王忠", "text": "今天讨论 KingbaseES 迁移。"},
        ]
        self.store._save("meetings", meeting)
        captured: dict = {}

        def capture_summary(ai_meeting):
            captured["segmentIds"] = [item["id"] for item in ai_meeting["segments"]]
            return {"topic": "迁移", "keywords": [], "keyPoints": [], "todos": [], "speakerSummaries": []}

        with patch.object(main, "generate_summary_with_workflow", side_effect=capture_summary):
            main.generate_summary(meeting["id"])

        self.assertEqual(captured["segmentIds"], ["real"])
        self.assertEqual(
            [item["id"] for item in self.store.get_or_create_meeting(meeting["id"])["segments"]],
            ["echo", "real"],
        )

    def test_realtime_context_filter_removes_only_complete_identity_items(self) -> None:
        """Multiline browser context drops identity lines without altering normal sentence text."""

        meeting = {
            "meetingName": "实时会议标题",
            "title": "历史标题字段",
            "fileName": "导入文件名.wav",
        }
        normal_context = "上一段讨论了导入文件名.wav 的解析结果"

        # Browser context is serialized as newline-separated transcript segments, whereas known
        # speakers arrive as individual values. The helper must apply the same complete-item rule
        # to both shapes and must not perform substring replacement inside a useful sentence.
        filtered = filter_realtime_context_items(
            meeting,
            ["实时会议标题\n正常正文", "导入文件名.wav", normal_context, "王工"],
        )

        self.assertEqual(filtered, ("正常正文", normal_context, "王工"))

    def test_realtime_title_only_final_is_returned_as_status_without_persistence(self) -> None:
        """A normalized whole-title echo must consume neither a segment nor a transcript revision."""

        processing_config = self._meeting()["processingConfig"]
        processing_config["transcriptionMode"] = "realtime"
        meeting = self.store.create_meeting(
            "快速会议 7-14 16:00",
            meeting_id="meeting-title-echo",
            processing_config=processing_config,
            process_status="processing",
        )
        revision_before = int(meeting.get("transcriptRevision", 0))
        event = {
            "type": "transcript",
            # Surrounding whitespace and terminal punctuation model harmless provider variation;
            # they should not let the same title bypass whole-text echo comparison.
            "segment": {"text": "  快速会议 7-14 16:00。  ", "startMs": 0, "endMs": 1000},
        }

        finalized = main._finalize_realtime_transcript_event(meeting["id"], event, "session-title-echo")
        persisted = self.store.get_or_create_meeting(meeting["id"])

        self.assertEqual(finalized["type"], "status")
        self.assertEqual(finalized["code"], "context_echo_filtered")
        self.assertEqual(finalized["meetingId"], meeting["id"])
        self.assertEqual(finalized["sessionToken"], "session-title-echo")
        self.assertEqual(persisted["segments"], [])
        self.assertEqual(int(persisted.get("transcriptRevision", 0)), revision_before)

    def test_realtime_sentence_mentioning_title_is_persisted(self) -> None:
        """Only whole-title equality is filtered; a real sentence mentioning the title must remain."""

        processing_config = self._meeting()["processingConfig"]
        processing_config["transcriptionMode"] = "realtime"
        meeting = self.store.create_meeting(
            "快速会议 7-14 16:00",
            meeting_id="meeting-title-mention",
            processing_config=processing_config,
            process_status="processing",
        )
        sentence = "我们开始快速会议 7-14 16:00 的议程。"
        event = {"type": "transcript", "segment": {"text": sentence, "startMs": 0, "endMs": 1000}}

        finalized = main._finalize_realtime_transcript_event(meeting["id"], event, "session-title-mention")
        persisted = self.store.get_or_create_meeting(meeting["id"])

        self.assertEqual(finalized["type"], "transcript")
        self.assertEqual(finalized["segment"]["text"], sentence)
        self.assertEqual([segment["text"] for segment in persisted["segments"]], [sentence])
        self.assertEqual(int(persisted.get("transcriptRevision", 0)), 1)

    def test_replacements_preserve_raw_text_and_audit_only_final_text(self) -> None:
        """Provider text remains available while final persisted text records every rule application."""

        self.store.create_config_item(
            "replacement_rules",
            "rr",
            {"wrongWord": "voice print", "correctWord": "voiceprint", "enabled": True, "scope": "meeting-policy"},
        )
        policy = build_effective_vocabulary(self._meeting(), self.store)

        normalized = apply_final_replacements("voice print voice print", policy.replacement_rules, policy.rule_ids)

        self.assertEqual(normalized.raw_text, "voice print voice print")
        self.assertEqual(normalized.text, "voiceprint voiceprint")
        self.assertEqual(
            normalized.normalization_edits,
            (
                {"from": "voice print", "to": "voiceprint", "ruleId": next(iter(policy.rule_ids.values()))},
                {"from": "voice print", "to": "voiceprint", "ruleId": next(iter(policy.rule_ids.values()))},
            ),
        )

    def test_import_asr_receives_policy_words_and_persists_final_replacement_audit(self) -> None:
        """Import hotwords and its final segment must come from the same meeting policy snapshot."""

        self.store.create_config_item(
            "replacement_rules",
            "rr",
            {"wrongWord": "voice print", "correctWord": "voiceprint", "enabled": True, "scope": "meeting-policy"},
        )
        import_processing_config = self._meeting()["processingConfig"]
        # Task 4 routes deliberately reject unknown ownership.  This fixture exercises the
        # import boundary, so declare the immutable mode just as real import creation does.
        import_processing_config["transcriptionMode"] = "import"
        meeting = self.store.create_meeting(
            "KingbaseES architecture review",
            meeting_id="meeting-policy",
            processing_config=import_processing_config,
            process_status="processing",
        )
        meeting["smartKeywordTerms"] = [{"term": "Qwen3-ASR", "confirmed": True}]
        self.store._save("meetings", meeting)
        self.store._save(
            "files",
            {"id": "file-policy", "meetingId": meeting["id"], "filename": "policy.wav", "path": "policy.wav", "status": "uploaded"},
        )
        expected_policy = build_effective_vocabulary(meeting, self.store)
        calls: list[dict] = []

        class CapturingImportGateway:
            """ASR double that records the adapter inputs while returning provider-originated text."""

            def transcribe_offline(self, **kwargs):
                calls.append(kwargs)
                return {
                    "status": "completed",
                    "segments": [{"id": "import-policy-1", "text": "voice print", "startMs": 0, "endMs": 1000}],
                }

        main.asr_gateway = CapturingImportGateway()
        main.transcribe_file("file-policy", main.TranscribeRequest(enableDiarization=False))
        persisted = self.store.get_or_create_meeting(meeting["id"])["segments"][-1]

        self.assertEqual(calls[0]["hotwords"], list(expected_policy.words))
        self.assertEqual(persisted["rawText"], "voice print")
        self.assertEqual(persisted["text"], "voiceprint")
        self.assertEqual(persisted["normalizationEdits"][0]["ruleId"], next(iter(expected_policy.rule_ids.values())))

    def test_realtime_final_persistence_uses_policy_replacements_without_touching_partial_text(self) -> None:
        """A realtime final is normalized once, while an interim preview remains provider text."""

        self.store.create_config_item(
            "replacement_rules",
            "rr",
            {"wrongWord": "voice print", "correctWord": "voiceprint", "enabled": True, "scope": "meeting-policy"},
        )
        realtime_processing_config = self._meeting()["processingConfig"]
        # This fixture enters the realtime final writer directly; preserve that durable ownership
        # explicitly rather than relying on the removed legacy unknown-mode fallback.
        realtime_processing_config["transcriptionMode"] = "realtime"
        meeting = self.store.create_meeting(
            "KingbaseES architecture review",
            meeting_id="meeting-policy",
            processing_config=realtime_processing_config,
            process_status="processing",
        )
        partial = {"type": "partial_transcript", "text": "voice print"}
        event = {"type": "transcript", "segment": {"text": "voice print", "startMs": 0, "endMs": 1000}}

        finalized = main._finalize_realtime_transcript_event(meeting["id"], event, "session-policy")
        persisted = self.store.get_or_create_meeting(meeting["id"])["segments"][-1]

        self.assertEqual(partial["text"], "voice print")
        self.assertEqual(finalized["segment"]["rawText"], "voice print")
        self.assertEqual(persisted["text"], "voiceprint")
        self.assertEqual(len(persisted["normalizationEdits"]), 1)

    def test_document_extraction_persists_terms_from_actual_parsed_text(self) -> None:
        """Document extraction must store real parsed terms rather than the former demo list."""

        document_path = Path(self.temp_dir.name) / "architecture.txt"
        document_path.write_text("KingbaseES KingbaseES Qwen3-ASR policy graph", encoding="utf-8")
        self.store.create_config_item(
            "optimization_documents",
            "doc",
            {"id": "doc-parsed", "filename": "architecture.txt", "path": str(document_path), "status": "uploaded"},
        )

        result = main.extract_document_keywords({"documentId": "doc-parsed", "meetingId": "meeting-policy"})
        persisted = self.store._get("optimization_documents", "doc-parsed")

        self.assertIn("KingbaseES", result["keywords"])
        self.assertNotIn("智能转写", result["keywords"])
        self.assertEqual(persisted["parsedText"], "KingbaseES KingbaseES Qwen3-ASR policy graph")
        self.assertEqual(persisted["meetingIds"], ["meeting-policy"])

    def test_smart_terms_require_confirmation_before_the_policy_can_consume_them(self) -> None:
        """Generated suggestions stay inactive until an explicit meeting-scoped confirmation arrives."""

        meeting = self.store.create_meeting(
            "KingbaseES architecture review",
            meeting_id="meeting-policy",
            processing_config=self._meeting()["processingConfig"],
            process_status="processing",
        )
        meeting["segments"] = [{"id": "smart-source", "text": "CAM++ model discussion"}]
        self.store._save("meetings", meeting)

        suggested = main.generate_smart_keywords({"meetingId": meeting["id"], "limit": 10})
        before_confirmation = build_effective_vocabulary(self.store.get_or_create_meeting(meeting["id"]), self.store)
        confirmed = main.generate_smart_keywords(
            {"meetingId": meeting["id"], "confirmedTerms": ["CAM++"], "limit": 10}
        )
        after_confirmation = build_effective_vocabulary(self.store.get_or_create_meeting(meeting["id"]), self.store)

        self.assertIn("CAM++", suggested["keywords"])
        self.assertNotIn("CAM++", before_confirmation.words)
        self.assertEqual(confirmed["confirmedTerms"], ["CAM++"])
        self.assertIn("CAM++", after_confirmation.words)


if __name__ == "__main__":
    unittest.main()
