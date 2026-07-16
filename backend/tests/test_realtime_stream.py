import asyncio
import io
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path

from app import realtime_stream
from app.realtime_stream import (
    DashScopeRealtimeStreamSession,
    Pcm16TimelineBuffer,
    Pcm16WaveRecorder,
    create_realtime_stream_session,
)


class FakeProviderWebSocket:
    """Minimal async WebSocket double for checking provider protocol frames."""

    def __init__(self):
        self.sent_messages: list[str] = []
        self.closed = False

    async def send(self, message: str):
        self.sent_messages.append(message)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class QueuedProviderWebSocket:
    """Async provider double that can model DashScope's session lifecycle.

    A realtime provider is not ready merely because TCP/WebSocket connected: DashScope validates
    ``session.update`` and acknowledges it with ``session.updated``.  The queue lets tests control that
    acknowledgement and also verifies that ``session.finish`` is followed by final transcript events before
    the client closes the upstream socket.
    """

    def __init__(self, *, acknowledge_update: bool = True, finish_events: list[dict] | None = None):
        self.sent_messages: list[str] = []
        self.closed = False
        self.acknowledge_update = acknowledge_update
        self.finish_events = finish_events or []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, message: str):
        self.sent_messages.append(message)
        frame = json.loads(message)
        if frame["type"] == "session.update" and self.acknowledge_update:
            await self.incoming.put(json.dumps({"type": "session.updated"}))
        if frame["type"] == "session.finish":
            for event in self.finish_events:
                await self.incoming.put(json.dumps(event, ensure_ascii=False))

    async def close(self):
        self.closed = True
        await self.incoming.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message


class RealtimeStreamProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_provider_receiver_survives_browser_callback_failure_and_continues_to_final(self):
        """浏览器发送失败不能杀掉 provider receiver，否则断线边界会丢最后一句。"""

        received = []

        async def flaky_callback(event):
            if not received:
                received.append("failed-once")
                raise RuntimeError("browser send failed")
            received.append(event)

        provider = QueuedProviderWebSocket()
        session = DashScopeRealtimeStreamSession(
            api_key="sk-test", base_url="https://dashscope.aliyuncs.com", model="qwen3-asr-flash-realtime",
            sample_rate=16000, language="zh", sensitive_words=[], on_event=flaky_callback,
        )
        session.websocket = provider
        receiver = asyncio.create_task(session._receive_loop())
        await provider.incoming.put(json.dumps({"type": "conversation.item.input_audio_transcription.text", "text": "预览"}))
        await provider.incoming.put(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "最后一句"}))
        await provider.incoming.put(None)
        await receiver

        self.assertEqual(received[-1]["segment"]["text"], "最后一句")

    async def test_close_does_not_rethrow_already_failed_receiver_task(self):
        async def failed_receiver():
            raise RuntimeError("browser send failed")

        session = DashScopeRealtimeStreamSession(
            api_key="sk-test", base_url="https://dashscope.aliyuncs.com", model="qwen3-asr-flash-realtime",
            sample_rate=16000, language="zh", sensitive_words=[], on_event=lambda event: asyncio.sleep(0),
        )
        session.receiver_task = asyncio.create_task(failed_receiver())
        await asyncio.sleep(0)
        await session.close()

    def test_pcm_timeline_extracts_exact_wav_window_and_trims_old_audio(self):
        """说话人分析必须按 final 时间戳取音频，不能把整场或下一句声音混入当前片段。"""

        timeline = Pcm16TimelineBuffer(sample_rate=1000, max_duration_ms=1000)
        timeline.append(b"\x01\x00" * 600)
        timeline.append(b"\x02\x00" * 600)

        # 1200ms 输入只保留最后 1000ms，因此可用时间线从 200ms 开始。
        self.assertEqual(timeline.available_start_ms, 200)
        self.assertEqual(timeline.available_end_ms, 1200)
        wav_bytes = timeline.extract_wav(500, 900)
        self.assertIsNotNone(wav_bytes)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            self.assertEqual(wav_file.getframerate(), 1000)
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getnframes(), 400)
        self.assertIsNone(timeline.extract_wav(100, 500), "请求起点已被裁掉时不能把残缺音频送去声纹识别")
        self.assertIsNone(timeline.extract_wav(900, 1300), "请求终点尚未到达时不能把残缺音频送去声纹识别")

    def test_pcm_wave_recorder_persists_complete_session_and_finalizes_idempotently(self):
        """整场录音不能受短窗裁剪影响，重复收尾也不能损坏 WAV 文件头。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            recording_path = Path(temp_dir) / "meeting-session.wav"
            recorder = Pcm16WaveRecorder(recording_path, sample_rate=1000)
            recorder.append(b"\x01\x00" * 600)
            recorder.append(b"\x02\x00" * 700)

            first_path = recorder.finalize()
            second_path = recorder.finalize()

            self.assertEqual(first_path, recording_path)
            self.assertEqual(second_path, recording_path)
            self.assertEqual(recorder.duration_ms, 1300)
            with wave.open(str(recording_path), "rb") as wav_file:
                self.assertEqual(wav_file.getframerate(), 1000)
                self.assertEqual(wav_file.getnframes(), 1300)

    def test_final_provider_event_uses_stable_anonymous_speaker_instead_of_generic_label(self):
        session = DashScopeRealtimeStreamSession(
            api_key="sk-test",
            base_url="https://dashscope.aliyuncs.com",
            model="qwen3-asr-flash-realtime",
            sample_rate=16000,
            language="zh",
            sensitive_words=[],
            on_event=lambda event: asyncio.sleep(0),
        )
        session.sent_audio_ms = 1200

        event = session._map_provider_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "第一位发言人的内容。",
                "start_ms": 200,
                "end_ms": 1000,
            }
        )

        self.assertEqual(event["segment"]["speakerName"], "发言人1")
        self.assertEqual(event["segment"]["speakerClusterId"], "speaker-1")

    async def test_start_sends_dashscope_qwen_asr_session_update_shape(self):
        """DashScope Qwen-ASR Realtime requires the documented session.update schema.

        The previous OpenAI-like shape used input_audio_sample_rate/input_audio_format=pcm16. DashScope's
        Qwen-ASR client-event reference documents input_audio_format=pcm, sample_rate, event_id, and recommends
        a low VAD threshold plus 400ms silence for faster meeting transcription.
        """

        provider = QueuedProviderWebSocket()
        connect_calls = []

        async def fake_connect(url, **kwargs):
            connect_calls.append({"url": url, "kwargs": kwargs})
            return provider

        old_connect = realtime_stream.websockets.connect
        realtime_stream.websockets.connect = fake_connect
        try:
            session = DashScopeRealtimeStreamSession(
                api_key="sk-test",
                base_url="https://dashscope.aliyuncs.com",
                model="qwen3-asr-flash-realtime",
                sample_rate=16000,
                language="zh",
                sensitive_words=[],
                on_event=lambda event: asyncio.sleep(0),
            )
            await session.start()
            await session.close()
        finally:
            realtime_stream.websockets.connect = old_connect

        frame = json.loads(provider.sent_messages[0])
        self.assertEqual(frame["type"], "session.update")
        self.assertTrue(frame.get("event_id", "").startswith("event_"))
        self.assertEqual(frame["session"]["modalities"], ["text"])
        self.assertEqual(frame["session"]["input_audio_format"], "pcm")
        self.assertEqual(frame["session"]["sample_rate"], 16000)
        self.assertEqual(frame["session"]["input_audio_transcription"]["language"], "zh")
        self.assertNotIn("model", frame["session"]["input_audio_transcription"])
        self.assertEqual(frame["session"]["turn_detection"]["type"], "server_vad")
        self.assertEqual(frame["session"]["turn_detection"]["threshold"], 0.0)
        self.assertEqual(frame["session"]["turn_detection"]["silence_duration_ms"], 1200)
        self.assertNotIn("prefix_padding_ms", frame["session"]["turn_detection"])
        self.assertIn("api-ws/v1/realtime?model=qwen3-asr-flash-realtime", connect_calls[0]["url"])
        self.assertEqual(connect_calls[0]["kwargs"]["additional_headers"]["OpenAI-Beta"], "realtime=v1")

    def test_silence_duration_uses_frontend_value_and_clamps_provider_range(self):
        """前端会议配置应进入 provider；越界值必须按 DashScope 允许范围钳制。"""

        normal = DashScopeRealtimeStreamSession(
            api_key="sk-test", base_url="https://dashscope.aliyuncs.com",
            model="qwen3-asr-flash-realtime", sample_rate=16000, language="zh",
            sensitive_words=[], on_event=lambda event: asyncio.sleep(0), silence_duration_ms=1800,
        )
        too_short = DashScopeRealtimeStreamSession(
            api_key="sk-test", base_url="https://dashscope.aliyuncs.com",
            model="qwen3-asr-flash-realtime", sample_rate=16000, language="zh",
            sensitive_words=[], on_event=lambda event: asyncio.sleep(0), silence_duration_ms=20,
        )
        too_long = DashScopeRealtimeStreamSession(
            api_key="sk-test", base_url="https://dashscope.aliyuncs.com",
            model="qwen3-asr-flash-realtime", sample_rate=16000, language="zh",
            sensitive_words=[], on_event=lambda event: asyncio.sleep(0), silence_duration_ms=9000,
        )

        self.assertEqual(normal._session_update_event()["session"]["turn_detection"]["silence_duration_ms"], 1800)
        self.assertEqual(too_short._session_update_event()["session"]["turn_detection"]["silence_duration_ms"], 200)
        self.assertEqual(too_long._session_update_event()["session"]["turn_detection"]["silence_duration_ms"], 6000)

    async def test_start_waits_until_provider_acknowledges_session_update(self):
        """Audio capture must not start before DashScope accepts the realtime session configuration.

        Without this handshake, browser PCM can arrive while the provider is still validating the session.
        Those leading frames are then lost, which presents as slow first text and missing sentence beginnings.
        """

        provider = QueuedProviderWebSocket(acknowledge_update=False)

        async def fake_connect(url, **kwargs):
            return provider

        old_connect = realtime_stream.websockets.connect
        realtime_stream.websockets.connect = fake_connect
        try:
            session = DashScopeRealtimeStreamSession(
                api_key="sk-test",
                base_url="https://dashscope.aliyuncs.com",
                model="qwen3-asr-flash-realtime",
                sample_rate=16000,
                language="zh",
                sensitive_words=[],
                on_event=lambda event: asyncio.sleep(0),
            )
            start_task = asyncio.create_task(session.start())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(start_task.done())

            await provider.incoming.put(json.dumps({"type": "session.updated"}))
            await asyncio.wait_for(start_task, timeout=1)
            await session.close()
        finally:
            realtime_stream.websockets.connect = old_connect

    async def test_stream_audio_and_finish_use_required_event_ids_and_session_finish(self):
        """Streaming audio frames and session end must use DashScope's event names.

        In VAD mode input_audio_buffer.commit is disabled; ending a session should send session.finish so the
        provider flushes the last detected utterance and then returns session.finished.
        """

        provider = FakeProviderWebSocket()
        session = DashScopeRealtimeStreamSession(
            api_key="sk-test",
            base_url="https://dashscope.aliyuncs.com",
            model="qwen3-asr-flash-realtime",
            sample_rate=16000,
            # Realtime meetings created by the old UI persist this display label. The session must
            # normalize it at the provider boundary just like offline import ASR does.
            language="中文普通话",
            sensitive_words=[],
            on_event=lambda event: asyncio.sleep(0),
        )
        session.websocket = provider
        self.assertEqual(session.language, "zh")

        await session.send_audio(b"\x01\x02\x03\x04")
        await session.finish()

        append_frame = json.loads(provider.sent_messages[0])
        finish_frame = json.loads(provider.sent_messages[1])
        self.assertEqual(append_frame["type"], "input_audio_buffer.append")
        self.assertTrue(append_frame.get("event_id", "").startswith("event_"))
        self.assertEqual(append_frame["audio"], "AQIDBA==")
        self.assertEqual(finish_frame["type"], "session.finish")
        self.assertTrue(finish_frame.get("event_id", "").startswith("event_"))
        self.assertTrue(provider.closed)

    async def test_finish_waits_for_last_transcript_and_session_finished_before_close(self):
        """Stopping realtime capture must preserve the provider's final buffered utterance.

        DashScope explicitly sends the last ``completed`` event after ``session.finish`` and only then emits
        ``session.finished``. Closing immediately after the client event cancels the receiver and drops the
        last sentence, so this test records the callback ordering as an externally visible contract.
        """

        received_events: list[dict] = []
        provider = QueuedProviderWebSocket(
            finish_events=[
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "这是停止前的最后一句。",
                },
                {"type": "session.finished"},
            ]
        )

        async def fake_connect(url, **kwargs):
            return provider

        async def collect_event(event: dict):
            received_events.append(event)

        old_connect = realtime_stream.websockets.connect
        realtime_stream.websockets.connect = fake_connect
        try:
            session = DashScopeRealtimeStreamSession(
                api_key="sk-test",
                base_url="https://dashscope.aliyuncs.com",
                model="qwen3-asr-flash-realtime",
                sample_rate=16000,
                language="zh",
                sensitive_words=[],
                on_event=collect_event,
            )
            await session.start()
            await session.send_audio(b"\x01\x00" * 1600)
            await session.finish()
        finally:
            realtime_stream.websockets.connect = old_connect

        transcript_events = [event for event in received_events if event.get("type") == "transcript"]
        self.assertEqual(transcript_events[0]["segment"]["text"], "这是停止前的最后一句。")
        self.assertTrue(provider.closed)

    def test_partial_text_uses_confirmed_text_plus_stash(self):
        """Qwen-ASR realtime preview text is text + stash, not either field alone."""

        session = DashScopeRealtimeStreamSession(
            api_key="sk-test",
            base_url="https://dashscope.aliyuncs.com",
            model="qwen3-asr-flash-realtime",
            sample_rate=16000,
            language="zh",
            sensitive_words=[],
            on_event=lambda event: asyncio.sleep(0),
        )

        event = session._map_provider_event(
            {
                "type": "conversation.item.input_audio_transcription.text",
                "text": "今天天气不错，",
                "stash": "阳光明媚",
            }
        )

        self.assertEqual(event["type"], "partial_transcript")
        self.assertEqual(event["text"], "今天天气不错，阳光明媚")

    def test_session_update_includes_bounded_context_corpus_for_domain_accuracy(self):
        """Meeting terminology should bias realtime recognition without an unbounded prompt."""

        session = DashScopeRealtimeStreamSession(
            api_key="sk-test",
            base_url="https://dashscope.aliyuncs.com",
            model="qwen3-asr-flash-realtime",
            sample_rate=16000,
            language="zh",
            sensitive_words=[],
            on_event=lambda event: asyncio.sleep(0),
            context_text="能源结构调整；全国政协；王忠" * 200,
        )

        config = session._session_update_event()["session"]["input_audio_transcription"]
        self.assertEqual(config["language"], "zh")
        self.assertIn("能源结构调整", config["corpus"]["text"])
        self.assertLessEqual(len(config["corpus"]["text"]), 1200)

    def test_factory_defaults_to_latest_qwen3_asr_realtime_snapshot(self):
        """Use the latest documented realtime snapshot instead of the older stable alias.

        DashScope currently maps the stable alias to the 2025-10-27 snapshot, while 2026-02-10 is the latest
        recognition snapshot. Pinning the newer model makes accuracy changes deliberate and reproducible.
        """

        old_value = os.environ.pop("DASHSCOPE_REALTIME_MODEL", None)
        try:
            session = create_realtime_stream_session(
                meeting_id="m-1",
                sample_rate=16000,
                language="zh",
                sensitive_words=[],
                on_event=lambda event: asyncio.sleep(0),
                context_text="测试会议；智能转写",
            )
        finally:
            if old_value is not None:
                os.environ["DASHSCOPE_REALTIME_MODEL"] = old_value

        self.assertEqual(session.model, "qwen3-asr-flash-realtime-2026-02-10")


if __name__ == "__main__":
    unittest.main()
