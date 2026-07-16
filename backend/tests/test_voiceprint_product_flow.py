"""TDD coverage for truthful voiceprint readiness and scoped speaker correction.

The routes use the same persistent-store APIs as production, but every test replaces the singleton
with a temporary SQLite database.  This makes the correction assertions safe for both realtime and
import records without changing a developer's durable meetings or voiceprint library.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import UploadFile


# Configure the module import before ``app.main`` creates its singleton store.  Individual tests
# still replace that store, but this prevents the import itself from touching workspace data.
_MODULE_DATA_DIR = TemporaryDirectory()
os.environ["DATA_DIR"] = _MODULE_DATA_DIR.name
os.environ["DATABASE_URL"] = str(Path(_MODULE_DATA_DIR.name) / "voiceprint-product-module.db")

from app import main
from app.store import PersistentStore


class VoiceprintProductFlowTest(unittest.TestCase):
    """Exercise capability truth, match eligibility, and correction failure isolation."""

    def setUp(self) -> None:
        """Bind routes and uploaded sample output to private paths for each scenario."""

        self.temp_dir = TemporaryDirectory()
        self.store = PersistentStore(Path(self.temp_dir.name) / "voiceprint-product.db", seed_defaults=False)
        self.audio_dir = Path(self.temp_dir.name) / "audio-clips"
        self.audio_dir.mkdir()
        self.store_patcher = patch.object(main, "store", self.store)
        self.audio_path_patcher = patch.object(main, "AUDIO_CLIP_DIR", self.audio_dir)
        self.store_patcher.start()
        self.audio_path_patcher.start()
        # Meeting creation keeps the historic template response fields alive, so the isolated
        # store needs one minimal template even though these tests do not exercise Task 6.
        self.store.create_config_item("templates", "tpl", {"name": "voiceprint test template"})

    def tearDown(self) -> None:
        """Restore process-wide route dependencies before deleting temporary files."""

        self.audio_path_patcher.stop()
        self.store_patcher.stop()
        self.temp_dir.cleanup()

    def _meeting(self, mode: str) -> dict[str, object]:
        """Create one mode-specific meeting with two persisted segments for a rename."""

        meeting = main._create_meeting_with_frozen_recognition_policy(
            main.MeetingCreateRequest(meetingName=f"{mode} correction"), mode=mode
        )
        meeting["segments"] = [
            {"id": f"{mode}-1", "speakerName": "Speaker 1", "text": "first"},
            {"id": f"{mode}-2", "speakerName": "Speaker 1", "text": "second"},
        ]
        return self.store._save("meetings", meeting)

    def test_status_reports_each_configured_capability_as_unavailable_when_health_probe_fails(self) -> None:
        """A configured URL is not readiness; every capability must expose the failed probe truth."""

        with patch.object(main, "VAD_GATEWAY_BASE_URL", "http://vad.invalid"), patch.object(
            main, "VOICEPRINT_GATEWAY_BASE_URL", "http://voiceprint.invalid"
        ), patch.object(main, "ALIGNMENT_GATEWAY_BASE_URL", "http://alignment.invalid"), patch.object(
            main, "_probe_local_model_health", side_effect=main.LocalModelServiceError("connection refused")
        ):
            result = main.get_model_services_status()

        self.assertEqual(set(result), {"vad", "voiceprint", "alignment"})
        for capability in result.values():
            self.assertFalse(capability["ready"])
            self.assertEqual(capability["mode"], "unavailable")
            self.assertIn("connection refused", capability["message"])
            self.assertTrue(capability["checkedAt"])

    def test_mock_health_is_not_a_genuine_voiceprint_registration_runtime(self) -> None:
        """Mock endpoints remain visible for diagnostics but cannot be advertised as real readiness."""

        with patch.object(main, "VOICEPRINT_GATEWAY_BASE_URL", "http://models.local"), patch.object(
            main,
            "_probe_local_model_health",
            return_value={"status": "ok", "service": "intelligent-meeting-local-model-service", "mockMode": True, "models": {}},
        ):
            status = main.get_model_services_status()["voiceprint"]

        self.assertFalse(status["ready"])
        self.assertEqual(status["mode"], "mock")
        self.assertIn("mock", status["message"].lower())

    def test_status_rejects_a_configured_service_without_identity_or_capability_probe(self) -> None:
        """The backend must not convert a bare status=ok response into readiness."""

        with patch.object(main, "VOICEPRINT_GATEWAY_BASE_URL", "http://models.local"), patch.object(
            main, "_probe_local_model_health", return_value={"status": "ok", "mockMode": False, "models": {"voiceprint": "CAM++"}}
        ):
            status = main.get_model_services_status()["voiceprint"]

        self.assertFalse(status["ready"])
        self.assertEqual(status["mode"], "unavailable")
        self.assertIn("identity", status["message"].lower())

    def test_voiceprint_status_rejects_stale_service_without_embedding_route(self) -> None:
        """旧 8100 即使声称 CAM++ ready，只要没有 embedding 路由也不能健康假阳性。"""

        health = {
            "status": "ok",
            "service": "intelligent-meeting-local-model-service",
            "mockMode": False,
            "capabilities": {"voiceprint": {"ready": True, "mode": "real"}},
        }
        with patch.object(main, "VOICEPRINT_GATEWAY_BASE_URL", "http://models.local"), patch.object(
            main, "_probe_local_model_health", return_value=health
        ), patch.object(main, "_probe_local_model_routes", return_value={"/v1/voiceprints/register"}):
            status = main.get_model_services_status()["voiceprint"]

        self.assertFalse(status["ready"])
        self.assertEqual(status["mode"], "unavailable")
        self.assertIn("/v1/speakers/embedding", status["message"])

    def test_voiceprint_status_accepts_real_health_with_embedding_route(self) -> None:
        """模型加载与 embedding 路由契约同时满足时，声纹能力才可标记为 ready。"""

        health = {
            "status": "ok",
            "service": "intelligent-meeting-local-model-service",
            "mockMode": False,
            "capabilities": {"voiceprint": {"ready": True, "mode": "real"}},
        }
        with patch.object(main, "VOICEPRINT_GATEWAY_BASE_URL", "http://models.local"), patch.object(
            main, "_probe_local_model_health", return_value=health
        ), patch.object(main, "_probe_local_model_routes", return_value={"/v1/speakers/embedding"}):
            status = main.get_model_services_status()["voiceprint"]

        self.assertTrue(status["ready"])
        self.assertEqual(status["mode"], "real")

    def test_metadata_and_sample_stay_pending_when_voiceprint_runtime_is_unavailable(self) -> None:
        """Neither a positive sample count nor mock mode may manufacture a registered identity."""

        with patch.object(main, "_voiceprint_runtime_status", return_value={"ready": False, "message": "runtime unavailable"}):
            created = main.create_voiceprint(main.VoiceprintRequest(name="Ada", samples=3))
            upload = UploadFile(filename="ada.wav", file=io.BytesIO(b"sample"))
            result = self._run(main.upload_voiceprint_sample(created["id"], upload))

        self.assertEqual(created["registerStatus"], "pending_sample")
        self.assertEqual(result["status"], "waiting_model_config")
        self.assertEqual(result["voiceprint"]["registerStatus"], "waiting_model_config")
        self.assertNotIn("mock_registered", result["voiceprint"].get("modelStatus", ""))

    def test_match_uses_only_registered_profiles_with_real_embedding_ids(self) -> None:
        """A matching service result cannot revive pending, disabled, or embedding-less personnel records."""

        self.store.save_voiceprint({"id": "pending", "name": "Pending", "registerStatus": "pending_sample", "embeddingId": "emb-p"})
        self.store.save_voiceprint({"id": "missing", "name": "Missing", "registerStatus": "registered"})
        self.store.save_voiceprint({"id": "disabled", "name": "Disabled", "registerStatus": "registered", "embeddingId": "emb-d", "enabled": False})
        self.store.save_voiceprint({"id": "eligible", "name": "Eligible", "registerStatus": "registered", "embeddingId": "emb-ok", "enabled": True})

        match = main._first_voiceprint_match(
            {"matches": [{"speakerId": "pending"}, {"speakerId": "missing"}, {"speakerId": "disabled"}, {"speakerId": "eligible"}]}
        )

        self.assertEqual(main._active_voiceprint_ids(), {"eligible"})
        self.assertEqual(match["voiceprintId"], "eligible")

    def test_sample_upload_rejects_a_fallback_response_even_if_it_claims_registered(self) -> None:
        """A gateway fallback must never promote a business profile to match-eligible registration."""

        class FallbackGateway:
            def __init__(self, base_url: str) -> None:
                self.base_url = base_url

            def register(self, **kwargs):
                return {
                    "status": "registered",
                    "embeddingId": "fallback-embedding",
                    "model": "CAM++ + audio_fingerprint_fallback",
                    "realModel": False,
                    "fallbackReason": "weights missing",
                }

        created = main.create_voiceprint(main.VoiceprintRequest(name="Ada"))
        upload = UploadFile(filename="ada.wav", file=io.BytesIO(b"sample"))
        with patch.object(main, "_voiceprint_runtime_status", return_value={"ready": True}), patch.object(
            main, "VOICEPRINT_GATEWAY_BASE_URL", "http://models.local"
        ), patch.object(main, "LocalVoiceprintClient", FallbackGateway):
            result = self._run(main.upload_voiceprint_sample(created["id"], upload))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["voiceprint"]["registerStatus"], "failed")
        self.assertFalse(result["voiceprint"].get("embeddingId"))
        self.assertFalse(result["sample"].get("embeddingId"))
        self.assertTrue(
            all(not sample.get("embeddingId") for sample in result["voiceprint"].get("sampleFiles", [])),
            "Rejected gateway IDs must not survive in any persisted sample record.",
        )

    def test_sample_upload_accepts_the_successful_real_model_response_contract(self) -> None:
        """Only the explicit real-model marker can complete a persisted enrollment."""

        class RealGateway:
            def __init__(self, base_url: str) -> None:
                self.base_url = base_url

            def register(self, **kwargs):
                return {
                    "status": "registered",
                    "embeddingId": "real-embedding",
                    "model": "iic/speech_campplus_sv_zh-cn_16k-common",
                    "realModel": True,
                    "fallbackReason": "",
                }

        created = main.create_voiceprint(main.VoiceprintRequest(name="Ada"))
        upload = UploadFile(filename="ada.wav", file=io.BytesIO(b"sample"))
        with patch.object(main, "_voiceprint_runtime_status", return_value={"ready": True}), patch.object(
            main, "VOICEPRINT_GATEWAY_BASE_URL", "http://models.local"
        ), patch.object(main, "LocalVoiceprintClient", RealGateway):
            result = self._run(main.upload_voiceprint_sample(created["id"], upload))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["voiceprint"]["registerStatus"], "registered")
        self.assertEqual(result["voiceprint"]["embeddingId"], "real-embedding")
        self.assertTrue(result["voiceprint"]["realModel"])

    def test_meeting_only_correction_updates_only_selected_meeting_and_bumps_once(self) -> None:
        """Meeting-only correction changes all matching segments without creating library metadata."""

        selected = self._meeting("realtime")
        other = self._meeting("import")

        result = main.correct_meeting_speaker(
            str(selected["id"]),
            main.SpeakerCorrectionRequest(oldName="Speaker 1", name="Ada", syncMode="meeting_only"),
        )

        persisted_selected = self.store.get_or_create_meeting(str(selected["id"]))
        persisted_other = self.store.get_or_create_meeting(str(other["id"]))
        self.assertEqual([segment["speakerName"] for segment in persisted_selected["segments"]], ["Ada", "Ada"])
        self.assertEqual([segment["speakerName"] for segment in persisted_other["segments"]], ["Speaker 1", "Speaker 1"])
        self.assertEqual(persisted_selected["transcriptRevision"], 1)
        self.assertEqual(result["syncMode"], "meeting_only")
        self.assertEqual(list(self.store.voiceprints.values()), [])

    def test_sync_correction_reuses_department_group_and_upserts_pending_person_after_import_rename(self) -> None:
        """Sync mode preserves the same one-time rename while deterministically reusing a department group."""

        meeting = self._meeting("import")
        group = self.store.create_config_item("voiceprint_groups", "vg", {"name": "Engineering", "description": "existing"})

        with patch.object(main, "_voiceprint_runtime_status", return_value={"ready": False, "message": "runtime unavailable"}):
            result = main.correct_meeting_speaker(
                str(meeting["id"]),
                main.SpeakerCorrectionRequest(oldName="Speaker 1", name="Ada", department="Engineering", syncMode="sync_voiceprint"),
            )

        persisted = self.store.get_or_create_meeting(str(meeting["id"]))
        profiles = list(self.store.voiceprints.values())
        self.assertEqual(persisted["transcriptRevision"], 1)
        self.assertTrue(all(segment["speakerName"] == "Ada" for segment in persisted["segments"]))
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["groupId"], group["id"])
        self.assertEqual(profiles[0]["registerStatus"], "pending_sample")
        self.assertEqual(result["voiceprintSync"]["status"], "warning")
        self.assertIn("runtime unavailable", result["warning"])

    def test_sync_correction_creates_a_new_department_group(self) -> None:
        """An explicit library sync creates the requested department group exactly once."""

        meeting = self._meeting("import")
        with patch.object(main, "_voiceprint_runtime_status", return_value={"ready": False, "message": "runtime unavailable"}):
            main.correct_meeting_speaker(
                str(meeting["id"]),
                main.SpeakerCorrectionRequest(oldName="Speaker 1", name="Ada", department="Research", syncMode="sync_voiceprint"),
            )

        groups = [group for group in self.store.voiceprint_groups.values() if group.get("name") == "Research"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(next(iter(self.store.voiceprints.values()))["groupId"], groups[0]["id"])

    def test_sync_voiceprint_person_upserts_duplicate_name_instead_of_creating_a_second_profile(self) -> None:
        """Repeated correction syncs update one person record while preserving the pending-sample truth."""

        with patch.object(main, "_voiceprint_runtime_status", return_value={"ready": False, "message": "runtime unavailable"}):
            first = main._sync_voiceprint_person("Ada", "Research")
            second = main._sync_voiceprint_person("Ada", "Operations")

        profiles = list(self.store.voiceprints.values())
        self.assertEqual(first["status"], "warning")
        self.assertEqual(second["status"], "warning")
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["department"], "Operations")
        self.assertEqual(profiles[0]["registerStatus"], "pending_sample")

    def test_sync_failure_returns_warning_without_rolling_back_realtime_rename(self) -> None:
        """Voiceprint metadata errors are secondary after the durable meeting correction succeeds."""

        meeting = self._meeting("realtime")
        with patch.object(main, "_sync_voiceprint_person", side_effect=RuntimeError("group database unavailable")):
            result = main.correct_meeting_speaker(
                str(meeting["id"]),
                main.SpeakerCorrectionRequest(oldName="Speaker 1", name="Ada", syncMode="sync_voiceprint"),
            )

        persisted = self.store.get_or_create_meeting(str(meeting["id"]))
        self.assertTrue(all(segment["speakerName"] == "Ada" for segment in persisted["segments"]))
        self.assertEqual(persisted["transcriptRevision"], 1)
        self.assertEqual(result["voiceprintSync"]["status"], "warning")
        self.assertIn("group database unavailable", result["warning"])

    def test_frontend_uses_server_scoped_correction_and_exposes_runtime_truth(self) -> None:
        """The browser must not recreate client-side sync or hide unavailable enrollment states."""

        script = (Path(__file__).resolve().parents[2] / "frontend" / "app.js").read_text(encoding="utf-8")

        self.assertIn("/api/model-services/status", script)
        self.assertIn("speakerRenameSyncMode", script)
        self.assertIn("meeting_only", script)
        self.assertIn("sync_voiceprint", script)
        self.assertIn("/speaker-correction", script)
        self.assertIn("sampleInput.disabled = !runtime.ready", script)
        self.assertEqual(script.count("function openSpeakerRenameDialog"), 1)
        self.assertEqual(script.count("function renameSpeakerAcrossSegments"), 1)
        self.assertNotIn("upsertVoiceprintFromSpeakerRename", script)

    @staticmethod
    def _run(awaitable):
        """Run the one async upload route without adding an HTTP test-server dependency."""

        import asyncio

        return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
