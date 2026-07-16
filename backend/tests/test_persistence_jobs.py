import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.store import PersistentStore


class PersistenceAndJobTest(unittest.TestCase):
    """验证系统不再只是内存展示，而是具备可持久化和任务状态能力。

    测试使用临时 SQLite 文件；正式部署到 KingbaseES 时，业务层仍使用同一套
    Store 方法，底层连接和 SQL 方言由后续适配器替换。
    """

    def test_meeting_and_config_survive_store_reopen(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "meeting-system.db"
            first_store = PersistentStore(db_path=db_path, seed_defaults=True)
            created = first_store.create_meeting("持久化联调会", keyword_library_ids=["kw-001"])
            first_store.create_config_item(
                "keyword_libraries",
                "kw",
                {"name": "重启保留词库", "words": ["KingbaseES"], "enabled": True, "scope": "数据库联调"},
            )

            reopened_store = PersistentStore(db_path=db_path, seed_defaults=False)
            reopened = reopened_store.get_or_create_meeting(created["id"])
            libraries = reopened_store.list_config_items("keyword_libraries")

            self.assertEqual(reopened["fileName"], "持久化联调会")
            self.assertTrue(any(item["name"] == "重启保留词库" for item in libraries))

    def test_job_lifecycle_is_queryable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "meeting-system.db"
            persistent_store = PersistentStore(db_path=db_path, seed_defaults=True)
            meeting = persistent_store.create_meeting("任务状态联调")

            job = persistent_store.create_job(
                meeting_id=meeting["id"],
                job_type="offline_transcribe",
                title="离线转写 demo.wav",
                steps=["uploaded", "transcoding", "asr", "voiceprint", "alignment", "minutes", "completed"],
            )
            persistent_store.update_job(job["id"], status="running", current_step="asr", progress=45)
            persistent_store.update_job(job["id"], status="completed", current_step="completed", progress=100)

            loaded = persistent_store.get_job(job["id"])
            meeting_jobs = persistent_store.list_jobs(meeting["id"])

            self.assertEqual(loaded["status"], "completed")
            self.assertEqual(loaded["currentStep"], "completed")
            self.assertEqual(loaded["progress"], 100)
            self.assertEqual(meeting_jobs[0]["id"], job["id"])

    def test_realtime_recording_and_whole_session_diarization_are_atomic(self):
        """实时录音不创建导入任务，整场发言人回写只增加一次逐字稿 revision。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            persistent_store = PersistentStore(db_path=root / "meeting-system.db", seed_defaults=True)
            meeting = persistent_store.create_meeting(
                "整场发言人整理测试",
                processing_config={"transcriptionMode": "realtime"},
            )
            for segment_id, text in (("seg-a", "第一段"), ("seg-b", "第二段")):
                persistent_store.add_realtime_segment(
                    meeting["id"],
                    {
                        "id": segment_id,
                        "text": text,
                        "speakerName": "发言人1",
                        "speakerClusterId": f"pending-{segment_id}",
                        "speakerSource": "dashscope_realtime",
                        "realtimeSessionToken": "rt-test",
                    },
                )
            recording_path = root / "recording.wav"
            recording_path.write_bytes(b"RIFF-test")

            file_record = persistent_store.attach_realtime_recording(
                meeting["id"],
                "recording.wav",
                recording_path,
                duration_ms=4000,
                session_token="rt-test",
            )
            before_revision = persistent_store.get_or_create_meeting(meeting["id"])["transcriptRevision"]
            affected = persistent_store.apply_realtime_diarization(
                meeting["id"],
                session_token="rt-test",
                speaker_patches={
                    "seg-a": {"speakerName": "发言人1", "speakerClusterId": "diarization-SPEAKER_00", "speakerSource": "diarization"},
                    "seg-b": {"speakerName": "发言人2", "speakerClusterId": "diarization-SPEAKER_01", "speakerSource": "diarization"},
                },
            )
            loaded = persistent_store.get_or_create_meeting(meeting["id"])

            self.assertEqual(file_record["kind"], "realtime_recording")
            self.assertEqual(persistent_store.list_jobs(meeting["id"]), [])
            self.assertEqual(len(affected), 2)
            self.assertEqual(loaded["transcriptRevision"], before_revision + 1)
            self.assertEqual([item["speakerName"] for item in loaded["segments"]], ["发言人1", "发言人2"])

    def test_delete_meeting_removes_owned_rows_and_only_its_recorded_files(self):
        """Deleting a meeting must leave neither database-owned rows nor uploaded bytes behind.

        The production store intentionally accepts only files below its configured upload/audio
        roots.  This test patches those roots to a private temporary directory so it verifies the
        real unlink boundary without touching developer data.  An unrelated meeting and file are
        created alongside the target to prove cleanup is scoped by ownership rather than by folder.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_dir = root / "uploads"
            audio_dir = root / "audio-clips"
            upload_dir.mkdir()
            audio_dir.mkdir()
            persistent_store = PersistentStore(root / "meeting-system.db", seed_defaults=False)
            persistent_store.create_config_item("templates", "cleanup-template", {"name": "cleanup template"})

            target = persistent_store.create_meeting("cleanup target")
            unrelated = persistent_store.create_meeting("cleanup survivor")
            target_media = upload_dir / "target.wav"
            embedded_only_media = upload_dir / "embedded-only.wav"
            unrelated_media = upload_dir / "unrelated.wav"
            target_media.write_bytes(b"target media")
            embedded_only_media.write_bytes(b"legacy embedded-only media")
            unrelated_media.write_bytes(b"unrelated media")

            target_file = persistent_store.save_file(target["id"], "target.wav", target_media, "audio/wav")
            persistent_store.save_file(unrelated["id"], "unrelated.wav", unrelated_media, "audio/wav")
            # These private saves model rows produced by the import/realtime pipeline.  Every row
            # carries the same meetingId ownership key used by the public query methods.
            persistent_store._save(
                "transcripts",
                {"id": "tr-cleanup-target", "meetingId": target["id"], "fileId": target_file["id"], "segments": []},
            )
            persistent_store._save(
                "highlights",
                {"id": "hl-cleanup-target", "meetingId": target["id"], "segmentId": "seg-1"},
            )
            # Historical versions could leave the meeting's compatibility copy ahead of the files
            # table. Keep such an embedded-only path in scope without manufacturing a files row.
            target_snapshot = persistent_store.get_meeting(target["id"])
            target_snapshot["files"].append({"id": "legacy-embedded", "path": str(embedded_only_media)})
            persistent_store._save("meetings", target_snapshot)

            with patch("app.store.UPLOAD_DIR", upload_dir), patch("app.store.AUDIO_CLIP_DIR", audio_dir):
                self.assertTrue(persistent_store.delete_meeting(target["id"]))

            self.assertIsNone(persistent_store._get("meetings", target["id"]))
            self.assertFalse(target_media.exists())
            self.assertFalse(embedded_only_media.exists())
            self.assertFalse(any(item.get("meetingId") == target["id"] for item in persistent_store._list("files")))
            self.assertFalse(any(item.get("meetingId") == target["id"] for item in persistent_store._list("jobs")))
            self.assertFalse(any(item.get("meetingId") == target["id"] for item in persistent_store._list("transcripts")))
            self.assertFalse(any(item.get("meetingId") == target["id"] for item in persistent_store._list("highlights")))
            self.assertIsNotNone(persistent_store._get("meetings", unrelated["id"]))
            self.assertTrue(unrelated_media.exists())


if __name__ == "__main__":
    unittest.main()
