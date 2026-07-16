import io
import json
import math
import struct
import unittest
import wave
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from app.alignment_service import find_audio_window_for_selection
from app.audio_quality import analyze_realtime_chunk_quality, realtime_chunk_has_voice
from app.asr_gateway import DashScopeAsrGateway, _probe_audio_duration_ms, _wav_requires_sync_normalization
from app.integration_service import map_todo_to_task_save_payload
from app.text_processing import apply_sensitive_words
from app.voiceprint_service import build_voiceprint_registration


def _build_test_wav(amplitude: float, sample_rate: int = 16000, seconds: float = 1.2) -> bytes:
    """构造测试用单声道 16-bit WAV，避免单元测试依赖真实麦克风或外部音频文件。"""

    frame_count = int(sample_rate * seconds)
    pcm = bytearray()
    for index in range(frame_count):
        sample = math.sin(2 * math.pi * 440 * index / sample_rate) * amplitude
        pcm.extend(struct.pack("<h", int(max(-1, min(1, sample)) * 32767)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(pcm))
    return buffer.getvalue()


class CoreServicesTest(unittest.TestCase):
    def test_sensitive_words_are_replaced_by_stars(self):
        result = apply_sensitive_words("这个方案很糟糕，但整体可控。", ["糟糕"])

        self.assertEqual(result, "这个方案很**，但整体可控。")

    def test_todo_maps_to_meeting_task_save_payload(self):
        payload = map_todo_to_task_save_payload(
            {
                "title": "完成系统联调",
                "content": "完成 ASR、声纹、纪要联调。",
                "ownerDept": "信息中心",
                "cooperateDept": "办公室",
                "dueDate": "2026-12-31",
                "milestones": [
                    {"time": "2026-08-31", "content": "完成 ASR 接入"},
                    {"time": "2026-10-31", "content": "完成试运行"},
                ],
            },
            meeting_id="m-001",
            meeting_name="智能会议建设会",
        )

        self.assertEqual(payload["taskType"], "TaskManagement")
        self.assertEqual(payload["taskName"], "完成系统联调")
        self.assertEqual(payload["taskContent"], "完成 ASR、声纹、纪要联调。")
        self.assertEqual(payload["responsibleDept"], "信息中心")
        self.assertEqual(payload["cooperateDept"], "办公室")
        self.assertEqual(payload["completeDate"], "2026-12-31")
        self.assertEqual(payload["meetingId"], "m-001")
        self.assertEqual(payload["meetingName"], "智能会议建设会")
        self.assertEqual(
            payload["childNodes"],
            [
                {"majorTime": "2026-08-31", "nodeContent": "完成 ASR 接入"},
                {"majorTime": "2026-10-31", "nodeContent": "完成试运行"},
            ],
        )

    def test_alignment_finds_audio_window_for_selected_text(self):
        words = [
            {"text": "请", "start_ms": 1000, "end_ms": 1100},
            {"text": "完成", "start_ms": 1100, "end_ms": 1400},
            {"text": "系统", "start_ms": 1400, "end_ms": 1700},
            {"text": "联调", "start_ms": 1700, "end_ms": 2100},
        ]

        window = find_audio_window_for_selection("请完成系统联调", "完成系统", words, padding_ms=200)

        self.assertEqual(window, {"start_ms": 900, "end_ms": 1900})

    def test_voiceprint_registration_uses_selection_and_audio_window(self):
        registration = build_voiceprint_registration(
            speaker_name="张三",
            meeting_id="m-001",
            source_file_id="file-001",
            selected_text="请完成系统联调",
            audio_window={"start_ms": 12000, "end_ms": 27000},
        )

        self.assertEqual(registration["speakerName"], "张三")
        self.assertEqual(registration["durationMs"], 15000)
        self.assertEqual(registration["source"], "selection")
        self.assertEqual(registration["meetingId"], "m-001")
        self.assertEqual(registration["sourceFileId"], "file-001")

    def test_dashscope_local_long_audio_is_transcribed_as_timestamped_chunks(self):
        """本地长音频不能整段塞给同步 ASR，应切片后返回连续时间戳片段。"""

        gateway = DashScopeAsrGateway(api_key="sk-test")
        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "meeting.mp3"
            chunk_1 = Path(temp_dir) / "chunk-1.wav"
            chunk_2 = Path(temp_dir) / "chunk-2.wav"
            source.write_bytes(b"fake long meeting audio")
            chunk_1.write_bytes(b"chunk one")
            chunk_2.write_bytes(b"chunk two")
            gateway._split_audio_for_sync_asr = lambda file_path: [(chunk_1, 0, 25000), (chunk_2, 25000, 50000)]
            texts = iter(["第一段真实转写。", "第二段真实转写。"])
            gateway._transcribe_audio_bytes = lambda *args, **kwargs: next(texts)
            gateway.sync_single_max_bytes = 1

            segments = gateway._sync_transcribe_file_segments(
                "m-001",
                "file-001",
                str(source),
                "zh",
                True,
                [],
            )

        self.assertEqual([item["text"] for item in segments], ["第一段真实转写。", "第二段真实转写。"])
        self.assertEqual([item["startMs"] for item in segments], [0, 25000])
        self.assertEqual([item["endMs"] for item in segments], [25000, 50000])

    def test_dashscope_default_sync_direct_window_does_not_exceed_chunk_policy(self):
        """Provider sync input should never bypass the product's 25-second chunk boundary.

        DashScope rejects some otherwise valid WAV inputs around forty seconds with an unsupported
        ASR-input error. Keeping the direct-file threshold at the same value as the proven chunk
        window makes longer local recordings take the VAD/fixed-split path deterministically.
        """

        gateway = DashScopeAsrGateway(api_key="sk-test")

        self.assertEqual(gateway.sync_chunk_seconds, 25)
        self.assertEqual(gateway.sync_single_max_seconds, gateway.sync_chunk_seconds)

    def test_wav_duration_probe_falls_back_when_ffprobe_is_not_installed(self):
        """A valid WAV must still trigger duration-based splitting on minimal Windows hosts."""

        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "meeting.wav"
            source.write_bytes(_build_test_wav(amplitude=0.2, seconds=2.5))
            with patch("app.asr_gateway.shutil.which", return_value=None):
                duration_ms = _probe_audio_duration_ms(str(source))

        self.assertGreaterEqual(duration_ms, 2490)
        self.assertLessEqual(duration_ms, 2510)

    def test_dashscope_marks_48khz_browser_wav_for_16khz_normalization(self):
        """Browser PCM WAV cannot bypass normalization merely because it is shorter than 25s."""

        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "browser.wav"
            source.write_bytes(_build_test_wav(amplitude=0.2, sample_rate=48000, seconds=1.0))

            self.assertTrue(_wav_requires_sync_normalization(str(source)))

            normalized = Path(temp_dir) / "normalized.wav"
            normalized.write_bytes(_build_test_wav(amplitude=0.2, sample_rate=16000, seconds=1.0))
            self.assertFalse(_wav_requires_sync_normalization(str(normalized)))

    def test_dashscope_offline_payloads_use_only_provider_supported_context_fields(self):
        """Filetrans may use corpus, while sync Qwen3-ASR must contain only one audio item.

        The dedicated sync ASR task rejects extra system/context text as an unsupported input. Its
        public contract has no accuracy-boost support, so frozen terms remain auditable in the
        meeting snapshot but are not injected into an invalid provider grammar.
        """

        captured_payloads = []

        class FakeResponse:
            """Minimal urllib response context manager for adapter request-body inspection."""

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout):
            if request.data is None:
                return FakeResponse({"output": {"task_status": "SUCCEEDED", "text": "provider text"}})
            captured_payloads.append(json.loads(request.data.decode("utf-8")))
            if len(captured_payloads) == 1:
                return FakeResponse({"output": {"task_id": "task-vocabulary"}})
            return FakeResponse({"choices": [{"message": {"content": "provider text"}}]})

        gateway = DashScopeAsrGateway(api_key="sk-test", urlopen=fake_urlopen)
        gateway._filetrans_transcribe_url(
            "https://example.test/meeting.wav",
            "en",
            True,
            hotwords=["KingbaseES", "Qwen3-ASR"],
            poll_interval=0,
        )
        gateway._transcribe_audio_bytes(
            b"audio",
            "audio/wav",
            "en",
            True,
            hotwords=["KingbaseES", "Qwen3-ASR"],
        )

        filetrans_payload, sync_payload = captured_payloads
        self.assertEqual(filetrans_payload["parameters"]["corpus"], {"text": "KingbaseES\nQwen3-ASR"})
        self.assertEqual(len(sync_payload["messages"]), 1)
        self.assertEqual(sync_payload["messages"][0]["role"], "user")
        self.assertEqual(len(sync_payload["messages"][0]["content"]), 1)
        self.assertEqual(sync_payload["messages"][0]["content"][0]["type"], "input_audio")

    def test_vad_segments_are_merged_padded_and_capped_for_offline_asr(self):
        """导入转写应优先按 VAD 语音端点切分，再用最大段长兜底，避免固定时间切断半句话。"""

        gateway = DashScopeAsrGateway(api_key="sk-test")

        windows = gateway._speech_windows_from_vad_segments(
            [
                {"start_ms": 0, "end_ms": 6200},
                {"start_ms": 6500, "end_ms": 12000},
                {"start_ms": 45000, "end_ms": 78000},
            ],
            duration_ms=80000,
            merge_gap_ms=500,
            padding_ms=300,
            max_segment_ms=30000,
        )

        self.assertEqual(windows, [(0, 12300), (44700, 74700), (74700, 78300)])

    def test_dashscope_long_audio_retries_transient_chunk_failures(self):
        """长录音分片转写遇到连接瞬断时要重试当前分片，不能整条导入降级成 mock 结果。"""

        gateway = DashScopeAsrGateway(api_key="sk-test")
        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "meeting.mp3"
            chunk_1 = Path(temp_dir) / "chunk-1.wav"
            source.write_bytes(b"fake long meeting audio")
            chunk_1.write_bytes(b"chunk one")
            gateway._split_audio_for_sync_asr = lambda file_path: [(chunk_1, 0, 25000)]
            gateway.sync_single_max_bytes = 1
            attempts = {"count": 0}

            def flaky_transcribe(*args, **kwargs):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("<urlopen error [Errno 10053] 你的主机中的软件中止了一个已建立的连接。>")
                return "重试后真实转写成功。"

            gateway._transcribe_audio_bytes = flaky_transcribe

            segments = gateway._sync_transcribe_file_segments(
                "m-001",
                "file-001",
                str(source),
                "zh",
                True,
                [],
            )

        self.assertEqual(attempts["count"], 2)
        self.assertEqual([item["text"] for item in segments], ["重试后真实转写成功。"])

    def test_dashscope_invalid_parameter_reports_single_attempt_without_fake_retry_count(self):
        """确定性的 400 参数错误只请求一次，错误文案不能声称已经重试多次。"""

        gateway = DashScopeAsrGateway(api_key="sk-test")
        attempts = {"count": 0}

        def invalid_parameter(*args, **kwargs):
            # 供应商明确返回 InvalidParameter 时，继续发送同一请求不会恢复，反而会增加等待时间和费用。
            attempts["count"] += 1
            raise RuntimeError("HTTP 400 InvalidParameter: unsupported language")

        gateway._transcribe_audio_bytes = invalid_parameter

        with self.assertRaisesRegex(RuntimeError, "DashScope 分片 1失败：HTTP 400 InvalidParameter") as error:
            gateway._transcribe_audio_bytes_with_retry(
                b"fake wav",
                "audio/wav",
                "zh",
                True,
                context="DashScope 分片 1",
                max_retries=3,
            )

        self.assertEqual(attempts["count"], 1)
        self.assertNotIn("已重试 3 次", str(error.exception))


    def test_realtime_audio_gate_rejects_silent_wav_chunks(self):
        """实时 ASR 不能把静音分片送进模型，否则真实模型容易把底噪幻听成乱码文本。"""

        self.assertFalse(realtime_chunk_has_voice(_build_test_wav(amplitude=0)))

    def test_realtime_audio_gate_allows_speech_like_wav_chunks(self):
        """有明显能量的分片应继续进入 ASR，避免质量门控把正常说话误拦截。"""

        self.assertTrue(realtime_chunk_has_voice(_build_test_wav(amplitude=0.08)))

    def test_realtime_audio_quality_returns_diagnostics_and_allows_quiet_speech(self):
        """安静环境下的人声不能只因音量偏小被误判成静音，同时要返回调试用的能量指标。"""

        quality = analyze_realtime_chunk_quality(_build_test_wav(amplitude=0.018))

        self.assertTrue(quality.has_voice)
        self.assertGreater(quality.rms, 0)
        self.assertGreater(quality.peak, 0)
        self.assertGreater(quality.active_ratio, 0)
        self.assertEqual(quality.reason, "voice")


if __name__ == "__main__":
    unittest.main()
