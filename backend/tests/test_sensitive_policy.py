"""TDD coverage for the meeting-scoped sensitive-word policy.

The tests deliberately use real route functions bound to a temporary SQLite store.  That proves
the display, AI, and export boundaries without reading or changing a developer's meeting data.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from docx import Document
from fastapi import HTTPException

# ``app.main`` creates its singleton store during import.  Redirect the configuration before the
# import so this module is safe to run on a developer workstation with existing meeting records.
_MODULE_DATA_DIR = TemporaryDirectory()
os.environ["DATA_DIR"] = _MODULE_DATA_DIR.name
os.environ["DATABASE_URL"] = str(Path(_MODULE_DATA_DIR.name) / "sensitive-policy-module.db")

from app import main
from app.asr_gateway import MockQwenAsrGateway
from app.sensitive_policy import apply_sensitive_policy
from app.store import PersistentStore


class SensitivePolicyTest(unittest.TestCase):
    """Exercise policy scope, frozen snapshots, and the consuming boundaries."""

    def setUp(self) -> None:
        """Bind application routes to a fresh store for every scenario."""

        self.temp_dir = TemporaryDirectory()
        self.store = PersistentStore(Path(self.temp_dir.name) / "sensitive-policy.db", seed_defaults=False)
        self.store_patcher = patch.object(main, "store", self.store)
        self.store_patcher.start()
        # ``PersistentStore.create_meeting`` preserves the legacy template compatibility field,
        # so even a policy-only isolated meeting needs one concrete template record.
        self.store.create_config_item("templates", "tpl", {"name": "sensitive policy template"})

    def tearDown(self) -> None:
        """Restore the process-wide route binding and release the temporary database."""

        self.store_patcher.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _display_rule() -> dict[str, object]:
        """Return one explicit display-only rule shared by the boundary scenarios."""

        return {
            "id": "sw-display",
            "word": "机密",
            "replacement": "stars",
            "applyScope": "display",
            "enabled": True,
            "caseSensitive": False,
            "language": "zh",
        }

    def _meeting_with_segment(self, *, mode: str = "realtime") -> dict[str, object]:
        """Create a meeting and persist provider/raw/final fields without mutating either text."""

        meeting = main._create_meeting_with_frozen_recognition_policy(
            main.MeetingCreateRequest(meetingName=f"{mode} policy meeting", language="zh"),
            mode=mode,
        )
        meeting["segments"] = [
            {
                "id": f"{mode}-segment-1",
                "startMs": 0,
                "endMs": 1000,
                "speakerName": "Ada",
                "rawText": "机密方案",
                "text": "机密方案",
            }
        ]
        return self.store._save("meetings", meeting)

    def test_display_only_rule_preserves_ai_and_export_text(self) -> None:
        """Each target applies only rules whose scope explicitly includes that target."""

        rule = self._display_rule()

        self.assertEqual(apply_sensitive_policy("机密方案", [rule], "display").text, "**方案")
        self.assertEqual(apply_sensitive_policy("机密方案", [rule], "ai").text, "机密方案")
        self.assertEqual(apply_sensitive_policy("机密方案", [rule], "export").text, "机密方案")

    def test_policy_honors_enabled_case_language_modes_overlap_and_stable_version(self) -> None:
        """Rule filtering and replacement order remain deterministic for audit replay."""

        rules = [
            {"id": "short", "word": "secret", "replacement": "stars", "applyScope": "all", "enabled": True, "language": "en"},
            {"id": "long", "word": "secret plan", "replacement": "stars", "applyScope": "all", "enabled": True, "language": "en"},
            {"id": "case", "word": "Token", "replacement": "hide", "applyScope": "export", "enabled": True, "caseSensitive": True, "language": "en"},
            {"id": "space", "word": "空格", "replacement": "space", "applyScope": "display", "enabled": True, "language": "zh"},
            {"id": "disabled", "word": "keep", "replacement": "stars", "applyScope": "all", "enabled": False, "language": "en"},
            {"id": "wrong-language", "word": "机密", "replacement": "stars", "applyScope": "all", "enabled": True, "language": "en"},
        ]

        display = apply_sensitive_policy("secret plan 空格 keep 机密", rules, "display")
        export = apply_sensitive_policy("token Token", rules, "export")

        self.assertEqual(display.text, "***********    keep 机密")
        self.assertEqual([hit["ruleId"] for hit in display.hits], ["long", "space"])
        self.assertEqual(export.text, "token ")
        self.assertEqual([hit["ruleId"] for hit in export.hits], ["case"])
        self.assertEqual(display.rule_version, apply_sensitive_policy("anything", list(reversed(rules)), "display").rule_version)
        with self.assertRaises(ValueError):
            apply_sensitive_policy("text", rules, "realtime")

    def test_legacy_sensitive_policy_backfill_does_not_overwrite_a_concurrent_realtime_final(self) -> None:
        """A one-time legacy policy snapshot must merge into the latest durable meeting row.

        The model double inserts a final segment after the caller has read its detached legacy
        meeting but before policy persistence.  This is the exact interleaving that made the old
        full-row save erase live speech while a user opened an AI/display feature for the first time.
        """

        meeting = main._create_meeting_with_frozen_recognition_policy(
            main.MeetingCreateRequest(meetingName="legacy sensitive backfill", language="zh"),
            mode="realtime",
        )
        processing_config = dict(meeting.get("processingConfig") or {})
        processing_config.pop("sensitivePolicy", None)
        processing_config.pop("sensitiveRuleVersion", None)
        meeting["processingConfig"] = processing_config
        self.store._save("meetings", meeting)
        detached_legacy_meeting = self.store.get_or_create_meeting(meeting["id"])
        real_freeze = main.freeze_sensitive_rule_snapshot

        def persist_realtime_final_during_policy_build(rules):
            self.store.add_realtime_segment(
                meeting["id"],
                {"id": "legacy-race-final", "text": "This final arrived during policy backfill.", "isFinal": True},
            )
            return real_freeze(rules)

        with patch.object(main, "freeze_sensitive_rule_snapshot", side_effect=persist_realtime_final_during_policy_build):
            main._frozen_sensitive_policy_for_meeting(detached_legacy_meeting)

        persisted = self.store.get_or_create_meeting(meeting["id"])
        self.assertIn("legacy-race-final", [segment["id"] for segment in persisted["segments"]])
        self.assertIsInstance(persisted["processingConfig"].get("sensitivePolicy"), dict)

    def test_frozen_snapshot_keeps_realtime_and_import_views_isolated_from_later_rule_edits(self) -> None:
        """Both record modes retain their creation-time policy while stored text stays untouched."""

        rule = self.store.create_config_item("sensitive_rules", "sw", self._display_rule())
        realtime = self._meeting_with_segment(mode="realtime")
        imported = self._meeting_with_segment(mode="import")
        original_version = realtime["processingConfig"]["sensitiveRuleVersion"]

        # Global administration changes after both meetings exist.  Historical display processing
        # must still use the copied rules rather than silently re-reading this mutable record.
        self.store.update_config_item("sensitive_rules", rule["id"], {"word": "公开", "enabled": True})

        for meeting in (realtime, imported):
            view = main.get_transcript_view(str(meeting["id"]))
            persisted = self.store.get_or_create_meeting(str(meeting["id"]))
            self.assertEqual(view["target"], "display")
            self.assertEqual(view["segments"][0]["displayText"], "**方案")
            self.assertEqual(view["ruleVersion"], original_version)
            self.assertEqual(persisted["segments"][0]["rawText"], "机密方案")
            self.assertEqual(persisted["segments"][0]["text"], "机密方案")

    def test_ai_receives_only_ai_target_text_and_persists_policy_audit(self) -> None:
        """AI input is transformed in a detached view and its artifact names the applied snapshot."""

        ai_rule = self._display_rule() | {"id": "sw-ai", "applyScope": "ai"}
        self.store.create_config_item("sensitive_rules", "sw", ai_rule)
        meeting = self._meeting_with_segment()
        captured: dict[str, object] = {}

        def fake_summary(ai_meeting: dict[str, object]) -> dict[str, object]:
            captured["meeting"] = ai_meeting
            return {"topic": "safe", "overview": "safe", "keywords": [], "keyPoints": [], "todos": []}

        with patch.object(main, "generate_summary_with_workflow", side_effect=fake_summary):
            result = main.generate_summary(str(meeting["id"]))

        ai_meeting = captured["meeting"]
        self.assertEqual(ai_meeting["segments"][0]["text"], "**方案")
        self.assertEqual(self.store.get_or_create_meeting(str(meeting["id"]))["segments"][0]["text"], "机密方案")
        self.assertTrue(result["sensitivePolicy"]["hits"])
        self.assertEqual(result["sensitivePolicy"]["ruleVersion"], meeting["processingConfig"]["sensitiveRuleVersion"])
        persisted = self.store.get_or_create_meeting(str(meeting["id"]))
        self.assertEqual(persisted["summaryArtifact"]["sensitivePolicy"], result["sensitivePolicy"])

    def test_docx_and_text_exports_use_export_target_and_record_audit_metadata(self) -> None:
        """Export masking is independent from display/AI rules and leaves stored source unchanged."""

        export_rule = self._display_rule() | {"id": "sw-export", "applyScope": "export"}
        self.store.create_config_item("sensitive_rules", "sw", export_rule)
        meeting = self._meeting_with_segment()

        docx_response = main.export_docx(str(meeting["id"]), main.ExportRequest(exportKind="transcript"))
        text_response = main.export_text(str(meeting["id"]), main.ExportRequest(exportKind="transcript"))
        document_text = "\n".join(paragraph.text for paragraph in Document(io.BytesIO(docx_response.body)).paragraphs)

        self.assertIn("**方案", document_text)
        self.assertNotIn("机密方案", document_text)
        self.assertIn("**方案", text_response.body.decode("utf-8"))
        persisted = self.store.get_or_create_meeting(str(meeting["id"]))
        self.assertEqual(persisted["segments"][0]["text"], "机密方案")
        self.assertEqual(len(persisted["exportAudits"]), 2)
        self.assertTrue(all(item["sensitivePolicy"]["hits"] for item in persisted["exportAudits"]))

    def test_frontend_consumes_display_view_without_reusing_masked_text_for_segment_patch(self) -> None:
        """The browser requests safe display text and never exposes it as the editable source field."""

        script = (Path(__file__).resolve().parents[2] / "frontend" / "app.js").read_text(encoding="utf-8")

        self.assertIn("/transcript-view?target=display", script)
        self.assertIn("data-segment-display-text", script)
        self.assertNotIn("applySensitiveRules(", script)
        self.assertNotIn('querySelector(`[data-segment-text="${segmentId}"]`)', script)

    def test_import_ingestion_keeps_source_text_unmasked_and_rejects_realtime_meetings(self) -> None:
        """Import ASR must retain source text; only the detached display view may mask it.

        ``MockQwenAsrGateway`` is deliberately used here because it exercised the legacy flat
        ``sensitive_words`` parameter before Task 4.  This regression therefore proves the real
        import ingestion boundary no longer persists that compatibility mask as transcript text.
        """

        self.store.create_config_item(
            "sensitive_rules",
            "sw",
            {"word": "ASR", "replacement": "stars", "applyScope": "display", "language": "en"},
        )
        imported = main._create_meeting_with_frozen_recognition_policy(
            main.MeetingCreateRequest(meetingName="import source preservation", language="zh"), mode="import"
        )
        file_record = self.store.save_file(str(imported["id"]), "import.wav", Path(__file__), "audio/wav")

        with patch.object(main, "asr_gateway", MockQwenAsrGateway()):
            result = main.transcribe_file(file_record["id"], main.TranscribeRequest(enableDiarization=False))

        persisted = self.store.get_or_create_meeting(str(imported["id"]))
        stored_segment = next(segment for segment in persisted["segments"] if "ASR" in segment["rawText"])
        display_view = main.get_transcript_view(str(imported["id"]))
        display_segment = next(segment for segment in display_view["segments"] if segment["id"] == stored_segment["id"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("ASR", stored_segment["rawText"])
        self.assertIn("ASR", stored_segment["text"])
        self.assertNotIn("ASR", display_segment["displayText"])

        realtime = main._create_meeting_with_frozen_recognition_policy(
            main.MeetingCreateRequest(meetingName="realtime cannot import", language="zh"), mode="realtime"
        )
        realtime_file = self.store.save_file(str(realtime["id"]), "wrong-mode.wav", Path(__file__), "audio/wav")
        with self.assertRaises(HTTPException) as rejection:
            main.transcribe_file(realtime_file["id"], main.TranscribeRequest(enableDiarization=False))
        self.assertEqual(rejection.exception.status_code, 409)

    def test_realtime_gateway_final_keeps_source_text_unmasked_and_rejects_import_meetings(self) -> None:
        """Realtime finals use their own write path and cannot fall back to import records."""

        self.store.create_config_item(
            "sensitive_rules",
            "sw",
            {"word": "ASR", "replacement": "stars", "applyScope": "display", "language": "en"},
        )
        realtime = main._create_meeting_with_frozen_recognition_policy(
            main.MeetingCreateRequest(meetingName="realtime source preservation", language="zh"), mode="realtime"
        )
        # This is the same concrete gateway event shape used by the synchronous realtime fallback.
        # Prior to the fix the gateway masked ``text`` and the persistence helper saved the stars.
        event = MockQwenAsrGateway().transcribe_realtime_chunk(str(realtime["id"]), 0, b"audio", ["ASR"])
        finalized = main._finalize_realtime_transcript_event(str(realtime["id"]), event, "session-source")

        persisted = self.store.get_or_create_meeting(str(realtime["id"]))
        stored_segment = persisted["segments"][0]
        self.assertIn("ASR", stored_segment["rawText"])
        self.assertIn("ASR", stored_segment["text"])
        self.assertIn("ASR", finalized["segment"]["text"])
        self.assertNotIn("ASR", main.get_transcript_view(str(realtime["id"]))["segments"][0]["displayText"])

        imported = main._create_meeting_with_frozen_recognition_policy(
            main.MeetingCreateRequest(meetingName="import cannot realtime", language="zh"), mode="import"
        )
        with self.assertRaises(HTTPException) as rejection:
            main._finalize_realtime_transcript_event(str(imported["id"]), event, "session-wrong-mode")
        self.assertEqual(rejection.exception.status_code, 409)

    def test_speaker_summary_and_minutes_template_use_frozen_ai_policy_with_audit(self) -> None:
        """Every AI-facing string, including template data, must use the frozen AI scope."""

        self.store.create_config_item(
            "sensitive_rules",
            "sw",
            {"word": "secret", "replacement": "stars", "applyScope": "ai", "language": "en"},
        )
        template = self.store.create_config_item(
            "templates",
            "tpl",
            {
                "name": "secret template",
                "content": "secret instructions",
                "tagBindings": [{"label": "secret binding"}],
            },
        )
        meeting = main._create_meeting_with_frozen_recognition_policy(
            main.MeetingCreateRequest(meetingName="secret meeting", language="en", templateId=template["id"]), mode="realtime"
        )
        meeting["segments"] = [{"id": "speaker-1", "speakerName": "Ada", "text": "secret discussion", "rawText": "secret discussion"}]
        self.store._save("meetings", meeting)
        captured: dict[str, object] = {}

        def fake_minutes(ai_meeting: dict[str, object], template_name: str, ai_template: dict[str, object]) -> dict[str, object]:
            captured["meeting"] = ai_meeting
            captured["template"] = ai_template
            captured["template_name"] = template_name
            return {"title": template_name, "templateName": template_name, "content": "safe"}

        speaker_summary = main.generate_speaker_summary(str(meeting["id"]))
        with patch.object(main, "generate_minutes_with_workflow", side_effect=fake_minutes):
            minutes = main.generate_minutes(str(meeting["id"]), main.MinutesRequest(templateName="fallback"))

        self.assertNotIn("secret", speaker_summary["items"][0]["summary"])
        self.assertEqual(speaker_summary["sensitivePolicy"]["target"], "ai")
        self.assertTrue(speaker_summary["sensitivePolicy"]["hits"])
        self.assertEqual(self.store.get_or_create_meeting(str(meeting["id"]))["speakerSummaryArtifact"]["sensitivePolicy"], speaker_summary["sensitivePolicy"])
        self.assertNotIn("secret", captured["meeting"]["segments"][0]["text"])
        self.assertNotIn("secret", captured["template"]["name"])
        self.assertNotIn("secret", captured["template"]["content"])
        self.assertNotIn("secret", captured["template"]["tagBindings"][0]["label"])
        self.assertNotIn("secret", captured["template_name"])
        self.assertTrue(minutes["sensitivePolicy"]["hits"])

    def test_idless_rule_version_is_independent_of_input_order(self) -> None:
        """Legacy rules without durable IDs still need a replayable order-independent version."""

        rules = [
            {"word": "alpha", "replacement": "stars", "applyScope": "display", "language": "en"},
            {"word": "beta", "replacement": "hide", "applyScope": "display", "language": "en"},
        ]
        self.assertEqual(
            apply_sensitive_policy("alpha beta", rules, "display").rule_version,
            apply_sensitive_policy("alpha beta", list(reversed(rules)), "display").rule_version,
        )
