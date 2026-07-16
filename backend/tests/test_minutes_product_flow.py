"""Product-flow tests for meeting-bound minutes templates and immutable history."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException

# Configure the import-time singleton before importing production modules.  The application owns
# a process-wide store, so this prevents the test module itself from ever opening developer data.
_MODULE_DATA_DIR = TemporaryDirectory()
os.environ["DATA_DIR"] = _MODULE_DATA_DIR.name
os.environ["DATABASE_URL"] = str(Path(_MODULE_DATA_DIR.name) / "module-store.db")

import app.main as main_module
from app.store import PersistentStore
from app.minutes_service import generate_minutes_version


class MinutesProductFlowTest(unittest.TestCase):
    """Exercise the public minutes routes against an isolated durable store."""

    def setUp(self) -> None:
        """Create two templates whose IDs, snapshots, and tags are easy to distinguish."""

        self.temp_dir = TemporaryDirectory()
        self.store = PersistentStore(Path(self.temp_dir.name) / "minutes-product-flow.db", seed_defaults=False)
        self.store.create_config_item(
            "templates",
            "tpl-project",
            {
                # The store uses the second argument as a prefix, so the durable ID under test
                # must be explicit instead of receiving a generated suffix.
                "id": "tpl-project",
                "name": "Project review",
                "sections": ["Decision", "Actions"],
                "tagBindings": [{"tag": "decision", "source": "summary.keyPoints"}],
            },
        )
        self.store.create_config_item(
            "templates",
            "tpl-qa",
            {
                # Keep the explicit ID distinct from the display name to prove ID-based lookup.
                "id": "tpl-qa",
                "name": "Quality review",
                "sections": ["Risk", "Verification"],
                "tagBindings": [{"tag": "risk", "source": "summary.riskFlags"}],
            },
        )
        self.store_patcher = patch.object(main_module, "store", self.store)
        self.store_patcher.start()
        self.addCleanup(self.store_patcher.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def _meeting_with_transcript(self, template_id: str = "tpl-project") -> dict:
        """Create a meeting with canonical source IDs required by the provenance assertions."""

        meeting = main_module.create_meeting(
            main_module.MeetingCreateRequest(meetingName="Minutes history", templateId=template_id)
        )
        self.store.add_realtime_segment(
            meeting["id"],
            {"id": "minutes-source", "text": "Avery confirmed the launch decision.", "startMs": 20, "endMs": 80},
        )
        return meeting

    def test_default_generation_uses_the_meeting_bound_template_snapshot_by_id(self) -> None:
        """An omitted request ID must use the meeting binding, not a mutable global default."""

        meeting = self._meeting_with_transcript()
        # Atomic transcript writers intentionally do not mutate the caller's stale meeting object.
        # Reload the durable revision before constructing the simulated model result.
        meeting = self.store.get_or_create_meeting(meeting["id"])
        frozen_snapshot = meeting["processingConfig"]["templateSnapshot"]
        self.store.update_config_item("templates", "tpl-project", {"name": "Changed after meeting start"})

        with patch.object(
            main_module,
            "generate_minutes_with_workflow",
            return_value={"templateName": frozen_snapshot["name"], "content": "Bound template minutes."},
        ):
            response = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())

        versions = main_module.list_minutes_versions(meeting["id"])["items"]
        self.assertEqual(response["templateId"], "tpl-project")
        self.assertEqual(response["templateSnapshot"], frozen_snapshot)
        self.assertEqual(versions[0]["templateId"], "tpl-project")
        self.assertEqual(versions[0]["templateSnapshot"], frozen_snapshot)
        self.assertEqual(versions[0]["sourceSegmentIds"], ["minutes-source"])
        self.assertEqual(versions[0]["sourceRanges"], [{"segmentId": "minutes-source", "startMs": 20, "endMs": 80}])

    def test_switching_template_creates_an_immutable_version_and_preserves_old_edited_layer(self) -> None:
        """Generating with a new ID appends history instead of overwriting a person's first edit."""

        meeting = self._meeting_with_transcript()
        with patch.object(
            main_module,
            "generate_minutes_with_workflow",
            side_effect=[
                {"templateName": "Project review", "content": "Project version."},
                {"templateName": "Quality review", "content": "Quality version."},
            ],
        ):
            first = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())
            main_module.save_minutes_draft(
                meeting["id"],
                main_module.MinutesDraftRequest(versionId=first["versionId"], content="Human notes for project version."),
            )
            second = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest(templateId="tpl-qa"))

        persisted = self.store.get_or_create_meeting(meeting["id"])
        versions = main_module.list_minutes_versions(meeting["id"])["items"]
        self.assertEqual([item["templateId"] for item in versions], ["tpl-project", "tpl-qa"])
        self.assertNotEqual(first["versionId"], second["versionId"])
        self.assertEqual(versions[0]["editedContent"], "Human notes for project version.")
        self.assertEqual(versions[0]["generatedContent"]["content"], "Project version.")
        self.assertEqual(persisted["minutesCurrentVersionId"], second["versionId"])
        self.assertEqual(persisted["minutesArtifact"]["versionId"], second["versionId"])
        self.assertEqual(persisted["minutes"], second["generatedContent"])

    def test_transcript_edit_marks_old_versions_stale_and_regeneration_moves_current_pointer(self) -> None:
        """Stale history remains selectable; only a later generation may become the current pointer."""

        meeting = self._meeting_with_transcript()
        with patch.object(
            main_module,
            "generate_minutes_with_workflow",
            side_effect=[
                {"templateName": "Project review", "content": "Before correction."},
                {"templateName": "Project review", "content": "After correction."},
            ],
        ):
            old_version = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())
            main_module.save_minutes_draft(
                meeting["id"],
                main_module.MinutesDraftRequest(versionId=old_version["versionId"], content="Keep this human correction."),
            )
            main_module.patch_meeting_segment(
                meeting["id"], "minutes-source", main_module.SegmentPatchRequest(text="Avery corrected the launch decision."),
            )
            new_version = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())

        versions = main_module.list_minutes_versions(meeting["id"])["items"]
        persisted = self.store.get_or_create_meeting(meeting["id"])
        self.assertEqual(versions[0]["status"], "stale")
        self.assertEqual(versions[0]["editedContent"], "Keep this human correction.")
        self.assertLess(versions[0]["sourceTranscriptRevision"], persisted["transcriptRevision"])
        self.assertEqual(versions[1]["status"], "current")
        self.assertEqual(versions[1]["sourceTranscriptRevision"], persisted["transcriptRevision"])
        self.assertEqual(persisted["minutesCurrentVersionId"], new_version["versionId"])
        self.assertEqual(persisted["minutesArtifact"]["versionId"], new_version["versionId"])

    def test_missing_or_unknown_template_ids_are_rejected_without_creating_history(self) -> None:
        """A supplied ID is an explicit selection, so it cannot fall back to another template."""

        meeting = self._meeting_with_transcript()
        for template_id in ("", "tpl-missing"):
            with self.assertRaises(HTTPException) as error:
                main_module.generate_minutes(meeting["id"], main_module.MinutesRequest(templateId=template_id))
            self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(main_module.list_minutes_versions(meeting["id"])["items"], [])

    def test_every_version_persists_the_frozen_sensitive_ai_audit(self) -> None:
        """Template strings and transcript text share the meeting's frozen AI-policy evidence."""

        self.store.create_config_item(
            "sensitive_rules",
            "rule-confidential",
            {
                # As above, persist the named rule ID rather than a generated prefix-based ID.
                "id": "rule-confidential",
                "word": "confidential",
                "enabled": True,
                "applyScope": "ai",
                "displayMode": "stars",
            },
        )
        meeting = self._meeting_with_transcript()
        self.store.update_config_item("templates", "tpl-project", {"name": "Changed mutable confidential template"})

        with patch.object(
            main_module,
            "generate_minutes_with_workflow",
            return_value={"templateName": "masked", "content": "Generated with masked inputs."},
        ):
            response = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())

        self.assertIn("sensitivePolicy", response)
        self.assertEqual(response["sensitivePolicy"]["target"], "ai")
        self.assertEqual(response["templateSnapshot"]["name"], "Project review")
        self.assertEqual(self.store.get_or_create_meeting(meeting["id"])["minutesVersions"][0]["sensitivePolicy"], response["sensitivePolicy"])

    def test_generation_that_loses_the_transcript_revision_race_is_preserved_as_stale_only(self) -> None:
        """A late model result may remain auditable, but cannot replace the last compatible minutes.

        The workflow stub mutates the durable transcript *while* the second generation is in flight.
        This is the real interleaving that a save-boundary revision check must handle: request-time
        checks are not sufficient because the model call can take materially longer than a transcript
        edit, a realtime final, or an import completion.
        """

        meeting = self._meeting_with_transcript()
        with patch.object(
            main_module,
            "generate_minutes_with_workflow",
            return_value={"templateName": "Project review", "content": "Compatible current version."},
        ):
            first = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())

        def generate_after_transcript_edit(*_args: object, **_kwargs: object) -> dict:
            main_module.patch_meeting_segment(
                meeting["id"],
                "minutes-source",
                main_module.SegmentPatchRequest(text="Transcript advanced during generation."),
            )
            return {"templateName": "Quality review", "content": "Late obsolete version."}

        with patch.object(main_module, "generate_minutes_with_workflow", side_effect=generate_after_transcript_edit):
            late = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest(templateId="tpl-qa"))

        persisted = self.store.get_or_create_meeting(meeting["id"])
        versions = main_module.list_minutes_versions(meeting["id"])["items"]
        self.assertEqual(late["status"], "stale")
        self.assertEqual(versions[-1]["generatedContent"]["content"], "Late obsolete version.")
        self.assertEqual(versions[-1]["status"], "stale")
        self.assertNotEqual(versions[-1]["versionId"], persisted["minutesCurrentVersionId"])
        self.assertEqual(persisted["minutesCurrentVersionId"], first["versionId"])
        self.assertEqual(persisted["minutesArtifact"]["versionId"], first["versionId"])
        self.assertEqual(persisted["minutes"]["content"], "Compatible current version.")

    def test_same_revision_regeneration_has_exactly_one_current_version(self) -> None:
        """Template switching preserves both records but explicitly supersedes the old pointer target."""

        meeting = self._meeting_with_transcript()
        with patch.object(
            main_module,
            "generate_minutes_with_workflow",
            side_effect=[
                {"templateName": "Project review", "content": "First current version."},
                {"templateName": "Quality review", "content": "Second current version."},
            ],
        ):
            first = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())
            second = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest(templateId="tpl-qa"))

        versions = main_module.list_minutes_versions(meeting["id"])["items"]
        status_by_id = {version["versionId"]: version["status"] for version in versions}
        self.assertEqual(status_by_id[first["versionId"]], "superseded")
        self.assertEqual(status_by_id[second["versionId"]], "current")
        self.assertEqual(sum(version["status"] == "current" for version in versions), 1)

    def test_editing_a_non_current_version_preserves_the_current_legacy_payload(self) -> None:
        """Historical corrections are durable records, never an implicit rollback of the current pointer."""

        meeting = self._meeting_with_transcript()
        with patch.object(
            main_module,
            "generate_minutes_with_workflow",
            side_effect=[
                {"templateName": "Project review", "content": "Historical generated text."},
                {"templateName": "Quality review", "content": "Current generated text."},
            ],
        ):
            first = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())
            second = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest(templateId="tpl-qa"))

        edited = main_module.save_minutes_draft(
            meeting["id"],
            main_module.MinutesDraftRequest(versionId=first["versionId"], content="Historical human correction."),
        )
        persisted = self.store.get_or_create_meeting(meeting["id"])
        self.assertEqual(edited["versionId"], first["versionId"])
        self.assertEqual(edited["editedContent"], "Historical human correction.")
        self.assertEqual(persisted["minutesCurrentVersionId"], second["versionId"])
        self.assertEqual(persisted["minutesArtifact"]["versionId"], second["versionId"])
        self.assertEqual(persisted["minutes"]["content"], "Current generated text.")

    def test_no_transcript_never_creates_a_minutes_version(self) -> None:
        """The compatibility empty-state response must not fabricate source-less version history."""

        meeting = main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="Empty minutes", templateId="tpl-project"))
        response = main_module.generate_minutes(meeting["id"], main_module.MinutesRequest())
        self.assertFalse(response.get("versionId"))
        self.assertEqual(main_module.list_minutes_versions(meeting["id"])["items"], [])

    def test_realtime_and_import_transcript_histories_each_generate_versioned_minutes(self) -> None:
        """Each approved transcript ownership path provides canonical provenance to minutes history."""

        realtime = self._meeting_with_transcript()
        imported = main_module._create_meeting_with_frozen_recognition_policy(
            main_module.MeetingCreateRequest(meetingName="Imported minutes", templateId="tpl-project"),
            mode="import",
        )
        self.store.add_transcript(
            imported["id"],
            "import-file",
            [{"id": "import-source", "text": "Imported decision text.", "startMs": 10, "endMs": 70}],
        )
        with patch.object(
            main_module,
            "generate_minutes_with_workflow",
            return_value={"templateName": "Project review", "content": "Versioned minutes."},
        ):
            realtime_version = main_module.generate_minutes(realtime["id"], main_module.MinutesRequest())
            import_version = main_module.generate_minutes(imported["id"], main_module.MinutesRequest())

        self.assertEqual(realtime_version["sourceSegmentIds"], ["minutes-source"])
        self.assertEqual(import_version["sourceSegmentIds"], ["import-source"])
        self.assertEqual(main_module.list_minutes_versions(realtime["id"])["items"][0]["status"], "current")
        self.assertEqual(main_module.list_minutes_versions(imported["id"])["items"][0]["status"], "current")

    def test_invalid_creation_template_id_is_rejected_before_a_meeting_is_saved(self) -> None:
        """Creation cannot persist a broken default binding that later conflicts with legacy minutes."""

        with self.assertRaises(HTTPException) as error:
            main_module.create_meeting(main_module.MeetingCreateRequest(meetingName="Invalid binding", templateId="tpl-missing"))
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(self.store.list_meetings(), [])

    def test_concurrent_final_segment_and_minutes_save_never_lose_transcript_or_publish_stale_current(self) -> None:
        """SQLite transaction ordering must protect both transcript truth and minutes status."""

        meeting = self._meeting_with_transcript()
        meeting = self.store.get_or_create_meeting(meeting["id"])
        version = generate_minutes_version(
            meeting,
            "tpl-project",
            meeting["transcriptRevision"],
            template_snapshot=self.store.templates["tpl-project"],
            generated_content={"content": "Concurrent minutes"},
            sensitive_policy={"target": "ai", "ruleVersion": "none", "hits": []},
        )
        barrier = threading.Barrier(3)
        errors: list[BaseException] = []

        def save_minutes() -> None:
            try:
                barrier.wait()
                self.store.save_minutes_version(meeting["id"], version)
            except BaseException as exc:  # noqa: BLE001 - preserve thread failures for the test thread.
                errors.append(exc)

        def save_transcript() -> None:
            try:
                barrier.wait()
                self.store.add_realtime_segment(
                    meeting["id"],
                    {"id": "concurrent-final", "text": "A final arrived during generation.", "isFinal": True},
                )
            except BaseException as exc:  # noqa: BLE001 - preserve thread failures for the test thread.
                errors.append(exc)

        threads = [threading.Thread(target=save_minutes), threading.Thread(target=save_transcript)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        refreshed = self.store.get_or_create_meeting(meeting["id"])
        self.assertIn("concurrent-final", [segment["id"] for segment in refreshed["segments"]])
        self.assertEqual(refreshed["transcriptRevision"], meeting["transcriptRevision"] + 1)
        saved_version = next(item for item in refreshed["minutesVersions"] if item["versionId"] == version["versionId"])
        self.assertEqual(saved_version["status"], "stale")
        self.assertFalse(any(item.get("status") == "current" for item in refreshed["minutesVersions"]))


if __name__ == "__main__":
    unittest.main()
