import json
import math
import os
import struct
import tempfile
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.asr_gateway import DashScopeAsrGateway, _request_json, create_asr_gateway
from app.llm_workflow import DeepSeekWorkflowClient
from app.model_clients import (
    LocalAlignmentClient,
    LocalModelServiceError,
    LocalVadClient,
    LocalVoiceprintClient,
)
from model_services import local_models_api as model_api


class _FakeResponse:
    """测试用 HTTP 响应对象。

    真实代码使用 urllib.request.urlopen，本对象只实现测试需要的 read()/status
    能力，避免单元测试真正访问 DashScope、声纹服务或智能体平台。
    """

    def __init__(self, payload):
        self.payload = payload
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _FakeUrlopen:
    """记录 urllib 请求的测试替身。

    每次调用会保存 URL、请求体和请求头，便于断言网关是否按约定
    调用了正确的模型服务路径。
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout=30):
        body = request.data.decode("utf-8") if getattr(request, "data", None) else ""
        self.calls.append(
            {
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": json.loads(body) if body else {},
                "timeout": timeout,
            }
        )
        return _FakeResponse(self.responses.pop(0))


class _FakeHttpErrorResponse:
    """模拟 DashScope 400 响应体。

    urllib.error.HTTPError 的 read() 行为和普通响应相似，单测用它确认网关不会丢失云端返回的具体错误信息。
    """

    def read(self):
        return b'{"code":"InvalidParameter","message":"audio payload is invalid"}'

    def close(self):
        return None


class LocalModelIntegrationTest(unittest.TestCase):
    def test_request_json_preserves_dashscope_http_error_body(self):
        """DashScope 返回 400 时必须保留响应体，方便前端兜底提示和后端日志定位真实原因。"""

        def broken_urlopen(request, timeout=30):
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                hdrs=None,
                fp=_FakeHttpErrorResponse(),
            )

        request = urllib.request.Request("https://dashscope.aliyuncs.com/mock", method="POST")

        with self.assertRaisesRegex(RuntimeError, "audio payload is invalid"):
            _request_json(broken_urlopen, request, timeout=30)

    def test_dashscope_gateway_reads_key_from_env_and_uses_sync_audio_api(self):
        fake_http = _FakeUrlopen(
            [
                {
                    "choices": [
                        {"message": {"content": "今天会议讨论智能会议系统建设。"}}
                    ]
                }
            ]
        )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(b"fake-audio")
            tmp_path = Path(tmp.name)
        self.addCleanup(lambda: tmp_path.exists() and tmp_path.unlink())

        gateway = DashScopeAsrGateway(
            api_key="test-key",
            urlopen=fake_http,
            sync_model="qwen3-asr-flash",
        )
        result = gateway.transcribe_offline(
            meeting_id="m-001",
            file_id="file-001",
            enable_diarization=True,
            hotwords=["智能会议"],
            sensitive_words=[],
            file_path=str(tmp_path),
            # 浏览器下拉框历史上提交的是中文展示文案。这个回归输入必须保留展示值，
            # 才能证明网关边界会转换为 DashScope 接受的 `zh`，而不是把问题隐藏在测试夹具里。
            language="中文普通话",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model"], "qwen3-asr-flash")
        self.assertIn("/compatible-mode/v1/chat/completions", fake_http.calls[0]["url"])
        self.assertEqual(fake_http.calls[0]["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(fake_http.calls[0]["body"]["model"], "qwen3-asr-flash")
        self.assertEqual(fake_http.calls[0]["body"]["asr_options"]["language"], "zh")

    def test_dashscope_realtime_chunk_uses_real_audio_api_instead_of_mock_text(self):
        """实时会议收到浏览器 WAV 分片后，也要走 DashScope ASR，不能继续返回演示假文本。"""
        fake_http = _FakeUrlopen(
            [
                {
                    "choices": [
                        {"message": {"content": "这是麦克风实时识别结果。"}}
                    ]
                }
            ]
        )
        gateway = DashScopeAsrGateway(api_key="test-key", urlopen=fake_http, sync_model="qwen3-asr-flash")

        event = gateway.transcribe_realtime_chunk(
            meeting_id="m-rt",
            chunk_index=0,
            audio_chunk=b"fake wav bytes",
            sensitive_words=[],
            mime_type="audio/wav",
        )

        self.assertEqual(event["type"], "transcript")
        self.assertEqual(event["segment"]["text"], "这是麦克风实时识别结果。")
        self.assertNotIn("实时发言已由 Qwen3-ASR 网关转写", event["segment"]["text"])
        audio_payload = fake_http.calls[0]["body"]["messages"][0]["content"][0]["input_audio"]["data"]
        self.assertTrue(audio_payload.startswith("data:audio/wav;base64,"))

    def test_create_gateway_supports_dashscope_mode_without_hardcoded_key(self):
        old_key = os.environ.get("DASHSCOPE_API_KEY")
        os.environ["DASHSCOPE_API_KEY"] = "env-key"
        try:
            gateway = create_asr_gateway("dashscope")
        finally:
            if old_key is None:
                os.environ.pop("DASHSCOPE_API_KEY", None)
            else:
                os.environ["DASHSCOPE_API_KEY"] = old_key

        self.assertIsInstance(gateway, DashScopeAsrGateway)
        self.assertEqual(gateway.api_key, "env-key")

    def test_local_model_clients_call_standard_service_paths(self):
        fake_http = _FakeUrlopen(
            [
                {"segments": [{"start_ms": 0, "end_ms": 1200}]},
                {"speakerName": "王总", "confidence": 0.91},
                {
                    "model": "CAM++",
                    "embedding": [0.25, 0.75],
                    "realModel": False,
                    "fallbackReason": "LOCAL_MODEL_MOCK_MODE is enabled",
                },
                {"words": [{"text": "会", "start_ms": 0, "end_ms": 200}]},
            ]
        )

        vad = LocalVadClient("http://127.0.0.1:8101", urlopen=fake_http)
        voiceprint = LocalVoiceprintClient("http://127.0.0.1:8103", urlopen=fake_http)
        alignment = LocalAlignmentClient("http://127.0.0.1:8102", urlopen=fake_http)

        self.assertEqual(vad.split("a.wav")["segments"][0]["end_ms"], 1200)
        self.assertEqual(voiceprint.match("a.wav")["speakerName"], "王总")
        self.assertEqual(voiceprint.embedding("a.wav")["embedding"], [0.25, 0.75])
        self.assertEqual(alignment.align("a.wav", "会议")["words"][0]["text"], "会")

        self.assertTrue(fake_http.calls[0]["url"].endswith("/v1/vad/split"))
        self.assertTrue(fake_http.calls[1]["url"].endswith("/v1/voiceprints/match"))
        # embedding 是后端到模型服务的内部调用，协议只发送音频路径，不附带会议业务数据。
        self.assertTrue(fake_http.calls[2]["url"].endswith("/v1/speakers/embedding"))
        self.assertEqual(fake_http.calls[2]["body"], {"audio_path": "a.wav"})
        self.assertTrue(fake_http.calls[3]["url"].endswith("/v1/align"))

    def test_mock_speaker_embedding_is_deterministic_and_explicitly_not_real(self):
        """Mock 向量可以用于联调聚类，但必须明确标记，不能伪装成 CAM++ 推理成功。"""

        with patch.object(model_api, "LOCAL_MODEL_MOCK_MODE", True):
            first = model_api.speaker_embedding(
                model_api.SpeakerEmbeddingRequest(audio_path="meeting-segment.wav")
            )
            second = model_api.speaker_embedding(
                model_api.SpeakerEmbeddingRequest(audio_path="meeting-segment.wav")
            )

        self.assertEqual(first["embedding"], second["embedding"])
        self.assertGreaterEqual(len(first["embedding"]), 16)
        self.assertFalse(first["realModel"])
        self.assertIn("mock", first["fallbackReason"].lower())

    def test_voiceprint_embedding_client_rejects_invalid_response_shapes(self):
        """内部向量接口也必须在客户端边界校验，不能让畸形模型响应进入会议 tracker。"""

        invalid_payloads = [
            [0.25, 0.75],
            {"model": "CAM++", "embedding": []},
            {"model": "CAM++", "embedding": [0.25, "not-a-number"]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                client = LocalVoiceprintClient(
                    "http://127.0.0.1:8103",
                    urlopen=_FakeUrlopen([payload]),
                )
                with self.assertRaises(LocalModelServiceError):
                    client.embedding("a.wav")

    def test_real_speaker_embedding_endpoint_returns_complete_campp_contract(self):
        """真实 CAM++ 成功时 route 必须返回模型、向量、真实性和空降级原因四个字段。"""

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(b"test-audio")
            audio_path = Path(tmp.name)
        self.addCleanup(lambda: audio_path.exists() and audio_path.unlink())

        expected_embedding = [0.6, 0.8]
        with (
            patch.object(model_api, "LOCAL_MODEL_MOCK_MODE", False),
            patch.object(model_api, "_speaker_embedding", return_value=expected_embedding),
        ):
            result = model_api.speaker_embedding(
                model_api.SpeakerEmbeddingRequest(audio_path=str(audio_path))
            )

        self.assertEqual(
            result,
            {
                "model": model_api.CAMPP_MODEL_ID,
                "embedding": expected_embedding,
                "realModel": True,
                "fallbackReason": "",
            },
        )

    def test_real_speaker_embedding_fails_closed_when_campp_fails(self):
        """真实模式不得用确定性 mock 或音频启发式向量掩盖 CAM++ 推理失败。"""

        class BrokenSpeakerModel:
            def generate(self, input):
                raise RuntimeError("CAM++ embedding failed")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(b"test-audio")
            audio_path = Path(tmp.name)
        self.addCleanup(lambda: audio_path.exists() and audio_path.unlink())

        with (
            patch.object(model_api, "LOCAL_MODEL_MOCK_MODE", False),
            patch.object(model_api, "_get_speaker_model", return_value=BrokenSpeakerModel()),
        ):
            with self.assertRaises(HTTPException) as error:
                model_api.speaker_embedding(
                    model_api.SpeakerEmbeddingRequest(audio_path=str(audio_path))
                )

        self.assertEqual(error.exception.status_code, 503)

    def test_real_mode_rejects_campp_failure_without_persisting_a_fallback_embedding(self):
        """A real deployment must return 503 instead of enrolling a non-CAM++ fingerprint."""

        class BrokenSpeakerModel:
            def generate(self, input):
                raise RuntimeError("CAM++ did not return embedding")

        old_mock_mode = model_api.LOCAL_MODEL_MOCK_MODE
        old_db_path = model_api.VOICEPRINT_DB_PATH
        model_api.LOCAL_MODEL_MOCK_MODE = False
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                wav_path = temp_path / "speaker.wav"
                with wave.open(str(wav_path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    frames = bytearray()
                    for index in range(16000):
                        frames += struct.pack("<h", int(math.sin(index / 16000 * math.pi * 2 * 440) * 1200))
                    wav.writeframes(frames)
                model_api.VOICEPRINT_DB_PATH = temp_path / "voiceprint_embeddings.json"

                with patch.object(model_api, "_get_speaker_model", return_value=BrokenSpeakerModel()):
                    with self.assertRaises(HTTPException) as register_error:
                        model_api.register_voiceprint(
                            model_api.VoiceprintRegisterRequest(
                                speaker_id="vp-fallback",
                                speaker_name="Fallback Speaker",
                                audio_path=str(wav_path),
                                metadata={},
                            )
                        )
                    with self.assertRaises(HTTPException) as match_error:
                        model_api.match_voiceprint(
                            model_api.VoiceprintMatchRequest(audio_path=str(wav_path), top_k=1)
                        )
        finally:
            model_api.LOCAL_MODEL_MOCK_MODE = old_mock_mode
            model_api.VOICEPRINT_DB_PATH = old_db_path

        self.assertEqual(register_error.exception.status_code, 503)
        self.assertEqual(match_error.exception.status_code, 503)
        self.assertFalse((temp_path / "voiceprint_embeddings.json").exists())

    def test_health_marks_missing_campp_weights_unavailable_with_service_identity(self):
        """Health exposes actual capability probe results, not merely configured model names."""

        old_mock_mode = model_api.LOCAL_MODEL_MOCK_MODE
        model_api.LOCAL_MODEL_MOCK_MODE = False
        try:
            with patch.object(model_api, "_get_vad_model", return_value=object()), patch.object(
                model_api, "_get_speaker_model", side_effect=RuntimeError("CAM++ weights missing")
            ):
                payload = model_api.health()
        finally:
            model_api.LOCAL_MODEL_MOCK_MODE = old_mock_mode

        self.assertEqual(payload["service"], "intelligent-meeting-local-model-service")
        self.assertFalse(payload["capabilities"]["voiceprint"]["ready"])
        self.assertFalse(payload["capabilities"]["voiceprint"]["embeddingReady"])
        self.assertEqual(payload["capabilities"]["voiceprint"]["state"], "unavailable")
        self.assertIn("CAM++ weights missing", payload["capabilities"]["voiceprint"]["message"])

    def test_health_marks_embedding_ready_with_a_ready_voiceprint_model(self):
        """健康值必须表达实时会议实际依赖的 embedding 推理能力。"""

        with patch.object(model_api, "LOCAL_MODEL_MOCK_MODE", False), patch.object(
            model_api, "_get_vad_model", return_value=object()
        ), patch.object(model_api, "_get_speaker_model", return_value=object()), patch.object(
            model_api, "_probe_alignment_health", return_value=None
        ):
            payload = model_api.health()

        voiceprint = payload["capabilities"]["voiceprint"]
        self.assertTrue(voiceprint["ready"])
        self.assertTrue(voiceprint["embeddingReady"])

    def test_real_selection_window_rejects_unconfigured_aligner_instead_of_using_mock_timestamps(self):
        """Real mode must never certify the heuristic timestamp fallback as forced alignment."""

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(b"test-audio")
            audio_path = Path(tmp.name)
        self.addCleanup(lambda: audio_path.exists() and audio_path.unlink())

        with (
            patch.object(model_api, "LOCAL_MODEL_MOCK_MODE", False),
            patch.object(model_api, "FORCED_ALIGNER_BACKEND_URL", ""),
            patch.object(model_api, "QWEN_FORCED_ALIGNER_MODEL_ID", ""),
        ):
            with self.assertRaises(HTTPException) as error:
                model_api.selection_window(
                    model_api.SelectionWindowRequest(
                        audio_path=str(audio_path),
                        transcript_text="智能会议系统",
                        selected_text="会议",
                        padding_ms=200,
                    )
                )

        self.assertEqual(error.exception.status_code, 503)

    def test_selection_window_maps_character_selection_across_multi_character_tokens(self):
        """选区字符位置不能直接作为 words 下标，多字符 token 必须命中其真实时间戳。"""

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(b"test-audio")
            audio_path = Path(tmp.name)
        self.addCleanup(lambda: audio_path.exists() and audio_path.unlink())

        # 这里模拟真实 ForcedAligner 的词级输出：每个 token 都包含两个汉字。
        # “会议”在拼接文本中的字符起点是 2，但在 words 中的下标是 1；旧实现用
        # words[2:4] 会错误选中“系统”，因此这个数据能稳定覆盖字符下标/token 下标混淆。
        aligned_words = [
            {"text": "智能", "start_ms": 0, "end_ms": 800},
            {"text": "会议", "start_ms": 900, "end_ms": 1500},
            {"text": "系统", "start_ms": 1600, "end_ms": 2200},
        ]
        with (
            patch.object(model_api, "LOCAL_MODEL_MOCK_MODE", False),
            patch.object(model_api, "FORCED_ALIGNER_BACKEND_URL", ""),
            patch.object(model_api, "QWEN_FORCED_ALIGNER_MODEL_ID", "test-forced-aligner"),
            patch.object(model_api, "_align_with_local_qwen", return_value={"words": aligned_words}),
        ):
            result = model_api.selection_window(
                model_api.SelectionWindowRequest(
                    audio_path=str(audio_path),
                    transcript_text="智能会议系统",
                    selected_text="会议",
                    padding_ms=100,
                )
            )

        self.assertEqual(result["start_ms"], 800)
        self.assertEqual(result["end_ms"], 1600)
        self.assertEqual(result["words"], [aligned_words[1]])

    def test_deepseek_workflow_client_uses_openai_compatible_json_protocol(self):
        fake_http = _FakeUrlopen(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "content": "{\"keywords\":[\"ASR\"],\"topic\":\"会议系统\"}"
                            }
                        }
                    ]
                },
            ]
        )
        client = DeepSeekWorkflowClient(
            api_key="deepseek-test-key",
            base_url="http://deepseek.local",
            model="deepseek-v4-pro",
            urlopen=fake_http,
        )

        result = client.complete_json(
            "请输出会议摘要 JSON",
            {
                "transcript_segments": [{"text": "讨论 ASR"}],
                "meeting_meta": {"meetingName": "测试会议"},
            },
        )

        self.assertEqual(result["topic"], "会议系统")
        self.assertTrue(fake_http.calls[0]["url"].endswith("/chat/completions"))
        self.assertEqual(fake_http.calls[0]["headers"]["Authorization"], "Bearer deepseek-test-key")
        self.assertEqual(fake_http.calls[0]["body"]["model"], "deepseek-v4-pro")
        self.assertIn("messages", fake_http.calls[0]["body"])
        self.assertNotIn("workflow_id", fake_http.calls[0]["body"])


if __name__ == "__main__":
    unittest.main()
