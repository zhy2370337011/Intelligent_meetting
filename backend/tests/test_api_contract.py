import asyncio
import json
import math
import struct
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException, WebSocketDisconnect

from app.main import (
    ConfigPatchRequest,
    ExportRequest,
    KeywordLibraryRequest,
    MeetingCreateRequest,
    MinutesRequest,
    SensitiveRuleRequest,
    TemplateRequest,
    ToolDraftRequest,
    TranscribeRequest,
    VoiceprintRequest,
    add_highlight,
    app,
    apply_voiceprint_match_to_segments,
    create_keyword_library,
    create_meeting,
    create_sensitive_rule,
    create_template,
    create_voiceprint,
    delete_keyword_library,
    delete_sensitive_rule,
    delete_template,
    export_docx,
    export_audio,
    extract_todos,
    generate_minutes,
    generate_summary,
    get_dashboard_overview,
    get_meeting,
    get_workflow_status,
    list_keyword_libraries,
    list_meetings,
    list_templates,
    reorganize_meeting_discourse,
    realtime_meeting,
    save_ai_tool_draft,
    save_minutes_draft,
    transcribe_file,
    upload_voiceprint_sample,
    update_keyword_library,
    update_template,
    update_voiceprint,
)
from app.asr_gateway import DashScopeAsrGateway, MockQwenAsrGateway
import app.main as main_module
from app.store import PersistentStore


# Existing tests below reference ``store`` directly. Bind that module-level name to the same
# temporary store used by route functions in ``setUp`` so every call path stays isolated.
store = main_module.store


def _build_test_wav(amplitude: float, sample_rate: int = 16000, seconds: float = 1.4) -> bytes:
    """构造单声道 16-bit WAV，让 WebSocket 契约测试不依赖真实麦克风或外部音频文件。"""

    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = bytearray()
        for index in range(int(sample_rate * seconds)):
            sample = int(32767 * amplitude * math.sin(2 * math.pi * 440 * index / sample_rate))
            frames.extend(struct.pack("<h", sample))
        wav_file.writeframes(bytes(frames))
    return buffer.getvalue()


class ApiContractTest(unittest.TestCase):
    """验证后端返回结构是否能直接支撑当前前端原型。

    当前项目未安装 httpx，因此不使用 FastAPI TestClient，直接调用路由函数。
    这些测试依然覆盖 API 契约字段和业务行为；等后续引入 httpx 后可再补 HTTP 层测试。
    """

    def setUp(self):
        """Give each API-contract test a private database instead of resetting developer data.

        Route functions resolve ``app.main.store`` at call time, while older tests in this module
        also reference the imported ``store`` name directly. Patching both bindings makes the
        required compatibility test exercise real persistence without touching the application's
        durable development database.
        """

        self.temp_dir = TemporaryDirectory()
        self.store = PersistentStore(Path(self.temp_dir.name) / "api_contract.db")
        self.route_store_patcher = patch.object(main_module, "store", self.store)
        self.route_store_patcher.start()
        self.audio_clip_dir = Path(self.temp_dir.name) / "audio_clips"
        self.audio_clip_dir.mkdir(parents=True, exist_ok=True)
        # Realtime chunks and voiceprint uploads use a module-level path. Patch it alongside the
        # store so API-contract tests cannot leave WAV files in the developer's durable data root.
        self.audio_dir_patcher = patch.object(main_module, "AUDIO_CLIP_DIR", self.audio_clip_dir)
        self.audio_dir_patcher.start()
        self.previous_test_store = globals()["store"]
        globals()["store"] = self.store
        self.addCleanup(self._restore_isolated_store)

    def _restore_isolated_store(self):
        """Restore module bindings after each test before deleting its private database directory."""

        globals()["store"] = self.previous_test_store
        self.audio_dir_patcher.stop()
        self.route_store_patcher.stop()
        self.temp_dir.cleanup()

    def _create_import_meeting(self, request: MeetingCreateRequest) -> dict:
        """Create an import-owned record for tests that exercise the file transcription boundary.

        The public ``create_meeting`` route intentionally creates a realtime record.  These legacy
        contract tests verify offline ASR and must therefore state their import ownership instead
        of relying on the removed cross-mode fallback.
        """

        meeting = main_module._create_meeting_with_frozen_recognition_policy(request, mode="import")
        return meeting

    def test_realtime_speaker_identity_patch_updates_only_target_cluster_without_touching_text(self):
        """迟到的声纹结果只能升级同簇身份，不能修改正文或其它说话人的片段。"""

        meeting = create_meeting(MeetingCreateRequest(meetingName="speaker patch", enableDiarization=True))
        self.store.add_realtime_segment(meeting["id"], {"id": "seg-1", "text": "第一段", "speakerName": "发言人1", "speakerClusterId": "speaker-1", "realtimeSessionToken": "session-a"})
        self.store.add_realtime_segment(meeting["id"], {"id": "seg-2", "text": "第二段", "speakerName": "发言人1", "speakerClusterId": "speaker-1", "realtimeSessionToken": "session-a"})
        self.store.add_realtime_segment(meeting["id"], {"id": "seg-3", "text": "第三段", "speakerName": "发言人2", "speakerClusterId": "speaker-2", "realtimeSessionToken": "session-a"})
        self.store.add_realtime_segment(meeting["id"], {"id": "seg-4", "text": "新会话", "speakerName": "发言人1", "speakerClusterId": "speaker-1", "realtimeSessionToken": "session-b"})

        affected = self.store.update_realtime_speaker_identity(
            meeting["id"],
            segment_id="seg-2",
            cluster_id="speaker-1",
            session_token="session-a",
            patch={"speakerName": "王忠", "speakerTitle": "办公室", "voiceprintId": "vp-1", "text": "禁止覆盖"},
        )
        saved = self.store.get_or_create_meeting(meeting["id"])["segments"]

        self.assertEqual([item["id"] for item in affected], ["seg-1", "seg-2"])
        self.assertEqual([item["text"] for item in saved], ["第一段", "第二段", "第三段", "新会话"])
        self.assertEqual([item["speakerName"] for item in saved], ["王忠", "王忠", "发言人2", "发言人1"])

        revision_before = self.store.get_or_create_meeting(meeting["id"])["transcriptRevision"]
        self.assertEqual(
            self.store.update_realtime_speaker_identity(
                meeting["id"], segment_id="seg-2", cluster_id="speaker-1", session_token="session-a", patch={"text": "仍然禁止"}
            ),
            [],
        )
        self.assertEqual(self.store.get_or_create_meeting(meeting["id"])["transcriptRevision"], revision_before)
        with self.assertRaises(ValueError):
            self.store.update_realtime_speaker_identity(
                meeting["id"], segment_id="seg-2", cluster_id="speaker-1", session_token="", patch={"speakerName": "越界"}
            )

    def test_alignment_success_returns_camel_case_window(self):
        """真实对齐客户端成功时，主产品接口只暴露前端约定的 camelCase 时间窗。"""

        self.store.transcripts = {"tr-align-success": {"id": "tr-align-success", "fileId": "file-align"}}
        request = main_module.AlignRequest(
            transcriptText="智能会议系统",
            selectedText="会议",
            words=[],
            paddingMs=100,
        )

        class SuccessfulAlignmentClient:
            """模拟真实客户端的 snake_case 服务响应，不启动或伪造 ForcedAligner ready 状态。"""

            def __init__(self, base_url: str):
                self.base_url = base_url

            def selection_window(self, **kwargs):
                return {"start_ms": 800, "end_ms": 1600}

        with patch.object(main_module, "ALIGNMENT_GATEWAY_BASE_URL", "http://aligner.test"), patch.object(
            main_module, "LocalAlignmentClient", SuccessfulAlignmentClient
        ):
            result = main_module.align_transcript("tr-align-success", request)

        self.assertEqual(result, {"startMs": 800, "endMs": 1600})
        self.assertNotIn("start_ms", result)
        self.assertNotIn("end_ms", result)

    def test_alignment_service_failure_falls_back_to_camel_case_window(self):
        """真实服务失败后的本地时间戳降级也必须保持主产品 camelCase 契约。"""

        self.store.transcripts = {"tr-align-fallback": {"id": "tr-align-fallback", "fileId": "file-align"}}
        request = main_module.AlignRequest(
            transcriptText="智能会议系统",
            selectedText="会议",
            words=[
                {"text": "智能", "start_ms": 0, "end_ms": 800},
                {"text": "会议", "start_ms": 900, "end_ms": 1500},
                {"text": "系统", "start_ms": 1600, "end_ms": 2200},
            ],
            paddingMs=100,
        )

        class FailingAlignmentClient:
            """在客户端边界制造服务异常，验证路由走现有本地 words 降级路径。"""

            def __init__(self, base_url: str):
                self.base_url = base_url

            def selection_window(self, **kwargs):
                raise main_module.LocalModelServiceError("forced aligner unavailable")

        with patch.object(main_module, "ALIGNMENT_GATEWAY_BASE_URL", "http://aligner.test"), patch.object(
            main_module, "LocalAlignmentClient", FailingAlignmentClient
        ):
            result = main_module.align_transcript("tr-align-fallback", request)

        self.assertEqual(result, {"startMs": 800, "endMs": 1600})
        self.assertNotIn("start_ms", result)
        self.assertNotIn("end_ms", result)

    def test_alignment_response_missing_end_falls_back_to_local_words(self):
        """真实服务返回缺少结束时间的 200 载荷时，不得把缺失字段静默补成 0。"""

        self.store.transcripts = {"tr-align-missing-end": {"id": "tr-align-missing-end", "fileId": "file-align"}}
        request = main_module.AlignRequest(
            transcriptText="智能会议系统",
            selectedText="会议",
            words=[
                {"text": "智能", "start_ms": 0, "end_ms": 800},
                {"text": "会议", "start_ms": 900, "end_ms": 1500},
                {"text": "系统", "start_ms": 1600, "end_ms": 2200},
            ],
            paddingMs=100,
        )

        with patch.object(main_module, "ALIGNMENT_GATEWAY_BASE_URL", "http://aligner.test"), patch.object(
            main_module, "LocalAlignmentClient"
        ) as client_class:
            # HTTP 200 并不代表业务载荷有效。这里刻意漏掉 end_ms，验证主路由拒绝半截时间窗，
            # 并复用 req.words 计算出完整的本地降级结果，而不是向前端返回 endMs=0。
            client_class.return_value.selection_window.return_value = {"start_ms": 900}
            result = main_module.align_transcript("tr-align-missing-end", request)

        self.assertEqual(result, {"startMs": 800, "endMs": 1600})

    def test_alignment_response_with_non_numeric_time_falls_back_to_local_words(self):
        """真实服务返回不可转换时间字段时，应按服务错误处理并进入本地降级。"""

        self.store.transcripts = {"tr-align-invalid-time": {"id": "tr-align-invalid-time", "fileId": "file-align"}}
        request = main_module.AlignRequest(
            transcriptText="智能会议系统",
            selectedText="会议",
            words=[
                {"text": "智能", "start_ms": 0, "end_ms": 800},
                {"text": "会议", "start_ms": 900, "end_ms": 1500},
                {"text": "系统", "start_ms": 1600, "end_ms": 2200},
            ],
            paddingMs=100,
        )

        with patch.object(main_module, "ALIGNMENT_GATEWAY_BASE_URL", "http://aligner.test"), patch.object(
            main_module, "LocalAlignmentClient"
        ) as client_class:
            client_class.return_value.selection_window.return_value = {
                "start_ms": "not-a-millisecond-value",
                "end_ms": 1500,
            }
            try:
                result = main_module.align_transcript("tr-align-invalid-time", request)
            except (TypeError, ValueError) as exc:
                # 旧实现会让 int() 的转换异常直接越过服务降级边界。将它转换为测试失败，
                # 使红灯明确表达“主路由没有降级”，而不是让用例以未处理异常结束。
                self.fail(f"畸形对齐载荷应进入本地降级，不应向路由调用方抛出转换异常：{exc}")

        self.assertEqual(result, {"startMs": 800, "endMs": 1600})

    def test_meeting_list_returns_frontend_record_fields(self):
        payload = list_meetings()

        self.assertIn("items", payload)
        first = payload["items"][0]
        for key in [
            "id",
            "fileName",
            "keywords",
            "keywordLibraryIds",
            "keywordLibraryNames",
            "minutesStatus",
            "processStatus",
            "createdAt",
            "creator",
            "status",
            "segments",
            "summary",
            "minutes",
            "todos",
            "decisionItems",
            "riskFlags",
            "integrationStatus",
        ]:
            self.assertIn(key, first)
        self.assertRegex(first["createdAt"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertGreaterEqual(len(first["keywordLibraryNames"]), 1)

    def test_create_meeting_uses_keyword_libraries_and_template(self):
        meeting = create_meeting(
            MeetingCreateRequest(
                meetingName="后端联调会",
                language="中文普通话",
                translateDirection="无",
                audioSource="麦克风阵列",
                templateId="tpl-001",
                keywordLibraryIds=["kw-001", "kw-002"],
                enableDiarization=True,
            )
        )

        self.assertEqual(meeting["fileName"], "后端联调会")
        self.assertEqual(meeting["keywordLibraryIds"], ["kw-001", "kw-002"])
        self.assertEqual(meeting["keywordLibraryNames"], ["政务会议词库", "智能会议技术词库"])
        self.assertEqual(meeting["minutesStatus"], "ready")
        self.assertEqual(meeting["processStatus"], "processing")
        self.assertEqual(meeting["integrationStatus"]["todoPush"], "待推送")

    def test_deleted_meeting_detail_returns_404_without_recreating_the_record(self):
        """Public detail reads must never turn a successful delete into an implicit create."""

        meeting = create_meeting(MeetingCreateRequest(meetingName="strict delete detail"))
        self.assertTrue(store.delete_meeting(meeting["id"]))

        with self.assertRaises(HTTPException) as missing:
            get_meeting(meeting["id"])

        self.assertEqual(missing.exception.status_code, 404)
        self.assertIsNone(store._get("meetings", meeting["id"]))

    def test_audio_export_preserves_original_wav_bytes_filename_and_media_type(self):
        """Recorded-media playback must not relabel WAV bytes as an MP3 response."""

        meeting = create_meeting(MeetingCreateRequest(meetingName="playback content type"))
        wav_path = Path(self.temp_dir.name) / "playback.wav"
        wav_path.write_bytes(_build_test_wav(0.3, seconds=0.2))
        store.save_file(meeting["id"], "playback.wav", wav_path, "audio/wav")

        response = export_audio(meeting["id"])

        self.assertEqual(response.media_type, "audio/wav")
        self.assertTrue(response.body.startswith(b"RIFF"))
        self.assertIn("playback.wav", response.headers["content-disposition"])

    def test_deleted_meeting_rejects_late_patch_and_upload_without_leaving_rows_or_bytes(self):
        """A delayed public write cannot resurrect a meeting after deletion has committed."""

        from fastapi import UploadFile

        meeting = create_meeting(MeetingCreateRequest(meetingName="delete finality"))
        self.assertTrue(store.delete_meeting(meeting["id"]))
        private_upload_dir = Path(self.temp_dir.name) / "late-uploads"
        private_upload_dir.mkdir()

        with self.assertRaises(HTTPException) as patch_missing:
            main_module.update_meeting(meeting["id"], main_module.MeetingUpdateRequest(meetingName="resurrected"))
        with patch.object(main_module, "UPLOAD_DIR", private_upload_dir):
            with self.assertRaises(HTTPException) as upload_missing:
                asyncio.run(
                    main_module.upload_file(
                        meeting["id"],
                        UploadFile(filename="late.wav", file=BytesIO(_build_test_wav(0.3, seconds=0.1))),
                    )
                )

        self.assertEqual(patch_missing.exception.status_code, 404)
        self.assertEqual(upload_missing.exception.status_code, 404)
        self.assertIsNone(store._get("meetings", meeting["id"]))
        self.assertFalse(any(item.get("meetingId") == meeting["id"] for item in store._list("files")))
        self.assertEqual(list(private_upload_dir.iterdir()), [])

    def test_create_quick_meeting_preserves_explicit_empty_keyword_selection(self):
        """An empty quick-meeting selection must not silently enable unrelated default dictionaries.

        Realtime contextual biasing is strong enough to turn ordinary speech into configured government or
        technical terms. The frontend intentionally sends an empty list for a general quick meeting, so the
        persistence layer must distinguish that choice from ``None`` (legacy callers asking for defaults).
        """

        meeting = create_meeting(
            MeetingCreateRequest(
                meetingName="普通快速会议",
                audioSource="麦克风阵列",
                keywordLibraryIds=[],
            )
        )

        self.assertEqual(meeting["keywordLibraryIds"], [])
        self.assertEqual(meeting["keywordLibraryNames"], [])

    def test_dashboard_overview_counts_frontend_metrics(self):
        payload = get_dashboard_overview()

        self.assertEqual(
            {item["key"] for item in payload["items"]},
            {"todayMeetings", "readyMinutes", "pendingTodos", "voiceprintRate", "sensitiveRules"},
        )

    def test_workflow_status_reports_local_deepseek_without_leaking_secrets(self):
        """AI 状态接口要说明当前是本地编排 + DeepSeek，并且不能泄漏密钥。

        路径仍叫 `/api/workflows/status` 是为了兼容旧前端和验收脚本；但用户已经明确要求不再接
        智能体平台，所以这里固定契约：五个会议能力都由后端本地编排，状态只暴露 provider/model/
        是否配置 key，不返回 API Key、token、密码或旧平台 invoke 地址。
        """

        payload = get_workflow_status()

        self.assertIn("remoteReady", payload)
        self.assertIn(payload["mode"], {"local_deepseek", "local_fallback"})
        self.assertEqual(payload["provider"], "deepseek")
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertTrue(all(item["configured"] for item in payload["workflows"].values()))
        self.assertNotIn("invoke", str(payload).lower())
        self.assertNotIn("token", str(payload).lower())
        self.assertNotIn("password", str(payload).lower())
        self.assertNotIn("sk-", str(payload).lower())

    def test_keyword_library_crud(self):
        created = create_keyword_library(
            KeywordLibraryRequest(name="后端测试词库", words=["FastAPI", "接口契约"], enabled=True, scope="联调会议")
        )

        self.assertEqual(created["name"], "后端测试词库")
        patched = update_keyword_library(created["id"], ConfigPatchRequest(enabled=False, words=["FastAPI"]))
        self.assertFalse(patched["enabled"])
        listed = list_keyword_libraries()["items"]
        self.assertTrue(any(item["id"] == created["id"] for item in listed))
        deleted = delete_keyword_library(created["id"])
        self.assertTrue(deleted["deleted"])

    def test_sensitive_rules_and_templates_crud(self):
        rule = create_sensitive_rule(
            SensitiveRuleRequest(word="测试敏感词", replacement="stars", enabled=True, scope="展示与导出", remark="测试")
        )
        self.assertEqual(rule["word"], "测试敏感词")

        template = create_template(
            TemplateRequest(name="后端测试模板", type="联调会议", isDefault=False, sections=["会议信息", "接口检查"])
        )
        self.assertEqual(template["sections"], ["会议信息", "接口检查"])

        self.assertTrue(delete_sensitive_rule(rule["id"])["deleted"])
        self.assertTrue(delete_template(template["id"])["deleted"])

    def test_forbidden_words_support_display_modes_and_case_sensitive(self):
        """Sensitive-rule API fields retain their display mode and case-sensitive policy behavior."""

        stars = create_sensitive_rule(
            SensitiveRuleRequest(
                word="Token",
                replacement="stars",
                displayMode="stars",
                applyScope="display",
                caseSensitive=True,
                language="en",
            )
        )
        hidden = create_sensitive_rule(
            SensitiveRuleRequest(
                word="remove", replacement="hide", displayMode="hide", applyScope="display", language="en"
            )
        )
        legacy_only = create_sensitive_rule(
            SensitiveRuleRequest(word="legacy", replacement="hide", scope="展示", language="en")
        )
        meeting = create_meeting(MeetingCreateRequest(meetingName="forbidden words", language="en"))
        meeting["segments"] = [{"id": "segment-1", "text": "token Token remove", "rawText": "token Token remove"}]
        store._save("meetings", meeting)

        view = main_module.get_transcript_view(meeting["id"])

        self.assertEqual(stars["displayMode"], "stars")
        self.assertTrue(stars["caseSensitive"])
        self.assertEqual(hidden["displayMode"], "hide")
        self.assertEqual(legacy_only["displayMode"], "hide")
        self.assertEqual(legacy_only["applyScope"], "展示")
        self.assertEqual(view["segments"][0]["displayText"], "token ***** ")

    def test_voiceprint_update_is_persisted(self):
        """声纹编辑要真正落到持久化 Store，不能只修改属性快照。"""
        created = create_voiceprint(VoiceprintRequest(name="测试发言人", department="测试部门", samples=1, enabled=True))

        patched = update_voiceprint(created["id"], ConfigPatchRequest(department="综合保障部", enabled=False, samples=3))
        reloaded = store.voiceprints[created["id"]]

        self.assertEqual(patched["department"], "综合保障部")
        self.assertFalse(reloaded["enabled"])
        self.assertEqual(reloaded["samples"], 3)

    def test_voiceprint_sample_upload_creates_registration_job(self):
        """上传声纹样本后应保存音频、增加样本数，并生成可查询的声纹注册任务。"""
        import asyncio
        from fastapi import UploadFile

        created = create_voiceprint(VoiceprintRequest(name="样本上传发言人", department="测试部门", samples=0, enabled=True))
        upload = UploadFile(filename="sample.wav", file=BytesIO(b"fake wav bytes"))

        result = asyncio.run(upload_voiceprint_sample(created["id"], upload))
        reloaded = store.voiceprints[created["id"]]

        # Fake bytes can encounter either an unconfigured gateway or a reachable real CAM++ that
        # rejects the invalid sample. Both are truthful non-registration states; local port state
        # must never make this contract test expect a fabricated registered embedding.
        self.assertIn(result["status"], {"waiting_model_config", "failed"})
        self.assertEqual(reloaded["samples"], 1)
        self.assertEqual(len(reloaded["sampleFiles"]), 1)
        self.assertEqual(result["job"]["type"], "voiceprint_register")
        self.assertEqual(result["job"]["status"], result["status"])
        self.assertEqual(reloaded["registerStatus"], result["status"])

    def test_default_template_is_unique_and_persisted(self):
        """默认纪要模板只能有一个，切换后重启/重读也要保持一致。"""
        template = create_template(
            TemplateRequest(name="唯一默认模板测试", type="联调会议", isDefault=False, sections=["会议事项", "落实要求"])
        )

        update_template(template["id"], ConfigPatchRequest(isDefault=True))
        templates = list_templates()["items"]
        default_templates = [item for item in templates if item.get("isDefault")]

        self.assertEqual(len(default_templates), 1)
        self.assertEqual(default_templates[0]["id"], template["id"])

    def test_transcribe_updates_pipeline_status(self):
        meeting = self._create_import_meeting(MeetingCreateRequest(meetingName="导入联调"))
        file_record = store.save_file(meeting["id"], "demo.wav", Path(__file__), "audio/wav")
        self.assertEqual(file_record["pipelineStatus"], "uploaded")

        # This contract verifies persistence transitions, not whether a developer's external ASR
        # credential is reachable.  Bind the deterministic local gateway so the assertion remains
        # meaningful across environments while still using the real import ingestion route.
        with patch.object(main_module, "asr_gateway", MockQwenAsrGateway()):
            result = transcribe_file(file_record["id"], TranscribeRequest(enableDiarization=True))

        self.assertEqual(result["status"], "completed")
        detail = get_meeting(meeting["id"])
        self.assertEqual(detail["processStatus"], "completed")
        self.assertGreaterEqual(len(detail["segments"]), 1)

    def test_transcribe_marks_file_failed_when_asr_gateway_raises(self):
        """真实 ASR/DashScope 重试后仍失败时要标记失败，不能用 mock 文本伪装成识别成功。"""
        import app.main as main_module

        class BrokenGateway:
            def transcribe_offline(self, **kwargs):
                raise RuntimeError("ASR service unavailable")

        old_gateway = main_module.asr_gateway
        old_model_mock_mode = main_module.MODEL_MOCK_MODE
        old_asr_mode = main_module.ASR_GATEWAY_MODE
        main_module.asr_gateway = BrokenGateway()
        main_module.MODEL_MOCK_MODE = False
        main_module.ASR_GATEWAY_MODE = "dashscope"
        try:
            meeting = self._create_import_meeting(MeetingCreateRequest(meetingName="失败转写联调"))
            file_record = store.save_file(meeting["id"], "broken.wav", Path(__file__), "audio/wav")

            result = transcribe_file(file_record["id"], TranscribeRequest(enableDiarization=True))
        finally:
            main_module.asr_gateway = old_gateway
            main_module.MODEL_MOCK_MODE = old_model_mock_mode
            main_module.ASR_GATEWAY_MODE = old_asr_mode

        self.assertEqual(result["status"], "failed")
        self.assertIn("ASR service unavailable", result["message"])
        self.assertEqual(result["segments"], [])
        self.assertIn("jobId", result)
        self.assertEqual(store.files[file_record["id"]]["pipelineStatus"], "failed")
        self.assertEqual(store.get_or_create_meeting(meeting["id"])["processStatus"], "failed")

    def test_import_transcribe_endpoint_hides_internal_meeting_creation_and_reports_asr_failure(self):
        """导入转写页只提交文件任务；内部记录创建由后端完成，前端不需要暴露“创建会议”。"""
        import asyncio
        import app.main as main_module
        from fastapi import UploadFile

        class BrokenGateway:
            def transcribe_offline(self, **kwargs):
                raise RuntimeError("DashScope rejected audio")

        old_gateway = main_module.asr_gateway
        old_model_mock_mode = main_module.MODEL_MOCK_MODE
        old_asr_mode = main_module.ASR_GATEWAY_MODE
        main_module.asr_gateway = BrokenGateway()
        main_module.MODEL_MOCK_MODE = False
        main_module.ASR_GATEWAY_MODE = "dashscope"
        try:
            upload = UploadFile(filename="import.wav", file=BytesIO(b"fake wav bytes"))
            result = asyncio.run(
                main_module.import_and_transcribe_file(
                    file=upload,
                    language="中文普通话",
                    template_id="tpl-001",
                    keyword_library_ids="kw-001,kw-002",
                    enable_diarization=True,
                )
            )
        finally:
            main_module.asr_gateway = old_gateway
            main_module.MODEL_MOCK_MODE = old_model_mock_mode
            main_module.ASR_GATEWAY_MODE = old_asr_mode

        self.assertIn("meeting", result)
        self.assertIn("file", result)
        self.assertIn("transcription", result)
        self.assertEqual(result["transcription"]["status"], "failed")
        self.assertIn("DashScope rejected audio", result["transcription"]["message"])
        self.assertEqual(result["meeting"]["audioSource"], "上传文件")

    def test_transcribe_applies_registered_voiceprint_match_to_segments(self):
        """离线转写完成后要把已注册声纹匹配到片段上，避免前端只能看到“实时发言人/待匹配发言人”。

        这个测试用一个很小的假 ASR 网关和假声纹客户端锁定契约：ASR 只负责文字，声纹服务返回
        已注册人员后，后端需要把 speakerName、voiceprintId、confidence 写回 segments，前端才能在导入
        转写或实时会议详情里自动区分“是谁在说话”。
        """
        import app.main as main_module

        class GenericSpeakerGateway:
            def transcribe_offline(self, **kwargs):
                return {
                    "status": "completed",
                    "segments": [
                        {
                            "id": "seg-generic-1",
                            "speakerName": "待匹配发言人",
                            "startMs": 0,
                            "endMs": 3000,
                            "text": "请把这一段匹配到已经提前录入的声纹人员。",
                        }
                    ],
                }

        class MatchedVoiceprintClient:
            def __init__(self, base_url):
                self.base_url = base_url

            def diarize(self, **kwargs):
                return {"status": "completed", "segments": []}

            def match(self, **kwargs):
                return {
                    "status": "matched",
                    "matches": [
                        {
                            "speakerName": "王忠",
                            "voiceprintId": "vp-001",
                            "confidence": 0.93,
                        }
                    ],
                }

        old_gateway = main_module.asr_gateway
        old_voiceprint_url = main_module.VOICEPRINT_GATEWAY_BASE_URL
        old_client = main_module.LocalVoiceprintClient
        main_module.asr_gateway = GenericSpeakerGateway()
        main_module.VOICEPRINT_GATEWAY_BASE_URL = "http://127.0.0.1:8100"
        main_module.LocalVoiceprintClient = MatchedVoiceprintClient
        try:
            store.update_config_item(
                "voiceprints",
                "vp-001",
                {"registerStatus": "registered", "modelStatus": "registered", "embeddingId": "emb-vp-001", "enabled": True},
            )
            meeting = self._create_import_meeting(MeetingCreateRequest(meetingName="声纹自动识别联调"))
            file_record = store.save_file(meeting["id"], "voiceprint.wav", Path(__file__), "audio/wav")

            result = transcribe_file(file_record["id"], TranscribeRequest(enableDiarization=True))
        finally:
            main_module.asr_gateway = old_gateway
            main_module.VOICEPRINT_GATEWAY_BASE_URL = old_voiceprint_url
            main_module.LocalVoiceprintClient = old_client

        self.assertEqual(result["segments"][0]["speakerName"], "王忠")
        self.assertEqual(result["segments"][0]["voiceprintId"], "vp-001")
        self.assertGreaterEqual(result["segments"][0]["voiceprintConfidence"], 0.9)

    def test_realtime_transcription_applies_voiceprint_library_match(self):
        """实时转写也要使用声纹库，而不只是离线导入时才识别人名。

        这个测试直接驱动 WebSocket 路由的协程：前端传入一个有能量的 WAV 分片，后端先走
        ASR 网关生成实时片段，再把同一个音频分片交给声纹匹配逻辑。最终推给前端、并写入
        会议详情的 segment 都必须带上声纹库里的姓名、部门、voiceprintId 和置信度。
        """
        import app.main as main_module

        def build_voice_wav() -> bytes:
            """构造一个超过质量门控阈值的短 WAV，避免测试被静音过滤逻辑提前跳过。"""
            buffer = BytesIO()
            sample_rate = 16000
            duration_seconds = 1.4
            with wave.open(buffer, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                frames = bytearray()
                for index in range(int(sample_rate * duration_seconds)):
                    sample = int(1800 * math.sin(2 * math.pi * 440 * index / sample_rate))
                    frames.extend(struct.pack("<h", sample))
                wav_file.writeframes(bytes(frames))
            return buffer.getvalue()

        class FakeRealtimeAsrGateway:
            def transcribe_realtime_chunk(self, meeting_id, chunk_index, audio_chunk, sensitive_words, mime_type="audio/wav", duration_ms=3000):
                # ASR 只提供“有一段文字”的实时结果，发言人仍是通用占位名；
                # 测试真正验证的是后续声纹库匹配会把它替换成已注册人员。
                return {
                    "type": "transcript",
                    "meetingId": meeting_id,
                    "segment": {
                        "id": f"rt-{meeting_id}-{chunk_index}",
                        "speakerName": "实时发言人",
                        "startMs": 0,
                        "endMs": duration_ms,
                        "text": "这段实时音频应该匹配到声纹库人员。",
                    },
                }

        class MatchedRealtimeVoiceprintClient:
            def __init__(self, base_url):
                self.base_url = base_url

            def diarize(self, **kwargs):
                return {"status": "completed", "segments": []}

            def match(self, **kwargs):
                return {
                    "status": "matched",
                    "matches": [
                        {
                            "speakerName": "实时王忠",
                            "voiceprintId": registered["id"],
                            "confidence": 0.91,
                        }
                    ],
                }

        class FakeWebSocket:
            """最小 WebSocket 双端假对象，用于不依赖 httpx/TestClient 地测试路由协程。"""

            def __init__(self, audio_bytes: bytes):
                self.accepted = False
                self.sent_texts: list[str] = []
                self._messages = [{"bytes": audio_bytes}, {"text": "stop"}]

            async def accept(self):
                self.accepted = True

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text: str):
                self.sent_texts.append(text)

        registered = create_voiceprint(
            VoiceprintRequest(name="实时王忠", department="办公室", samples=1, enabled=True)
        )
        registered = store.update_config_item(
            "voiceprints",
            registered["id"],
            {"registerStatus": "registered", "modelStatus": "registered", "embeddingId": "emb-realtime"},
        )
        meeting = create_meeting(MeetingCreateRequest(meetingName="实时声纹联调", enableDiarization=True))
        websocket = FakeWebSocket(build_voice_wav())

        old_gateway = main_module.asr_gateway
        old_voiceprint_url = main_module.VOICEPRINT_GATEWAY_BASE_URL
        old_client = main_module.LocalVoiceprintClient
        main_module.asr_gateway = FakeRealtimeAsrGateway()
        main_module.VOICEPRINT_GATEWAY_BASE_URL = "http://127.0.0.1:8100"
        main_module.LocalVoiceprintClient = MatchedRealtimeVoiceprintClient
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.asr_gateway = old_gateway
            main_module.VOICEPRINT_GATEWAY_BASE_URL = old_voiceprint_url
            main_module.LocalVoiceprintClient = old_client

        transcript_event = next(json.loads(item) for item in websocket.sent_texts if json.loads(item).get("type") == "transcript")
        saved_segment = get_meeting(meeting["id"])["segments"][0]

        self.assertTrue(websocket.accepted)
        self.assertEqual(transcript_event["segment"]["speakerName"], "实时王忠")
        self.assertEqual(transcript_event["segment"]["speakerTitle"], "办公室")
        self.assertEqual(transcript_event["segment"]["voiceprintId"], registered["id"])
        self.assertGreaterEqual(transcript_event["segment"]["voiceprintConfidence"], 0.9)
        self.assertEqual(saved_segment["speakerName"], "实时王忠")

    def test_realtime_transcription_uses_chunk_metadata_timestamps(self):
        """实时 WebSocket 收到 chunk 元数据时，应按真实音频时间戳落库，而不是按 chunk_index 推算。"""
        import app.main as main_module

        def build_voice_wav() -> bytes:
            buffer = BytesIO()
            sample_rate = 16000
            with wave.open(buffer, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                frames = bytearray()
                for index in range(int(sample_rate * 1.4)):
                    sample = int(1800 * math.sin(2 * math.pi * 440 * index / sample_rate))
                    frames.extend(struct.pack("<h", sample))
                wav_file.writeframes(bytes(frames))
            return buffer.getvalue()

        class MetadataAwareAsrGateway:
            def __init__(self):
                self.durations: list[int] = []

            def transcribe_realtime_chunk(self, meeting_id, chunk_index, audio_chunk, sensitive_words, mime_type="audio/wav", duration_ms=3000):
                self.durations.append(duration_ms)
                return {
                    "type": "transcript",
                    "meetingId": meeting_id,
                    "segment": {
                        "id": f"rt-{meeting_id}-{chunk_index}",
                        "speakerName": "实时发言人",
                        "startMs": 0,
                        "endMs": duration_ms,
                        "text": "metadata timestamp text",
                    },
                }

        class FakeWebSocket:
            def __init__(self, audio_bytes: bytes):
                self.accepted = False
                self.sent_texts: list[str] = []
                self._messages = [
                    {
                        "text": json.dumps(
                            {
                                "type": "realtime_config",
                                "mimeType": "audio/wav",
                                "endpointingMode": "balanced",
                                "silenceEndMs": 1200,
                                "sentenceEndSilenceMs": 800,
                                "maxSegmentMs": 25000,
                                "overlapMs": 300,
                            }
                        )
                    },
                    {
                        "text": json.dumps(
                            {
                                "type": "realtime_chunk",
                                "startMs": 1200,
                                "endMs": 6200,
                                "reason": "silence_end",
                                "overlapMs": 300,
                                "speechMs": 5000,
                            }
                        )
                    },
                    {"bytes": audio_bytes},
                    {"text": "stop"},
                ]

            async def accept(self):
                self.accepted = True

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text: str):
                self.sent_texts.append(text)

        meeting = create_meeting(MeetingCreateRequest(meetingName="metadata timestamps", enableDiarization=False))
        websocket = FakeWebSocket(build_voice_wav())
        gateway = MetadataAwareAsrGateway()

        old_gateway = main_module.asr_gateway
        main_module.asr_gateway = gateway
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.asr_gateway = old_gateway

        transcript_event = next(json.loads(item) for item in websocket.sent_texts if json.loads(item).get("type") == "transcript")
        saved_segment = get_meeting(meeting["id"])["segments"][0]

        self.assertEqual(gateway.durations, [5000])
        self.assertEqual(transcript_event["segment"]["startMs"], 1200)
        self.assertEqual(transcript_event["segment"]["endMs"], 6200)
        self.assertEqual(transcript_event["segment"]["flushReason"], "silence_end")
        self.assertEqual(saved_segment["startMs"], 1200)
        self.assertEqual(saved_segment["endMs"], 6200)

    def test_realtime_chunk_passes_transcript_context_to_asr_gateway(self):
        """实时分片必须把上一段转写文本交给 ASR。

        当前系统还不是供应商级“真流式”识别，每个 WAV 分片都会单独请求一次 ASR。
        如果不带上一段会议正文，模型很容易把半句话、专有名词和代词识别错；这个契约
        锁住前后端的上下文传递，避免后续优化时又退回“每段孤立识别”。
        """
        import app.main as main_module

        class ContextAwareAsrGateway:
            def __init__(self):
                self.contexts: list[str] = []

            def transcribe_realtime_chunk(
                self,
                meeting_id,
                chunk_index,
                audio_chunk,
                sensitive_words,
                mime_type="audio/wav",
                duration_ms=3000,
                context_text="",
            ):
                self.contexts.append(context_text)
                return {
                    "type": "transcript",
                    "meetingId": meeting_id,
                    "segment": {
                        "id": f"rt-{meeting_id}-{chunk_index}",
                        "speakerName": "实时发言人",
                        "startMs": 0,
                        "endMs": duration_ms,
                        "text": "这段识别需要继承上一段上下文。",
                    },
                }

        class FakeWebSocket:
            def __init__(self, audio_bytes: bytes):
                self.accepted = False
                self.sent_texts: list[str] = []
                self._messages = [
                    {
                        "text": json.dumps(
                            {
                                "type": "realtime_chunk",
                                "startMs": 6200,
                                "endMs": 11200,
                                "reason": "silence_end",
                                "speechMs": 5000,
                                "contextText": "上一段说到张三负责安全环保部整改。",
                                "sessionToken": "session-context",
                            }
                        )
                    },
                    {"bytes": audio_bytes},
                    {"text": "stop"},
                ]

            async def accept(self):
                self.accepted = True

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text: str):
                self.sent_texts.append(text)

        meeting = create_meeting(MeetingCreateRequest(meetingName="context realtime", enableDiarization=False))
        gateway = ContextAwareAsrGateway()
        websocket = FakeWebSocket(_build_test_wav(amplitude=0.25, seconds=5.0))

        old_gateway = main_module.asr_gateway
        main_module.asr_gateway = gateway
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.asr_gateway = old_gateway

        self.assertTrue(websocket.accepted)
        # The frozen realtime context intentionally includes the meeting title before the previous
        # transcript tail. Assert both components instead of the obsolete transcript-only shape.
        self.assertEqual(len(gateway.contexts), 1)
        # Browser transcript continuity remains useful, but the meeting title itself is metadata
        # and must never be sent back as recognition context from this synchronous fallback path.
        self.assertNotIn("context realtime", gateway.contexts[0])
        self.assertIn("上一段说到张三负责安全环保部整改。", gateway.contexts[0])

    def test_dashscope_realtime_chunk_uses_short_timeout_and_supported_audio_only_payload(self):
        """同步回退分片使用短超时，并遵守 Qwen3-ASR 的单音频输入语法。

        离线导入可以慢慢等；会议现场一旦同步请求卡 300 秒，用户就会觉得实时转写坏了。
        原生 realtime WebSocket 负责连续上下文；同步回退额外塞 text/system 会返回 400，
        因此这里同时验证短超时和唯一 input_audio 内容。
        """

        calls: list[dict[str, Any]] = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "上下文后的实时识别文本"}}]}).encode("utf-8")

        def fake_urlopen(request, timeout):
            calls.append({"payload": json.loads(request.data.decode("utf-8")), "timeout": timeout})
            return FakeResponse()

        gateway = DashScopeAsrGateway(api_key="test-key", base_url="https://dashscope.example", urlopen=fake_urlopen)
        event = gateway.transcribe_realtime_chunk(
            "meeting-context",
            0,
            b"RIFF realtime wav bytes",
            [],
            duration_ms=5000,
            context_text="上一段会议上下文：王忠正在介绍整改计划。",
        )

        content = calls[0]["payload"]["messages"][0]["content"]
        self.assertEqual(event["type"], "transcript")
        self.assertLessEqual(calls[0]["timeout"], 30)
        self.assertEqual(len(calls[0]["payload"]["messages"]), 1)
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "input_audio")

    def test_realtime_websocket_streaming_mode_uses_stream_session_and_partial_events(self):
        """实时会议启用流式模式时，不能再走“WAV 分片 -> 同步 ASR”的慢链路。

        市面实时转写产品的核心体验是边说边出 partial，服务端句末再给 final。
        这个测试用一个假的流式会话模拟供应商 WebSocket：后端收到浏览器 PCM 帧后，
        应把 partial 直接推给前端，把 final 片段落库，并且完全不调用旧的同步分片 ASR。
        """
        import app.main as main_module

        class FailingChunkAsrGateway:
            def transcribe_realtime_chunk(self, *args, **kwargs):
                raise AssertionError("streaming mode must not call sync chunk ASR")

        class FakeStreamSession:
            def __init__(self, on_event):
                self.on_event = on_event
                self.started = False
                self.audio_frames: list[bytes] = []
                self.finished = False

            async def start(self):
                self.started = True

            async def send_audio(self, audio_bytes: bytes):
                self.audio_frames.append(audio_bytes)
                await self.on_event({"type": "partial_transcript", "text": "正在实时预览", "startMs": 0, "endMs": 480})
                await self.on_event(
                    {
                        "type": "transcript",
                        "segment": {
                            "speakerName": "实时发言人",
                            "startMs": 0,
                            "endMs": 960,
                            "text": "这是流式实时转写最终文本。",
                        },
                    }
                )
                # A second utterance in the same provider session proves final events append independently.
                # The original UI bug kept one textarea and replaced it on every later result.
                await self.on_event({"type": "partial_transcript", "text": "第二句正在预览", "startMs": 960, "endMs": 1320})
                await self.on_event(
                    {
                        "type": "transcript",
                        "segment": {
                            "speakerName": "实时发言人",
                            "startMs": 960,
                            "endMs": 1800,
                            "text": "这是第二条独立的最终文本。",
                        },
                    }
                )

            async def finish(self):
                self.finished = True

            async def close(self):
                self.finished = True

        created_sessions: list[FakeStreamSession] = []
        created_contexts: list[str] = []
        speaker_analysis_calls = 0

        def fake_analyze_realtime_speaker_wav(wav_bytes: bytes):
            nonlocal speaker_analysis_calls
            self.assertTrue(wav_bytes.startswith(b"RIFF"))
            speaker_analysis_calls += 1
            return ([1.0, 0.0], None) if speaker_analysis_calls == 1 else ([0.0, 1.0], None)

        def fake_create_realtime_stream_session(**kwargs):
            session = FakeStreamSession(kwargs["on_event"])
            created_sessions.append(session)
            created_contexts.append(kwargs.get("context_text", ""))
            return session

        class FakeWebSocket:
            def __init__(self):
                self.accepted = False
                self.sent_texts: list[str] = []
                self._messages = [
                    {
                        "text": json.dumps(
                            {
                                "type": "realtime_config",
                                "streamingMode": "dashscope_realtime",
                                "audioFormat": "pcm16",
                                "sampleRate": 16000,
                                "sessionToken": "stream-session",
                            }
                        )
                    },
                    {"bytes": b"\x01\x00" * (16000 * 2)},
                    {"text": "stop"},
                ]

            async def accept(self):
                self.accepted = True

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text: str):
                self.sent_texts.append(text)

        meeting = create_meeting(MeetingCreateRequest(meetingName="streaming realtime", enableDiarization=True))
        websocket = FakeWebSocket()

        old_gateway = main_module.asr_gateway
        old_factory = main_module.create_realtime_stream_session
        old_speaker_analyzer = main_module._analyze_realtime_speaker_wav
        old_voiceprint_url = main_module.VOICEPRINT_GATEWAY_BASE_URL
        main_module.asr_gateway = FailingChunkAsrGateway()
        main_module.create_realtime_stream_session = fake_create_realtime_stream_session
        main_module._analyze_realtime_speaker_wav = fake_analyze_realtime_speaker_wav
        # 此用例只验证 provider-native 文本流。整场 3D-Speaker 的存储原子性另有专门测试，
        # 这里关闭外部模型网络，避免单元测试依赖本机 8100 服务和冷启动耗时。
        main_module.VOICEPRINT_GATEWAY_BASE_URL = ""
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.asr_gateway = old_gateway
            main_module.create_realtime_stream_session = old_factory
            main_module._analyze_realtime_speaker_wav = old_speaker_analyzer
            main_module.VOICEPRINT_GATEWAY_BASE_URL = old_voiceprint_url

        partial_event = next(json.loads(item) for item in websocket.sent_texts if json.loads(item).get("type") == "partial_transcript")
        transcript_event = next(json.loads(item) for item in websocket.sent_texts if json.loads(item).get("type") == "transcript")
        saved_segments = get_meeting(meeting["id"])["segments"]

        self.assertTrue(websocket.accepted)
        self.assertTrue(created_sessions[0].started)
        self.assertEqual(len(created_sessions[0].audio_frames[0]), 16000 * 2 * 2)
        self.assertEqual(partial_event["text"], "正在实时预览")
        self.assertEqual(transcript_event["segment"]["text"], "这是流式实时转写最终文本。")
        self.assertEqual([segment["text"] for segment in saved_segments], [
            "这是流式实时转写最终文本。",
            "这是第二条独立的最终文本。",
        ])
        self.assertEqual(len({segment["id"] for segment in saved_segments}), 2)
        self.assertEqual(transcript_event["sessionToken"], "stream-session")
        sent_events = [json.loads(item) for item in websocket.sent_texts]
        final_indexes = [index for index, item in enumerate(sent_events) if item.get("type") == "transcript"]
        speaker_indexes = [index for index, item in enumerate(sent_events) if item.get("type") == "speaker_update"]
        self.assertEqual(len(final_indexes), 2)
        self.assertEqual(len(speaker_indexes), 2, "有效短句向量应在实时阶段回填稳定发言人身份")
        self.assertGreater(speaker_indexes[0], final_indexes[0], "正文必须先于异步发言人更新显示")
        speaker_updates = [sent_events[index] for index in speaker_indexes]
        self.assertEqual(
            [item.get("speakerName") for item in speaker_updates],
            ["发言人1", "发言人2"],
            "明显不同的短句向量必须区分为两个会议内身份",
        )
        self.assertEqual(speaker_analysis_calls, 2, "CAM++ 应依次分析每个已落库的最终片段")
        self.assertEqual([segment["text"] for segment in get_meeting(meeting["id"])["segments"]], [
            "这是流式实时转写最终文本。",
            "这是第二条独立的最终文本。",
        ])
        # A meeting title is record metadata, not spoken context. The provider-native stream must
        # receive no title even when the meeting has no other participant or policy vocabulary.
        self.assertNotIn("streaming realtime", created_contexts[0])

    def test_realtime_reconfiguration_finishes_old_provider_without_relabeling_its_final(self):
        """重复配置必须先 flush 旧上游，旧 final 仍使用旧 token，不能混入新会话。"""

        class FakeStreamSession:
            def __init__(self, on_event, ordinal):
                self.on_event = on_event
                self.ordinal = ordinal
                self.started = False
                self.finished = False

            async def start(self):
                self.started = True

            async def send_audio(self, audio_bytes):
                pass

            async def finish(self):
                self.finished = True
                if self.ordinal == 1:
                    await self.on_event({"type": "transcript", "segment": {"speakerName": "实时发言人", "startMs": 0, "endMs": 500, "text": "旧会话末句"}})

            async def close(self):
                self.finished = True

        sessions = []

        def factory(**kwargs):
            session = FakeStreamSession(kwargs["on_event"], len(sessions) + 1)
            sessions.append(session)
            return session

        class FakeWebSocket:
            def __init__(self):
                self.sent_texts = []
                self._messages = [
                    {"text": json.dumps({"type": "realtime_config", "streamingMode": "dashscope_realtime", "sampleRate": 16000, "sessionToken": "session-a"})},
                    {"text": json.dumps({"type": "realtime_config", "streamingMode": "dashscope_realtime", "sampleRate": 16000, "sessionToken": "session-b"})},
                    {"text": "stop"},
                ]

            async def accept(self):
                pass

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text):
                self.sent_texts.append(text)

        meeting = create_meeting(MeetingCreateRequest(meetingName="reconfigure", enableDiarization=False))
        websocket = FakeWebSocket()
        old_factory = main_module.create_realtime_stream_session
        main_module.create_realtime_stream_session = factory
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.create_realtime_stream_session = old_factory

        transcript = next(json.loads(item) for item in websocket.sent_texts if json.loads(item).get("type") == "transcript")
        self.assertEqual(len(sessions), 2)
        self.assertTrue(sessions[0].finished)
        self.assertTrue(sessions[1].finished)
        self.assertEqual(transcript["sessionToken"], "session-a")
        self.assertEqual(get_meeting(meeting["id"])["segments"][0]["realtimeSessionToken"], "session-a")

    def test_realtime_disconnect_finishes_provider_and_persists_buffered_last_sentence(self):
        """浏览器断开时仍要 flush 供应商缓冲；无法回推 UI 也必须保住末句落库。"""

        class FakeStreamSession:
            def __init__(self, on_event):
                self.on_event = on_event
                self.finished = False

            async def start(self):
                pass

            async def send_audio(self, audio_bytes):
                pass

            async def finish(self):
                self.finished = True
                await self.on_event({"type": "transcript", "segment": {"speakerName": "实时发言人", "startMs": 0, "endMs": 600, "text": "断网前最后一句"}})

            async def close(self):
                raise AssertionError("disconnect 必须调用 finish 而不是直接 close")

        created = []

        def factory(**kwargs):
            session = FakeStreamSession(kwargs["on_event"])
            created.append(session)
            return session

        class FakeWebSocket:
            def __init__(self):
                self.sent_texts = []
                self.received_config = False

            async def accept(self):
                pass

            async def receive(self):
                if not self.received_config:
                    self.received_config = True
                    return {"text": json.dumps({"type": "realtime_config", "streamingMode": "dashscope_realtime", "sampleRate": 16000, "sessionToken": "disconnect-session"})}
                raise WebSocketDisconnect()

            async def send_text(self, text):
                self.sent_texts.append(text)

        meeting = create_meeting(MeetingCreateRequest(meetingName="disconnect flush", enableDiarization=False))
        old_factory = main_module.create_realtime_stream_session
        main_module.create_realtime_stream_session = factory
        try:
            asyncio.run(realtime_meeting(FakeWebSocket(), meeting["id"]))
        finally:
            main_module.create_realtime_stream_session = old_factory

        self.assertTrue(created[0].finished)
        saved = get_meeting(meeting["id"])["segments"]
        self.assertEqual([item["text"] for item in saved], ["断网前最后一句"])
        self.assertEqual(saved[0]["realtimeSessionToken"], "disconnect-session")

    def test_realtime_speaker_job_is_discarded_when_session_changes_during_model_call(self):
        """模型运行中切换会话时，旧任务返回后不能占用新 tracker 或发送更新。"""

        release_analysis = threading.Event()

        def slow_analyzer(wav_bytes):
            release_analysis.wait(timeout=2)
            return [1.0, 0.0], None

        class FakeStreamSession:
            def __init__(self, on_event, ordinal):
                self.on_event = on_event
                self.ordinal = ordinal

            async def start(self):
                if self.ordinal == 2:
                    release_analysis.set()

            async def send_audio(self, audio_bytes):
                if self.ordinal == 1:
                    await self.on_event({"type": "transcript", "segment": {"speakerName": "实时发言人", "startMs": 0, "endMs": 1000, "text": "旧会话文本"}})
                    await asyncio.sleep(0.05)

            async def finish(self):
                pass

            async def close(self):
                pass

        sessions = []

        def factory(**kwargs):
            session = FakeStreamSession(kwargs["on_event"], len(sessions) + 1)
            sessions.append(session)
            return session

        class FakeWebSocket:
            def __init__(self):
                self.sent_texts = []
                self._messages = [
                    {"text": json.dumps({"type": "realtime_config", "streamingMode": "dashscope_realtime", "sampleRate": 16000, "sessionToken": "session-a"})},
                    {"bytes": b"\x01\x00" * 16000},
                    {"text": json.dumps({"type": "realtime_config", "streamingMode": "dashscope_realtime", "sampleRate": 16000, "sessionToken": "session-b"})},
                    {"text": "stop"},
                ]

            async def accept(self):
                pass

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text):
                self.sent_texts.append(text)

        meeting = create_meeting(MeetingCreateRequest(meetingName="slow speaker switch", enableDiarization=True))
        websocket = FakeWebSocket()
        old_factory = main_module.create_realtime_stream_session
        old_analyzer = main_module._analyze_realtime_speaker_wav
        main_module.create_realtime_stream_session = factory
        main_module._analyze_realtime_speaker_wav = slow_analyzer
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.create_realtime_stream_session = old_factory
            main_module._analyze_realtime_speaker_wav = old_analyzer

        events = [json.loads(item) for item in websocket.sent_texts]
        self.assertFalse(any(item.get("type") == "speaker_update" for item in events))
        self.assertEqual(get_meeting(meeting["id"])["segments"][0]["realtimeSessionToken"], "session-a")

    def test_realtime_wav_fallback_filters_context_title_and_does_not_persist_title_echo(self):
        """The synchronous WAV path must share the final echo guard used by native streaming.

        This fixture also places identity values in frozen policy words and browser context to prove
        the final fallback prompt cannot accidentally reintroduce metadata removed by the builder.
        """

        class CapturingTitleEchoGateway:
            """Capture the final ASR context and return the meeting title as provider final text."""

            def __init__(self, title: str):
                self.title = title
                self.contexts: list[str] = []

            def transcribe_realtime_chunk(self, *args, **kwargs):
                self.contexts.append(str(kwargs.get("context_text") or ""))
                return {
                    "type": "transcript",
                    "segment": {
                        "speakerName": "实时发言人",
                        "startMs": 0,
                        "endMs": 5000,
                        "text": self.title,
                    },
                }

        class FakeWebSocket:
            """Send one voiced WAV with a browser context equal to the imported filename."""

            def __init__(self, browser_context: str):
                self.sent_texts: list[str] = []
                self._messages = [
                    {
                        "text": json.dumps(
                            {
                                "type": "realtime_chunk",
                                "startMs": 1000,
                                "endMs": 6000,
                                "speechMs": 5000,
                                "contextText": browser_context,
                                "sessionToken": "fallback-title-session",
                            }
                        )
                    },
                    {"bytes": _build_test_wav(amplitude=0.25)},
                    {"text": "stop"},
                ]

            async def accept(self):
                return None

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text: str):
                self.sent_texts.append(text)

        meeting_title = "同步回退会议标题"
        # The deliberately long filename makes the complete browser block exceed the old 500-char
        # receive-time cap. If truncation happens before line filtering, a suffix of this identity
        # leaks into ASR context and can no longer compare equal to the persisted fileName.
        imported_filename = f"同步回退录音-{'甲' * 520}.wav"
        normal_browser_context = "浏览器保留的正常正文"
        meeting = create_meeting(MeetingCreateRequest(meetingName=meeting_title, enableDiarization=False))
        persisted = self.store.get_or_create_meeting(meeting["id"])
        persisted["fileName"] = imported_filename
        # Inject the title through the already-frozen policy snapshot. This reproduces existing
        # records whose historical keyword configuration already contains meeting metadata.
        persisted["processingConfig"]["recognitionPolicy"]["words"] = [meeting_title, "保留策略词"]
        self.store._save("meetings", persisted)
        websocket = FakeWebSocket(f"{imported_filename}\n{normal_browser_context}")
        gateway = CapturingTitleEchoGateway(meeting_title)

        old_gateway = main_module.asr_gateway
        main_module.asr_gateway = gateway
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.asr_gateway = old_gateway

        sent_events = [json.loads(item) for item in websocket.sent_texts]
        filtered_status = next(event for event in sent_events if event.get("code") == "context_echo_filtered")
        saved_meeting = get_meeting(meeting["id"])

        self.assertEqual(filtered_status["sessionToken"], "fallback-title-session")
        self.assertEqual(saved_meeting["segments"], [])
        self.assertEqual(int(saved_meeting.get("transcriptRevision", 0)), 0)
        self.assertNotIn(meeting_title, gateway.contexts[0])
        self.assertNotIn(imported_filename, gateway.contexts[0])
        # Exact equality proves no truncated filename suffix survived and that length limiting was
        # applied only after the complete identity line had been removed.
        self.assertEqual(gateway.contexts[0], f"保留策略词\n{normal_browser_context}")

    def test_realtime_reconnect_generates_unique_segment_ids(self):
        """同一会议暂停后重新开始实时识别时，新片段不能覆盖旧片段。

        WebSocket 连接内的 chunk_index 会从 0 开始；如果后端直接使用 ASR 网关返回的
        `rt-{meeting_id}-0`，前端会把新结果当成已有 DOM 片段，表现为“下一次识别覆盖上一次”。
        """
        import app.main as main_module

        class DuplicateIdRealtimeAsrGateway:
            def transcribe_realtime_chunk(self, meeting_id, chunk_index, audio_chunk, sensitive_words, mime_type="audio/wav", duration_ms=3000):
                return {
                    "type": "transcript",
                    "meetingId": meeting_id,
                    "segment": {
                        "id": f"rt-{meeting_id}-{chunk_index}",
                        "speakerName": "实时发言人",
                        "startMs": 0,
                        "endMs": duration_ms,
                        "text": f"reconnect text {chunk_index}",
                    },
                }

        class FakeWebSocket:
            def __init__(self, audio_bytes: bytes):
                self.accepted = False
                self.sent_texts: list[str] = []
                self._messages = [
                    {"text": json.dumps({"type": "realtime_chunk", "startMs": 5000, "endMs": 10000, "reason": "silence_end", "overlapMs": 300, "speechMs": 5000})},
                    {"bytes": audio_bytes},
                    {"text": "stop"},
                ]

            async def accept(self):
                self.accepted = True

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text: str):
                self.sent_texts.append(text)

        meeting = create_meeting(MeetingCreateRequest(meetingName="reconnect ids", enableDiarization=False))
        store.add_realtime_segment(
            meeting["id"],
            {"id": f"rt-{meeting['id']}-0", "speakerName": "实时发言人", "startMs": 0, "endMs": 3000, "text": "first text"},
        )
        websocket = FakeWebSocket(_build_test_wav(amplitude=0.25))

        old_gateway = main_module.asr_gateway
        main_module.asr_gateway = DuplicateIdRealtimeAsrGateway()
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.asr_gateway = old_gateway

        saved_segments = get_meeting(meeting["id"])["segments"]
        saved_ids = [segment["id"] for segment in saved_segments]
        transcript_event = next(json.loads(item) for item in websocket.sent_texts if json.loads(item).get("type") == "transcript")

        self.assertEqual(len(saved_segments), 2)
        self.assertEqual(len(saved_ids), len(set(saved_ids)))
        self.assertNotEqual(transcript_event["segment"]["id"], f"rt-{meeting['id']}-0")
        self.assertEqual(saved_segments[-1]["startMs"], 5000)
        self.assertEqual(saved_segments[-1]["endMs"], 10000)

    def test_realtime_websocket_low_volume_status_is_structured_and_keeps_connection_open(self):
        """低音量分片是可恢复的采集状态：后端应返回结构化 status，而不是关闭实时连接。"""
        import app.main as main_module

        class CountingRealtimeAsrGateway:
            def __init__(self):
                self.calls = 0

            def transcribe_realtime_chunk(self, *args, **kwargs):
                self.calls += 1
                return {"type": "transcript", "segment": {"text": "不应被调用"}}

        class FakeWebSocket:
            def __init__(self):
                self.accepted = False
                self.sent_texts: list[str] = []
                self._messages = [{"bytes": _build_test_wav(amplitude=0)}, {"text": "stop"}]

            async def accept(self):
                self.accepted = True

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text: str):
                self.sent_texts.append(text)

        meeting = create_meeting(MeetingCreateRequest(meetingName="low volume realtime", enableDiarization=False))
        websocket = FakeWebSocket()
        gateway = CountingRealtimeAsrGateway()

        old_gateway = main_module.asr_gateway
        main_module.asr_gateway = gateway
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.asr_gateway = old_gateway

        status_event = next(
            json.loads(item)
            for item in websocket.sent_texts
            if json.loads(item).get("type") == "status" and json.loads(item).get("code") == "low_volume"
        )

        self.assertTrue(websocket.accepted)
        self.assertEqual(status_event["code"], "low_volume")
        self.assertIn("rms", status_event)
        self.assertIn("peak", status_event)
        self.assertEqual(gateway.calls, 0)
        self.assertTrue(any(json.loads(item).get("type") == "closed" for item in websocket.sent_texts))

    def test_realtime_short_context_chunk_waits_for_more_audio(self):
        """过短实时片段不能直接落成最终正文。

        市面上的实时转写通常会先积累足够语音上下文，再把稳定结果写入正文；否则 1 秒左右的
        口头停顿、环境声或半句话会被同步 ASR 猜成“嗯嗯/那个”这类碎片。这个测试让后端
        作为最后一道保护：即使前端误把短窗口发过来，也只返回 collecting_context 状态，
        不调用 ASR、不落库，避免污染会议正文。
        """
        import app.main as main_module

        class CountingRealtimeAsrGateway:
            def __init__(self):
                self.calls = 0

            def transcribe_realtime_chunk(self, *args, **kwargs):
                self.calls += 1
                return {"type": "transcript", "segment": {"text": "不应该落库的短碎片"}}

        class FakeWebSocket:
            def __init__(self):
                self.accepted = False
                self.sent_texts: list[str] = []
                self._messages = [
                    {"text": json.dumps({"type": "realtime_config", "sessionToken": "session-a"})},
                    {
                        "text": json.dumps(
                            {
                                "type": "realtime_chunk",
                                "startMs": 0,
                                "endMs": 1200,
                                "reason": "silence_end",
                                "overlapMs": 300,
                                "sessionToken": "session-a",
                            }
                        )
                    },
                    {"bytes": _build_test_wav(amplitude=0.25, seconds=1.2)},
                    {"text": "stop"},
                ]

            async def accept(self):
                self.accepted = True

            async def receive(self):
                return self._messages.pop(0)

            async def send_text(self, text: str):
                self.sent_texts.append(text)

        meeting = create_meeting(MeetingCreateRequest(meetingName="short context realtime", enableDiarization=False))
        websocket = FakeWebSocket()
        gateway = CountingRealtimeAsrGateway()

        old_gateway = main_module.asr_gateway
        main_module.asr_gateway = gateway
        try:
            asyncio.run(realtime_meeting(websocket, meeting["id"]))
        finally:
            main_module.asr_gateway = old_gateway

        status_event = next(
            json.loads(item)
            for item in websocket.sent_texts
            if json.loads(item).get("type") == "status" and json.loads(item).get("code") == "collecting_context"
        )

        self.assertTrue(websocket.accepted)
        self.assertEqual(status_event["code"], "collecting_context")
        self.assertEqual(status_event["sessionToken"], "session-a")
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(get_meeting(meeting["id"])["segments"], [])

    def test_realtime_voiceprint_does_not_guess_single_registered_person_without_gateway(self):
        """实时识别没有声纹模型网关时，不能把新声音强行猜成库里唯一已注册人员。"""
        import app.main as main_module

        store.delete_config_item("voiceprints", "vp-002")
        old_voiceprint_url = main_module.VOICEPRINT_GATEWAY_BASE_URL
        main_module.VOICEPRINT_GATEWAY_BASE_URL = ""
        try:
            result = main_module.match_voiceprint_for_audio("meeting-realtime.wav")
        finally:
            main_module.VOICEPRINT_GATEWAY_BASE_URL = old_voiceprint_url

        self.assertIsNone(result)

    def test_voiceprint_patch_samples_does_not_fabricate_registration_without_embedding(self):
        """A sample count is metadata and cannot substitute for a registered model embedding."""

        created = create_voiceprint(
            VoiceprintRequest(name="现场发言人", department="党建部", samples=0, groupId="vg-ungrouped")
        )
        self.assertEqual(created["registerStatus"], "pending_sample")

        updated = update_voiceprint(
            created["id"],
            ConfigPatchRequest(samples=1, remark="由会议已识别声音同步，无需重复录音"),
        )

        self.assertEqual(updated["samples"], 1)
        self.assertEqual(updated["registerStatus"], "pending_sample")
        self.assertNotEqual(updated.get("modelStatus"), "registered")
        self.assertFalse(updated.get("embeddingId"))
        self.assertIn("无需重复录音", updated["remark"])

    def test_voiceprint_match_ignores_deleted_or_unknown_embedding_candidates(self):
        """声纹服务向量库可能残留已删除人员，后端必须只接受当前业务库仍有效的声纹 ID。"""
        import app.main as main_module

        class GenericSpeakerGateway:
            def transcribe_offline(self, **kwargs):
                return {
                    "status": "completed",
                    "segments": [
                        {
                            "id": "seg-stale-1",
                            "speakerName": "待匹配发言人",
                            "startMs": 0,
                            "endMs": 3000,
                            "text": "这段应该匹配到仍然存在的声纹人员。",
                        }
                    ],
                }

        class StaleFirstVoiceprintClient:
            def __init__(self, base_url):
                self.base_url = base_url

            def diarize(self, **kwargs):
                return {"status": "completed", "segments": []}

            def match(self, **kwargs):
                return {
                    "status": "matched",
                    "matches": [
                        {"speakerName": "已删除人员", "speakerId": "vp-deleted", "confidence": 0.99},
                        {"speakerName": "王忠", "speakerId": "vp-001", "confidence": 0.88},
                    ],
                }

        old_gateway = main_module.asr_gateway
        old_voiceprint_url = main_module.VOICEPRINT_GATEWAY_BASE_URL
        old_client = main_module.LocalVoiceprintClient
        main_module.asr_gateway = GenericSpeakerGateway()
        main_module.VOICEPRINT_GATEWAY_BASE_URL = "http://127.0.0.1:8100"
        main_module.LocalVoiceprintClient = StaleFirstVoiceprintClient
        try:
            store.update_config_item(
                "voiceprints",
                "vp-001",
                {"registerStatus": "registered", "modelStatus": "registered", "embeddingId": "emb-vp-001", "enabled": True},
            )
            meeting = self._create_import_meeting(MeetingCreateRequest(meetingName="过滤已删除声纹候选"))
            file_record = store.save_file(meeting["id"], "stale.wav", Path(__file__), "audio/wav")

            result = transcribe_file(file_record["id"], TranscribeRequest(enableDiarization=True))
        finally:
            main_module.asr_gateway = old_gateway
            main_module.VOICEPRINT_GATEWAY_BASE_URL = old_voiceprint_url
            main_module.LocalVoiceprintClient = old_client

        self.assertEqual(result["segments"][0]["speakerName"], "王忠")
        self.assertEqual(result["segments"][0]["voiceprintId"], "vp-001")
        self.assertGreaterEqual(result["segments"][0]["voiceprintConfidence"], 0.8)

    def test_diarization_segments_without_voiceprint_are_numbered_speakers(self):
        """多人分离只有 speaker key 时，前端也应看到发言人1/发言人2，而不是一串实时发言人。"""

        segments = [
            {"id": "seg-1", "speakerName": "待匹配发言人", "startMs": 0, "endMs": 4000, "text": "第一位发言。"},
            {"id": "seg-2", "speakerName": "待匹配发言人", "startMs": 5000, "endMs": 9000, "text": "第二位发言。"},
        ]
        diarization = {
            "status": "completed",
            "segments": [
                {"speaker": "SPEAKER_00", "startMs": 0, "endMs": 4500},
                {"speaker": "SPEAKER_01", "startMs": 4500, "endMs": 9500},
            ],
        }

        patched = apply_voiceprint_match_to_segments(segments, "", diarization)

        self.assertEqual([item["speakerName"] for item in patched], ["发言人1", "发言人2"])
        self.assertEqual([item["speakerSource"] for item in patched], ["diarization", "diarization"])
        self.assertEqual(
            [item["speakerClusterId"] for item in patched],
            ["diarization-SPEAKER_00", "diarization-SPEAKER_01"],
        )

    def test_long_import_asr_segment_is_split_at_diarization_speaker_changes(self):
        """一个 30 秒 ASR 文本段内的多人轮流发言必须拆回独立底层 segment。

        这是导入转写区别不了发言人的核心回归：旧逻辑只给整个长段选择重叠最多的一位，
        即使 3D-Speaker 已返回两人也会全部显示成发言人1。测试同时锁定原始来源 ID、
        字词时间戳和文字总量，避免修复说话人时破坏编辑、来源跳转或 ASR 原文。
        """

        original_text = "甲方 发言，乙方 回应。"
        word_characters = [char for char in original_text if not char.isspace()]
        segment = {
            "id": "seg-import-long",
            "speakerName": "待匹配发言人",
            "startMs": 0,
            "endMs": 12000,
            "text": original_text,
            "rawText": original_text,
            "wordTimestampsEstimated": True,
            "words": [
                {"text": char, "start_ms": index * 240, "end_ms": (index + 1) * 240}
                for index, char in enumerate(word_characters)
            ],
        }
        diarization_segments = [
            {"speaker": "SPEAKER_00", "start_ms": 0, "end_ms": 6000},
            {"speaker": "SPEAKER_01", "start_ms": 6000, "end_ms": 12000},
        ]

        split = main_module.split_asr_segments_by_diarization([segment], diarization_segments)
        patched = apply_voiceprint_match_to_segments(split, "", {"segments": diarization_segments})

        self.assertEqual([item["speakerName"] for item in patched], ["发言人1", "发言人2"])
        self.assertEqual([item["sourceSegmentId"] for item in patched], ["seg-import-long", "seg-import-long"])
        self.assertEqual("".join(item["rawText"] for item in patched), segment["rawText"])
        self.assertEqual([item["id"] for item in patched], ["seg-import-long-speaker-1", "seg-import-long-speaker-2"])
        self.assertLessEqual(patched[0]["endMs"], patched[1]["startMs"])

    def test_import_diarization_without_word_timestamps_keeps_legacy_segment(self):
        """旧 ASR 适配器没有 words 时必须保持原段，不能为识别人而丢正文。"""

        original = {"id": "legacy", "startMs": 0, "endMs": 5000, "text": "保留旧文本"}
        split = main_module.split_asr_segments_by_diarization(
            [original],
            [{"speaker": "SPEAKER_00", "start_ms": 0, "end_ms": 5000}],
        )

        self.assertEqual(split, [original])

    def test_five_ai_workflow_buttons_and_docx_export_are_frontend_ready(self):
        """前端右侧会议工具只保留规整、摘要、纪要、待办和标记相关能力。

        这里直接调用路由函数固定契约：摘要、纪要、待办、语篇规整都返回前端可展示的数据；
        docx 导出也要返回非空 Word 字节，保证用户能在前端完成完整智慧会议闭环。
        """

        meeting = create_meeting(MeetingCreateRequest(meetingName="五工作流联调会"))
        store.update_meeting(meeting["id"], {"segments": store._default_segments(meeting["id"]), "processStatus": "completed"})

        summary = generate_summary(meeting["id"])
        minutes = generate_minutes(meeting["id"], MinutesRequest(templateName="默认会议纪要"))
        todos = extract_todos(meeting["id"])
        discourse = reorganize_meeting_discourse(meeting["id"])
        exported = export_docx(meeting["id"], ExportRequest(exportKind="all"))
        route_paths = {route.path for route in app.routes}

        self.assertIn("keywords", summary)
        self.assertIn("content", minutes)
        self.assertIn("items", todos)
        self.assertTrue(discourse["text"])
        self.assertNotIn("/api/meetings/{meeting_id}/translate", route_paths)
        self.assertNotIn("/api/meetings/{meeting_id}/tools/mindmap", route_paths)
        self.assertGreater(len(exported.body), 100)
        self.assertEqual(
            exported.media_type,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def test_ai_tool_responses_include_frontend_generation_ui_metadata(self):
        """右侧 AI 工具面板需要流式生成、可编辑、复制、重新生成和添加至纪要。

        后端仍然返回原有业务字段，避免破坏既有调用；同时补充统一的 ui 元数据，
        让前端不用按每个接口硬猜标题、可编辑正文和进度阶段。
        """

        meeting = create_meeting(MeetingCreateRequest(meetingName="AI 生成面板契约"))
        store.update_meeting(meeting["id"], {"segments": store._default_segments(meeting["id"]), "processStatus": "completed"})

        results = [
            generate_summary(meeting["id"]),
            generate_minutes(meeting["id"], MinutesRequest(templateName="默认会议纪要")),
            extract_todos(meeting["id"]),
            reorganize_meeting_discourse(meeting["id"]),
            add_highlight(meeting["id"], {"text": "重点事项", "segmentId": ""}),
        ]

        for result in results:
            self.assertIn("ui", result)
            self.assertIn("title", result["ui"])
            self.assertIn("editableText", result["ui"])
            self.assertIn("progressStages", result["ui"])
            self.assertGreaterEqual(len(result["ui"]["progressStages"]), 3)
            self.assertTrue(result["ui"]["actions"]["copy"])
            self.assertTrue(result["ui"]["actions"]["regenerate"])
            self.assertTrue(result["ui"]["actions"]["applyToMinutes"])

    def test_ai_tool_edited_text_can_be_saved_as_minutes_draft(self):
        """前端右侧 AI 工具结果支持在线修改，修改后的正文要能写回会议纪要。"""

        import app.main as main_module

        meeting = create_meeting(MeetingCreateRequest(meetingName="AI 结果写入纪要"))

        saved = save_minutes_draft(
            meeting["id"],
            main_module.MinutesDraftRequest(sourceTool="summary", content="用户编辑后的摘要正文"),
        )
        detail = get_meeting(meeting["id"])

        self.assertEqual(saved["content"], "用户编辑后的摘要正文")
        self.assertEqual(saved["sourceTool"], "summary")
        self.assertEqual(detail["minutes"]["content"], "用户编辑后的摘要正文")
        self.assertEqual(detail["minutesStatus"], "generated")

    def test_ai_tool_draft_save_persists_per_tool_and_meeting(self):
        """AI 工具生成结果保存后，切换工具或重新打开详情都应直接回显草稿。"""

        meeting = create_meeting(MeetingCreateRequest(meetingName="AI 工具草稿保存"))

        saved = save_ai_tool_draft(
            meeting["id"],
            "summary",
            ToolDraftRequest(content="保存后的 AI 摘要草稿", title="AI 摘要"),
        )
        detail = get_meeting(meeting["id"])

        self.assertEqual(saved["tool"], "summary")
        self.assertEqual(saved["content"], "保存后的 AI 摘要草稿")
        self.assertIn("savedAt", saved)
        self.assertEqual(detail["aiToolDrafts"]["summary"]["content"], "保存后的 AI 摘要草稿")

    def test_discourse_reorganize_returns_frontend_message_when_transcript_is_empty(self):
        """没有转写片段时，语篇规整按钮也要返回可展示提示，而不是空白面板。"""

        meeting = create_meeting(MeetingCreateRequest(meetingName="空转写语篇规整"))

        result = reorganize_meeting_discourse(meeting["id"])

        self.assertIn("暂无可规整", result["text"])

    def test_empty_meeting_ai_tools_return_transcription_guidance(self):
        """空会议不能生成看似真实的摘要、纪要或待办，必须提示先开始识别。

        用户截图里刚创建的快速会议没有任何转写片段，但右侧 AI 摘要却生成了一段完整纪要。
        这会让人误以为会议已经被识别，因此后端也要兜底：即使绕过前端直接调用接口，
        也只能返回“先开始识别/导入文件”的指导文案，不能写入虚假的业务结果。
        """
        meeting = create_meeting(MeetingCreateRequest(meetingName="空会议 AI 工具保护"))

        summary = generate_summary(meeting["id"])
        minutes = generate_minutes(meeting["id"], MinutesRequest(templateName="默认会议纪要"))
        todos = extract_todos(meeting["id"])

        self.assertIn("请先开始会议识别", summary["ui"]["editableText"])
        self.assertIn("请先开始会议识别", minutes["ui"]["editableText"])
        self.assertIn("请先开始会议识别", todos["ui"]["editableText"])
        self.assertEqual(get_meeting(meeting["id"])["segments"], [])


if __name__ == "__main__":
    unittest.main()
