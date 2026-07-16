import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

# Redirect configuration before importing the application. ``app.store`` creates its singleton
# during module import, so a per-test patch alone cannot protect the developer's default database.
_MODULE_DATA_DIR = TemporaryDirectory()
os.environ["DATA_DIR"] = _MODULE_DATA_DIR.name
os.environ["DATABASE_URL"] = str(Path(_MODULE_DATA_DIR.name) / "module-store.db")

import app.main as main_module
from app.store import PersistentStore


class MeetingProductClosureTest(unittest.TestCase):
    """Regression coverage for the meeting-level snapshot and revision contract."""

    def setUp(self):
        """Bind real route functions to a database private to this test case.

        ``app.main`` owns a process-wide store for the running application. Replacing that
        binding only during a test ensures this focused suite never calls ``reset`` on the
        durable developer database, while it continues to exercise production route code.
        """

        self.temp_dir = TemporaryDirectory()
        self.store = PersistentStore(Path(self.temp_dir.name) / "meeting_product_closure.db")
        self.store_patcher = patch.object(main_module, "store", self.store)
        self.store_patcher.start()
        self.audio_clip_dir = Path(self.temp_dir.name) / "audio_clips"
        self.audio_clip_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir_patcher = patch.object(main_module, "AUDIO_CLIP_DIR", self.audio_clip_dir)
        self.audio_dir_patcher.start()
        self.addCleanup(self.audio_dir_patcher.stop)
        self.addCleanup(self.store_patcher.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_meeting_snapshot_persists_product_configuration(self):
        """Realtime meeting creation freezes all selected processing inputs."""

        meeting = main_module.create_meeting(
            main_module.MeetingCreateRequest(
                meetingName="project review",
                language="en-US",
                audioSource="desk microphone",
                enableDiarization=True,
                participantNames=[" Avery ", "", "Avery", "Blake", " Blake "],
                voiceprintGroupId=" vg-office ",
                optimizationProfile={"manual": True, "document": False, "smart": True, "replacement": True},
                keywordLibraryIds=[" kw-001 ", "", "kw-001"],
                templateId=" tpl-001 ",
                notes="internal review only",
                attachments=[{"id": "attachment-1", "name": "agenda.docx"}],
            )
        )

        config = meeting.get("processingConfig", {})
        self.assertEqual(config.get("transcriptionMode"), "realtime")
        self.assertEqual(config.get("participantNames"), ["Avery", "Blake"])
        self.assertEqual(config.get("voiceprintGroupId"), "vg-office")
        self.assertEqual(config.get("keywordLibraryIds"), ["kw-001"])
        self.assertEqual(meeting.get("keywordLibraryIds"), ["kw-001"])
        self.assertEqual(config.get("templateId"), "tpl-001")
        self.assertEqual(meeting.get("transcriptRevision"), 0)

    def test_import_snapshot_uses_import_mode_without_realtime_fallback(self):
        """The import entry point creates its own import-mode snapshot and record."""

        async def fake_save_uploaded_audio_file(meeting_id, file):
            return {"id": "file-import", "meetingId": meeting_id, "filename": file.filename}

        with (
            patch("app.main.save_uploaded_audio_file", new=fake_save_uploaded_audio_file),
            patch("app.main.transcribe_file", return_value={"status": "completed", "segments": []}),
        ):
            result = asyncio.run(
                main_module.import_and_transcribe_file(
                    file=SimpleNamespace(filename="import.wav"),
                    language="en-US",
                    template_id="tpl-001",
                    keyword_library_ids="kw-001",
                    enable_diarization=False,
                )
            )

        config = result["meeting"].get("processingConfig", {})
        self.assertEqual(config.get("transcriptionMode"), "import")
        self.assertEqual(config.get("audioSource"), result["meeting"].get("audioSource"))
        self.assertEqual(config.get("keywordLibraryIds"), ["kw-001"])

    def test_processing_snapshot_is_unchanged_after_global_configuration_edits(self):
        """Later library/template edits must not rewrite a historical meeting snapshot."""

        meeting = main_module.create_meeting(
            main_module.MeetingCreateRequest(meetingName="immutable snapshot", keywordLibraryIds=["kw-001"])
        )
        before = meeting.get("processingConfig", {})

        self.store.update_config_item("keyword_libraries", "kw-001", {"words": ["changed later"]})
        self.store.update_config_item("templates", "tpl-001", {"name": "changed later"})
        after = self.store.get_or_create_meeting(meeting["id"]).get("processingConfig", {})

        self.assertIn("effectiveVocabulary", after)
        self.assertEqual(after, before)
        self.assertNotIn("changed later", after.get("effectiveVocabulary", []))

    def test_snapshot_canonicalizes_vocabulary_and_frozen_sensitive_words(self):
        """Snapshot strings are trimmed, blank-free, and stable after first occurrence."""

        self.store.update_config_item(
            "keyword_libraries",
            "kw-001",
            {"words": [" frozen term ", "", "frozen term", "second term "]},
        )
        # Disable every seeded rule first so this fixture proves the snapshot's trimming and
        # deduplication behavior without depending on unrelated demo-sensitive-word defaults.
        for rule_id in self.store.sensitive_rules:
            self.store.update_config_item("sensitive_rules", rule_id, {"enabled": False})
        sensitive_rule_id = next(iter(self.store.sensitive_rules))
        self.store.update_config_item(
            "sensitive_rules",
            sensitive_rule_id,
            {"word": " frozen secret ", "enabled": True},
        )

        meeting = main_module.create_meeting(
            main_module.MeetingCreateRequest(
                meetingName="canonical snapshot",
                keywordLibraryIds=[" kw-001 ", "", "kw-001"],
                participantNames=[" Avery ", "", "Avery", " Blake "],
            )
        )
        config = meeting["processingConfig"]

        self.assertEqual(config["keywordLibraryIds"], ["kw-001"])
        self.assertEqual(config["participantNames"], ["Avery", "Blake"])
        self.assertEqual(config["effectiveVocabulary"], ["frozen term", "second term"])
        # Task 4 keeps ASR source text unmasked. Detailed rules are frozen separately and applied
        # only at display/AI/export boundaries; the legacy flat ASR list must remain empty.
        self.assertEqual(config["sensitiveWords"], [])
        frozen_rule_words = [rule["word"] for rule in config["sensitivePolicy"]["rules"]]
        self.assertIn("frozen secret", frozen_rule_words)
        self.assertTrue(all(word == word.strip() and word for word in frozen_rule_words))
        self.assertEqual(len(frozen_rule_words), len(set(frozen_rule_words)))

    def test_import_and_realtime_asr_use_frozen_snapshot_inputs_after_global_edits(self):
        """Both ASR paths must receive values captured when the meeting was created."""

        class CapturingGateway:
            """Record arguments at both production ASR boundaries without simulating behavior."""

            def __init__(self):
                self.offline_calls = []
                self.realtime_calls = []

            def transcribe_offline(self, **kwargs):
                self.offline_calls.append(kwargs)
                return {"status": "completed", "segments": []}

            def transcribe_realtime_chunk(self, meeting_id, chunk_index, audio_chunk, sensitive_words, **kwargs):
                self.realtime_calls.append({"sensitive_words": sensitive_words, **kwargs})
                return {"type": "status", "meetingId": meeting_id}

        class FakeWebSocket:
            """Drive the route's non-streaming ASR fallback without a network server."""

            def __init__(self):
                self.messages = [
                    {"text": json.dumps({"type": "realtime_config"})},
                    {"bytes": b"audio"},
                    {"text": "stop"},
                ]
                self.sent_messages = []

            async def accept(self):
                return None

            async def receive(self):
                return self.messages.pop(0)

            async def send_text(self, text):
                self.sent_messages.append(text)

        self.store.update_config_item("keyword_libraries", "kw-001", {"words": ["frozen vocabulary"]})
        # Isolate one enabled rule so exact gateway-boundary assertions remain readable and
        # cannot inherit a second seeded word from the default test data.
        for rule_id in self.store.sensitive_rules:
            self.store.update_config_item("sensitive_rules", rule_id, {"enabled": False})
        sensitive_rule_id = next(iter(self.store.sensitive_rules))
        self.store.update_config_item("sensitive_rules", sensitive_rule_id, {"word": "frozen secret", "enabled": True})
        realtime_meeting_record = main_module.create_meeting(
            main_module.MeetingCreateRequest(meetingName="frozen ASR", keywordLibraryIds=["kw-001"])
        )
        import_meeting = main_module._create_meeting_with_frozen_recognition_policy(
            main_module.MeetingCreateRequest(meetingName="frozen import ASR", keywordLibraryIds=["kw-001"]),
            mode="import",
        )
        file_record = self.store.save_file(import_meeting["id"], "frozen.wav", Path(__file__), "audio/wav")

        # These mutations must affect future meetings only. The recorded ASR inputs below
        # demonstrate that neither import nor realtime reads the mutable store aliases.
        self.store.update_config_item("keyword_libraries", "kw-001", {"words": ["mutated vocabulary"]})
        self.store.update_config_item("sensitive_rules", sensitive_rule_id, {"word": "mutated secret", "enabled": True})
        gateway = CapturingGateway()
        websocket = FakeWebSocket()

        with (
            patch.object(main_module, "asr_gateway", gateway),
            patch.object(
                main_module,
                "analyze_realtime_chunk_quality",
                return_value=SimpleNamespace(has_voice=True, duration_ms=1000),
            ),
        ):
            main_module.transcribe_file(file_record["id"], main_module.TranscribeRequest())
            asyncio.run(main_module.realtime_meeting(websocket, realtime_meeting_record["id"]))

        self.assertEqual(gateway.offline_calls[0]["hotwords"], ["frozen vocabulary"])
        self.assertEqual(gateway.offline_calls[0]["sensitive_words"], [])
        self.assertEqual(gateway.realtime_calls[0]["sensitive_words"], [])
        self.assertIn("frozen vocabulary", gateway.realtime_calls[0]["context_text"])

    def test_persisted_final_segments_bump_revision_but_partial_segments_do_not(self):
        """Only durable final text can invalidate transcript-derived artifacts."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="revision boundary"))
        self.store.add_realtime_segment(meeting["id"], {"id": "partial-1", "text": "preview only", "isFinal": False})
        self.assertEqual(self.store.get_or_create_meeting(meeting["id"]).get("transcriptRevision"), 0)

        self.store.add_realtime_segment(meeting["id"], {"id": " final-1 ", "text": "durable realtime text", "isFinal": True})
        updated = self.store.get_or_create_meeting(meeting["id"])

        self.assertEqual(updated.get("transcriptRevision"), 1)
        self.assertEqual(updated.get("transcriptRevisionReason"), "realtime_final")
        self.assertEqual(updated.get("transcriptRevisionSegmentIds"), ["final-1"])

    def test_completed_import_persistence_bumps_revision_once_for_all_final_segments(self):
        """A completed import persists final text as one revision, not a realtime substitute."""

        meeting = main_module._create_meeting_with_frozen_recognition_policy(
            main_module.MeetingCreateRequest(meetingName="import revision"), mode="import"
        )
        self.store.add_transcript(
            meeting["id"],
            "file-import",
            [
                {"id": " import-1 ", "text": "first imported sentence"},
                {"id": "", "text": "id-less imported sentence"},
                {"id": "import-1", "text": "duplicate imported sentence"},
            ],
        )
        updated = self.store.get_or_create_meeting(meeting["id"])

        self.assertEqual(updated.get("transcriptRevision"), 1)
        self.assertEqual(updated.get("transcriptRevisionReason"), "import_completed")
        persisted_ids = [segment["id"] for segment in updated["segments"]]
        self.assertEqual(persisted_ids[0], "import-1")
        self.assertEqual(len(persisted_ids), len(set(persisted_ids)))
        self.assertTrue(all(segment_id.strip() for segment_id in persisted_ids))
        self.assertEqual(updated.get("transcriptRevisionSegmentIds"), persisted_ids)

    def test_realtime_segment_without_provider_id_receives_a_durable_id(self):
        """Final realtime text remains traceable even when the provider omits its segment ID."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="realtime IDs"))
        self.store.add_realtime_segment(meeting["id"], {"id": " ", "text": "hello", "isFinal": True})
        updated = self.store.get_or_create_meeting(meeting["id"])

        persisted_id = updated["segments"][0]["id"]
        self.assertTrue(persisted_id.strip())
        self.assertEqual(updated["transcriptRevisionSegmentIds"], [persisted_id])

    def test_meeting_patch_keeps_top_level_keyword_library_ids_canonical(self):
        """The legacy mutable field stays canonical without rewriting the immutable snapshot."""

        meeting = main_module.create_meeting(
            main_module.MeetingCreateRequest(meetingName="patched libraries", keywordLibraryIds=["kw-001"])
        )
        original_snapshot = json.loads(json.dumps(meeting["processingConfig"]))

        updated = self.store.update_meeting(
            meeting["id"], {"keywordLibraryIds": [" kw-002 ", "", "kw-002"]}
        )

        self.assertEqual(updated["keywordLibraryIds"], ["kw-002"])
        self.assertEqual(updated["processingConfig"], original_snapshot)

    def test_text_and_speaker_patch_increment_revision_but_metadata_patch_does_not(self):
        """Only transcript text and speaker identity are revision-bearing editor changes."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="editor revision"))
        self.store.add_realtime_segment(meeting["id"], {"id": "segment-1", "text": "original", "speakerName": "Avery"})

        self.store.update_meeting_segment(meeting["id"], "segment-1", {"marked": True})
        self.assertEqual(self.store.get_or_create_meeting(meeting["id"]).get("transcriptRevision"), 1)

        self.store.update_meeting_segment(meeting["id"], "segment-1", {"text": "corrected", "speakerName": "Blake"})
        updated = self.store.get_or_create_meeting(meeting["id"])

        self.assertEqual(updated.get("transcriptRevision"), 2)
        self.assertEqual(updated.get("transcriptRevisionReason"), "segment_patch")
        self.assertEqual(updated.get("transcriptRevisionSegmentIds"), ["segment-1"])

    def test_store_exposes_speaker_wide_rename_as_a_revision_bearing_operation(self):
        """A consistent speaker rename is an explicit transcript mutation, not metadata."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="speaker-wide rename"))
        self.store.add_realtime_segment(meeting["id"], {"id": "speaker-1", "text": "first", "speakerName": "Avery"})
        self.store.add_realtime_segment(meeting["id"], {"id": "speaker-2", "text": "second", "speakerName": "Avery"})

        changed = self.store.rename_meeting_speaker(meeting["id"], "Avery", "Blake")
        updated = self.store.get_or_create_meeting(meeting["id"])

        self.assertEqual([segment["id"] for segment in changed], ["speaker-1", "speaker-2"])
        self.assertEqual([segment["speakerName"] for segment in updated["segments"]], ["Blake", "Blake"])
        self.assertEqual(updated.get("transcriptRevision"), 3)
        self.assertEqual(updated.get("transcriptRevisionReason"), "speaker_rename")
        self.assertEqual(updated.get("transcriptRevisionSegmentIds"), ["speaker-1", "speaker-2"])

    def test_generated_artifacts_capture_current_canonical_transcript_sources(self):
        """Summary, minutes, and todos retain durable source IDs and timestamp ranges."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="artifact provenance"))
        self.store.add_realtime_segment(
            meeting["id"],
            {"id": " source-a ", "text": "Avery will prepare the release plan.", "startMs": 10, "endMs": 40},
        )
        self.store.add_realtime_segment(
            meeting["id"],
            {"id": "source-b", "text": "Blake will review the rollout risks.", "startMs": 41, "endMs": 90},
        )

        summary = main_module.generate_summary(meeting["id"])
        minutes = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())
        todos = main_module.extract_todos(meeting["id"])
        refreshed = self.store.get_or_create_meeting(meeting["id"])

        expected_ids = ["source-a", "source-b"]
        for response, artifact_key in (
            (summary, "summaryArtifact"),
            (minutes, "minutesArtifact"),
            (todos, "todosArtifact"),
        ):
            artifact = refreshed[artifact_key]
            self.assertEqual(response["status"], "current")
            self.assertEqual(artifact["status"], "current")
            self.assertEqual(artifact["sourceTranscriptRevision"], refreshed["transcriptRevision"])
            self.assertEqual(artifact["sourceSegmentIds"], expected_ids)
            self.assertEqual(
                artifact["sourceRanges"],
                [
                    {"segmentId": "source-a", "startMs": 10, "endMs": 40},
                    {"segmentId": "source-b", "startMs": 41, "endMs": 90},
                ],
            )
            self.assertIn("generatedContent", artifact)
            self.assertIsNone(artifact["editedContent"])

        # Existing consumers still receive the former result fields alongside the new envelope.
        self.assertIn("overview", summary)
        self.assertIn("content", minutes)
        self.assertIn("items", todos)

    def test_editing_transcript_marks_artifacts_stale_without_erasing_generated_or_edited_content(self):
        """A revision transition stales old results but preserves both content layers for review."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="artifact invalidation"))
        self.store.add_realtime_segment(
            meeting["id"],
            {"id": "segment-1", "text": "The first transcript wording.", "startMs": 0, "endMs": 100},
        )
        main_module.generate_summary(meeting["id"])
        main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())
        main_module.extract_todos(meeting["id"])
        before_edit = self.store.get_or_create_meeting(meeting["id"])
        generated_minutes = before_edit["minutesArtifact"]["generatedContent"]

        main_module.save_minutes_draft(
            meeting["id"],
            main_module.MinutesDraftRequest(content="Human-edited minutes must survive invalidation."),
        )
        main_module.patch_meeting_segment(
            meeting["id"],
            "segment-1",
            main_module.SegmentPatchRequest(text="Corrected transcript wording."),
        )
        refreshed = self.store.get_or_create_meeting(meeting["id"])

        for artifact_key in ("summaryArtifact", "minutesArtifact", "todosArtifact"):
            artifact = refreshed[artifact_key]
            self.assertEqual(artifact["status"], "stale")
            self.assertLess(artifact["sourceTranscriptRevision"], refreshed["transcriptRevision"])
            self.assertIn("generatedContent", artifact)
        self.assertEqual(refreshed["minutesArtifact"]["generatedContent"], generated_minutes)
        self.assertEqual(
            refreshed["minutesArtifact"]["editedContent"],
            "Human-edited minutes must survive invalidation.",
        )

    def test_regeneration_at_the_current_revision_returns_a_current_artifact(self):
        """Regeneration replaces stale provenance with references to the latest persisted revision."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="artifact regeneration"))
        self.store.add_realtime_segment(meeting["id"], {"id": "segment-1", "text": "Initial text."})
        main_module.generate_summary(meeting["id"])
        main_module.patch_meeting_segment(
            meeting["id"],
            "segment-1",
            main_module.SegmentPatchRequest(text="Corrected text."),
        )

        regenerated = main_module.generate_summary(meeting["id"])
        refreshed = self.store.get_or_create_meeting(meeting["id"])

        self.assertEqual(regenerated["status"], "current")
        self.assertEqual(refreshed["summaryArtifact"]["status"], "current")
        self.assertEqual(
            refreshed["summaryArtifact"]["sourceTranscriptRevision"],
            refreshed["transcriptRevision"],
        )

    def test_late_single_artifact_generations_stay_stale_without_replacing_current_results(self):
        """A transcript write during each model call must keep the earlier current result intact.

        This intentionally performs the transcript mutation inside the model double rather than before
        calling the route.  It models the production interleaving precisely: the route has already
        captured its generation snapshot, but the durable transcript advances before the model result
        reaches the artifact save boundary.
        """

        cases = (
            (
                "summary",
                "summaryArtifact",
                "generate_summary_with_workflow",
                lambda meeting_id: main_module.generate_summary(meeting_id),
                {"overview": "Current summary", "keywords": [], "todos": []},
                {"overview": "Late summary", "keywords": [], "todos": []},
                "summary",
                {"overview": "Current summary", "keywords": [], "todos": []},
            ),
            (
                "todos",
                "todosArtifact",
                "extract_todos_with_workflow",
                lambda meeting_id: main_module.extract_todos(meeting_id),
                [{"title": "Current todo"}],
                [{"title": "Late todo"}],
                "todos",
                [{"title": "Current todo"}],
            ),
            (
                "discourse",
                "discourseArtifact",
                "reorganize_discourse",
                lambda meeting_id: main_module.reorganize_meeting_discourse(meeting_id),
                "Current discourse.",
                "Late discourse.",
                "discourse",
                {"text": "Current discourse."},
            ),
        )

        for (
            artifact_type,
            artifact_field,
            workflow_name,
            invoke,
            current_model_result,
            late_model_result,
            legacy_field,
            expected_legacy_content,
        ) in cases:
            with self.subTest(artifact_type=artifact_type):
                meeting = main_module.create_meeting(
                    main_module.MeetingCreateRequest(meetingName=f"late {artifact_type} generation")
                )
                self.store.add_realtime_segment(
                    meeting["id"],
                    {"id": "c1-source", "text": "The initial transcript.", "startMs": 0, "endMs": 10},
                )

                # The first generation establishes a known-good current artifact that a late result
                # must never replace in either the compatibility fields or the current envelope.
                with patch.object(main_module, workflow_name, return_value=current_model_result):
                    current_response = invoke(meeting["id"])

                def advance_transcript_during_generation(*_args: object, **_kwargs: object):
                    main_module.patch_meeting_segment(
                        meeting["id"],
                        "c1-source",
                        main_module.SegmentPatchRequest(text="The transcript changed while the model ran."),
                    )
                    return late_model_result

                with patch.object(main_module, workflow_name, side_effect=advance_transcript_during_generation):
                    late_response = invoke(meeting["id"])

                persisted = self.store.get_or_create_meeting(meeting["id"])
                self.assertEqual(late_response["status"], "stale")
                # The earlier result is already stale because the transcript changed, but its
                # compatibility field remains the last published data.  The late model response
                # must not overwrite that field while it is retained with honest stale provenance.
                self.assertEqual(persisted[legacy_field], expected_legacy_content)
                stale = persisted[artifact_field]
                self.assertEqual(stale["status"], "stale")
                self.assertEqual(stale["generatedContent"], late_response["generatedContent"])
                self.assertLess(stale["sourceTranscriptRevision"], persisted["transcriptRevision"])
                self.assertEqual(stale["sourceSegmentIds"], ["c1-source"])
                self.assertIn("sensitivePolicy", stale)

    def test_concurrent_transcript_and_derived_artifact_save_do_not_lose_the_transcript(self):
        """The artifact save must serialize with final-segment persistence through SQLite.

        The barrier releases both writers from separate threads at the same time.  Whichever writer
        acquires SQLite first, the final durable record must retain the new segment and mark the
        generation based on the earlier snapshot stale after both operations complete.
        """

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="artifact save race"))
        self.store.add_realtime_segment(meeting["id"], {"id": "race-source", "text": "Before race."})
        snapshot = self.store.get_or_create_meeting(meeting["id"])
        barrier = threading.Barrier(3)
        errors: list[BaseException] = []

        def save_artifact() -> None:
            try:
                barrier.wait()
                self.store.save_derived_artifact(
                    meeting["id"],
                    "summary",
                    {"overview": "Concurrent model output", "keywords": [], "todos": []},
                    ["race-source"],
                    generation_transcript_revision=snapshot["transcriptRevision"],
                    sensitive_policy={"target": "ai", "ruleVersion": "none", "hits": []},
                )
            except BaseException as exc:  # noqa: BLE001 - surface worker failures in the test process.
                errors.append(exc)

        def save_final_segment() -> None:
            try:
                barrier.wait()
                self.store.add_realtime_segment(
                    meeting["id"],
                    {"id": "race-final", "text": "A final arrived during artifact persistence.", "isFinal": True},
                )
            except BaseException as exc:  # noqa: BLE001 - surface worker failures in the test process.
                errors.append(exc)

        workers = [threading.Thread(target=save_artifact), threading.Thread(target=save_final_segment)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=10)

        self.assertEqual(errors, [])
        persisted = self.store.get_or_create_meeting(meeting["id"])
        self.assertIn("race-final", [segment["id"] for segment in persisted["segments"]])
        self.assertEqual(persisted["transcriptRevision"], snapshot["transcriptRevision"] + 1)
        self.assertEqual(persisted["summaryArtifact"]["status"], "stale")
        self.assertEqual(persisted["summaryArtifact"]["sourceTranscriptRevision"], snapshot["transcriptRevision"])

    def test_discourse_and_highlight_provenance_stale_together_in_both_durable_representations(self):
        """Discourse and highlight artifacts retain canonical sources, and both highlight copies stale together."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="highlight provenance"))
        self.store.add_realtime_segment(
            meeting["id"],
            {"id": "highlight-source", "text": "Avery confirmed the release decision.", "startMs": 10, "endMs": 40},
        )

        discourse = main_module.reorganize_meeting_discourse(meeting["id"])
        highlight = main_module.add_highlight(
            meeting["id"], {"text": "Release decision", "segmentId": "highlight-source"}
        )
        before_edit = self.store.get_or_create_meeting(meeting["id"])

        self.assertEqual(discourse["status"], "current")
        self.assertEqual(before_edit["discourseArtifact"]["sourceSegmentIds"], ["highlight-source"])
        self.assertEqual(highlight["status"], "current")
        self.assertEqual(highlight["sourceRanges"], [{"segmentId": "highlight-source", "startMs": 10, "endMs": 40}])

        main_module.patch_meeting_segment(
            meeting["id"], "highlight-source", main_module.SegmentPatchRequest(text="Avery corrected the release decision.")
        )
        after_edit = self.store.get_or_create_meeting(meeting["id"])
        persisted_highlight = next(item for item in self.store.highlights if item["id"] == highlight["id"])

        self.assertEqual(after_edit["discourseArtifact"]["status"], "stale")
        self.assertEqual(after_edit["highlightArtifacts"][0]["status"], "stale")
        self.assertEqual(persisted_highlight["artifact"]["status"], "stale")
        self.assertEqual(
            persisted_highlight["artifact"],
            after_edit["highlightArtifacts"][0],
        )

    def test_detail_read_repairs_a_legacy_highlight_representation_mismatch(self):
        """Reading detail heals rows written while the two compatible highlight copies diverged."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="legacy highlight repair"))
        self.store.add_realtime_segment(meeting["id"], {"id": "repair-source", "text": "Source text."})
        highlight = main_module.add_highlight(
            meeting["id"], {"text": "Repair marker", "segmentId": "repair-source"}
        )
        main_module.patch_meeting_segment(
            meeting["id"], "repair-source", main_module.SegmentPatchRequest(text="Corrected source text.")
        )
        row = next(item for item in self.store.highlights if item["id"] == highlight["id"])
        row["artifact"]["status"] = "current"
        self.store._save("highlights", row)

        refreshed = self.store.get_or_create_meeting(meeting["id"])
        repaired = next(item for item in self.store.highlights if item["id"] == highlight["id"])

        self.assertEqual(refreshed["highlightArtifacts"][0]["status"], "stale")
        self.assertEqual(repaired["artifact"], refreshed["highlightArtifacts"][0])

    def test_highlights_reject_nonblank_unknown_or_cross_meeting_sources_and_label_unsourced_markers(self):
        """A marker may be deliberately unlinked, but a supplied source must belong to this meeting."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="source validation"))
        other_meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="other meeting"))
        self.store.add_realtime_segment(meeting["id"], {"id": "local-source", "text": "Local transcript text."})
        self.store.add_realtime_segment(other_meeting["id"], {"id": "foreign-source", "text": "Foreign transcript text."})

        with self.assertRaises(HTTPException) as unknown_error:
            main_module.add_highlight(meeting["id"], {"text": "Invalid", "segmentId": "missing-source"})
        self.assertEqual(unknown_error.exception.status_code, 400)

        with self.assertRaises(HTTPException) as cross_meeting_error:
            main_module.add_highlight(meeting["id"], {"text": "Cross meeting", "segmentId": "foreign-source"})
        self.assertEqual(cross_meeting_error.exception.status_code, 400)

        unlinked = main_module.add_highlight(meeting["id"], {"text": "Unlinked note", "segmentId": ""})

        self.assertEqual(unlinked["status"], "unlinked")
        self.assertIsNone(unlinked["sourceTranscriptRevision"])
        self.assertEqual(unlinked["sourceSegmentIds"], [])
        self.assertEqual(unlinked["sourceRanges"], [])

    def test_minutes_generation_uses_the_frozen_processing_template_snapshot_and_id(self):
        """Minutes use the template selected when the meeting started, even after global template edits."""

        meeting = main_module.create_meeting(
            main_module.MeetingCreateRequest(meetingName="template provenance", templateId="tpl-001")
        )
        self.store.add_realtime_segment(meeting["id"], {"id": "template-source", "text": "Template evidence."})
        frozen_snapshot = meeting["processingConfig"]["templateSnapshot"]
        self.store.update_config_item("templates", "tpl-001", {"name": "Changed global template"})

        with patch.object(
            main_module,
            "generate_minutes_with_workflow",
            return_value={"templateName": frozen_snapshot["name"], "content": "Frozen template minutes."},
        ) as workflow:
            response = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest(templateName="Request fallback"))

        workflow.assert_called_once()
        self.assertEqual(workflow.call_args.args[1], frozen_snapshot["name"])
        self.assertEqual(workflow.call_args.args[2], frozen_snapshot)
        self.assertEqual(response["templateId"], "tpl-001")
        self.assertEqual(self.store.get_or_create_meeting(meeting["id"])["minutesArtifact"]["templateId"], "tpl-001")

    def test_pre_generation_minutes_draft_keeps_generated_content_absent_and_status_draft(self):
        """A user draft is not model output, including for an empty meeting with no source transcript."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="draft before generation"))

        response = main_module.save_minutes_draft(
            meeting["id"], main_module.MinutesDraftRequest(sourceTool="summary", content="Human draft before generation."),
        )
        artifact = self.store.get_or_create_meeting(meeting["id"])["minutesArtifact"]

        self.assertEqual(response["status"], "draft")
        self.assertEqual(artifact["status"], "draft")
        self.assertIsNone(artifact["generatedContent"])
        self.assertEqual(artifact["editedContent"], "Human draft before generation.")
        self.assertEqual(artifact["sourceSegmentIds"], [])
        self.assertEqual(artifact["sourceRanges"], [])

    def test_batch_segment_patch_updates_multiple_rows_with_one_revision(self):
        """批量编辑多个底层片段必须只产生一次逐字稿版本，且保留所有 segment id。"""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="atomic batch edit"))
        self.store.add_realtime_segment(meeting["id"], {"id": "batch-1", "text": "first", "speakerName": "speaker 1"})
        self.store.add_realtime_segment(meeting["id"], {"id": "batch-2", "text": "second", "speakerName": "speaker 1"})
        before = self.store.get_or_create_meeting(meeting["id"])

        result = main_module.patch_meeting_segments_batch(
            meeting["id"],
            main_module.SegmentBatchPatchRequest(
                expectedTranscriptRevision=before["transcriptRevision"],
                updates=[
                    {"segmentId": "batch-1", "text": "edited first"},
                    {"segmentId": "batch-2", "text": "edited second", "speakerName": "Alice"},
                ],
            ),
        )

        self.assertEqual(result["transcriptRevision"], before["transcriptRevision"] + 1)
        self.assertEqual(result["updatedSegmentIds"], ["batch-1", "batch-2"])
        self.assertEqual([item["id"] for item in result["segments"]], ["batch-1", "batch-2"])

    def test_batch_segment_patch_rejects_revision_conflict_without_writes(self):
        """旧页面提交过期 revision 时返回 409，不能覆盖其他用户已保存的文本。"""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="revision conflict"))
        self.store.add_realtime_segment(meeting["id"], {"id": "conflict-1", "text": "current"})
        current = self.store.get_or_create_meeting(meeting["id"])

        with self.assertRaises(HTTPException) as conflict:
            main_module.patch_meeting_segments_batch(
                meeting["id"],
                main_module.SegmentBatchPatchRequest(
                    expectedTranscriptRevision=current["transcriptRevision"] - 1,
                    updates=[{"segmentId": "conflict-1", "text": "must not persist"}],
                ),
            )

        self.assertEqual(conflict.exception.status_code, 409)
        refreshed = self.store.get_or_create_meeting(meeting["id"])
        self.assertEqual(refreshed["segments"][0]["text"], "current")
        self.assertEqual(refreshed["transcriptRevision"], current["transcriptRevision"])

    def test_batch_segment_patch_rolls_back_when_any_segment_is_missing(self):
        """同批次任一 segment 不存在时，其他合法片段也不能出现半成功。"""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="batch rollback"))
        self.store.add_realtime_segment(meeting["id"], {"id": "valid-1", "text": "unchanged"})
        current = self.store.get_or_create_meeting(meeting["id"])

        with self.assertRaises(HTTPException) as missing:
            main_module.patch_meeting_segments_batch(
                meeting["id"],
                main_module.SegmentBatchPatchRequest(
                    expectedTranscriptRevision=current["transcriptRevision"],
                    updates=[
                        {"segmentId": "valid-1", "text": "partial write"},
                        {"segmentId": "missing-2", "text": "missing"},
                    ],
                ),
            )

        self.assertEqual(missing.exception.status_code, 404)
        refreshed = self.store.get_or_create_meeting(meeting["id"])
        self.assertEqual(refreshed["segments"][0]["text"], "unchanged")
        self.assertEqual(refreshed["transcriptRevision"], current["transcriptRevision"])

    def test_sensitive_policy_revision_changes_only_derived_policy(self):
        """显式更新既有会议禁忌词快照时，原始 segment 和逐字稿 revision 均保持不变。"""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="policy revision"))
        self.store.add_realtime_segment(meeting["id"], {"id": "policy-source", "text": "raw confidential text"})
        rule = main_module.create_sensitive_rule(
            main_module.SensitiveRuleRequest(word="confidential", displayMode="stars", applyScope="display,ai,export")
        )
        before = self.store.get_or_create_meeting(meeting["id"])

        result = main_module.revise_meeting_sensitive_policy(
            meeting["id"], main_module.SensitivePolicyRevisionRequest(ruleIds=[rule["id"]])
        )
        after = self.store.get_or_create_meeting(meeting["id"])

        self.assertFalse(result["rawTranscriptChanged"])
        self.assertEqual(after["segments"], before["segments"])
        self.assertEqual(after["transcriptRevision"], before["transcriptRevision"])
        self.assertEqual(after["processingConfig"]["sensitiveRuleVersion"], result["sensitiveRuleVersion"])

    def test_failed_retranscription_preserves_current_transcript(self):
        """重新转写 ASR 失败只能写失败任务，不能切换或清空当前逐字稿。"""

        meeting = main_module._create_meeting_with_frozen_recognition_policy(
            main_module.MeetingCreateRequest(meetingName="safe retranscription", audioSource="上传文件"),
            mode="import",
        )
        self.store.add_transcript(meeting["id"], "file-retry", [{"id": "old-segment", "text": "keep current text"}])
        audio_path = Path(self.temp_dir.name) / "retry.wav"
        audio_path.write_bytes(b"RIFF")
        file_record = {"id": "file-retry", "meetingId": meeting["id"], "path": str(audio_path), "filename": "retry.wav"}
        self.store._save("files", file_record)
        current = self.store.get_or_create_meeting(meeting["id"])
        current["files"] = [file_record]
        current["status"] = "completed"
        current["processStatus"] = "completed"
        self.store._save("meetings", current)
        before = self.store.get_or_create_meeting(meeting["id"])

        with patch.object(main_module.asr_gateway, "transcribe_offline", side_effect=RuntimeError("provider unavailable")):
            result = main_module.create_retranscription(meeting["id"], main_module.RetranscriptionRequest())

        after = self.store.get_or_create_meeting(meeting["id"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["currentTranscriptPreserved"])
        self.assertEqual(after["segments"], before["segments"])
        self.assertEqual(after["transcriptRevision"], before["transcriptRevision"])
        self.assertEqual(after.get("transcriptVersions", []), [])


if __name__ == "__main__":
    unittest.main()
