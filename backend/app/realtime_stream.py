"""Realtime ASR streaming bridge.

This module is intentionally separate from ``asr_gateway.py``. Offline/import ASR and realtime ASR have
different product requirements: import can wait for large stable files, while realtime must keep a long
WebSocket session open, forward tiny PCM frames, and surface interim text immediately. Keeping the bridge
isolated makes the old synchronous chunk path a fallback instead of the primary architecture.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import uuid
import wave
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets

from app.asr_language import normalize_asr_language



RealtimeEventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class Pcm16TimelineBuffer:
    """保留一段有界 PCM16 单声道时间线，并按 ASR 时间戳导出 WAV。

    DashScope 的 final 事件给出相对于本次实时会话的 ``startMs/endMs``。声纹模型需要文件路径，
    所以路由不能只保留最新一帧，也不能把整场录音反复送入模型。本缓冲按绝对采样帧计数：裁掉旧
    字节后时间原点仍保持不变，迟到的 final 事件要么精确取得仍在窗口内的声音，要么明确返回空。
    """

    _BYTES_PER_FRAME = 2

    def __init__(self, sample_rate: int, max_duration_ms: int = 120_000) -> None:
        self.sample_rate = max(1, int(sample_rate))
        self.max_frames = max(1, int(self.sample_rate * max_duration_ms / 1000))
        self._pcm = bytearray()
        self._total_frames = 0

    @property
    def available_start_ms(self) -> int:
        retained_frames = len(self._pcm) // self._BYTES_PER_FRAME
        start_frame = self._total_frames - retained_frames
        return int(start_frame * 1000 / self.sample_rate)

    @property
    def available_end_ms(self) -> int:
        return int(self._total_frames * 1000 / self.sample_rate)

    def append(self, pcm_bytes: bytes) -> None:
        """追加完整 PCM16 帧，并只裁掉超过窗口的最老音频。"""

        usable_length = len(pcm_bytes) - (len(pcm_bytes) % self._BYTES_PER_FRAME)
        if usable_length <= 0:
            return
        self._pcm.extend(pcm_bytes[:usable_length])
        self._total_frames += usable_length // self._BYTES_PER_FRAME
        retained_frames = len(self._pcm) // self._BYTES_PER_FRAME
        overflow_frames = max(0, retained_frames - self.max_frames)
        if overflow_frames:
            del self._pcm[: overflow_frames * self._BYTES_PER_FRAME]

    def extract_wav(self, start_ms: int, end_ms: int) -> bytes | None:
        """提取仍在缓冲中的半开区间 ``[start_ms, end_ms)``，并封装成标准 WAV。"""

        retained_frames = len(self._pcm) // self._BYTES_PER_FRAME
        available_start_frame = self._total_frames - retained_frames
        requested_start_frame = max(0, int(int(start_ms) * self.sample_rate / 1000))
        requested_end_frame = max(requested_start_frame, int(int(end_ms) * self.sample_rate / 1000))
        # 声纹向量对语音边界敏感。请求只要有一端落在保留窗口之外，就返回空而不是静默
        # 裁剪；残缺半句话可能属于另一位发言人，继续分析比暂时保留匿名名风险更大。
        if requested_start_frame < available_start_frame or requested_end_frame > self._total_frames:
            return None
        start_frame = requested_start_frame
        end_frame = requested_end_frame
        if end_frame <= start_frame:
            return None
        relative_start = (start_frame - available_start_frame) * self._BYTES_PER_FRAME
        relative_end = (end_frame - available_start_frame) * self._BYTES_PER_FRAME
        pcm_window = bytes(self._pcm[relative_start:relative_end])
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(self._BYTES_PER_FRAME)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm_window)
        return output.getvalue()


class Pcm16WaveRecorder:
    """把浏览器实时 PCM16 音频持续写成可回放、可重新分离的整场 WAV。

    ``Pcm16TimelineBuffer`` 只为短句声纹窗口保留最近两分钟，不能承担会议录音归档。
    本记录器直接使用 Python ``wave`` 写入标准文件头和音频帧；``finalize`` 幂等关闭
    单个文件并修正 RIFF 长度，因此会议结束后 3D-Speaker、播放器和重新转写都读取同一份
    原始时间线，不再依赖重启后会消失的内存缓冲。
    """

    _BYTES_PER_FRAME = 2

    def __init__(self, path: Path, sample_rate: int) -> None:
        self.path = Path(path)
        self.sample_rate = max(1, int(sample_rate))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._frame_count = 0
        self._closed = False
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(self._BYTES_PER_FRAME)
        self._wav.setframerate(self.sample_rate)

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def duration_ms(self) -> int:
        return int(self._frame_count * 1000 / self.sample_rate)

    def append(self, pcm_bytes: bytes) -> None:
        """只写入完整的 16 位采样帧；关闭后的迟到帧被安全忽略。"""

        if self._closed:
            return
        usable_length = len(pcm_bytes) - (len(pcm_bytes) % self._BYTES_PER_FRAME)
        if usable_length <= 0:
            return
        payload = pcm_bytes[:usable_length]
        self._wav.writeframesraw(payload)
        self._frame_count += usable_length // self._BYTES_PER_FRAME

    def finalize(self) -> Path | None:
        """关闭当前唯一 WAV 并返回路径；没有有效采样时返回 ``None``。"""

        if not self._closed:
            # ``writeframes`` 会在关闭前刷新 WAV 数据长度；空字节不会复制整场录音。
            self._wav.writeframes(b"")
            self._wav.close()
            self._closed = True
        return self.path if self._frame_count > 0 else None


class DashScopeRealtimeStreamSession:
    """Bridge one browser microphone session to DashScope's realtime WebSocket API.

    DashScope's realtime API follows the same broad shape as other market realtime transcription products:
    a persistent upstream WebSocket receives small PCM chunks, and server events provide interim/final text.
    The browser-facing FastAPI socket therefore should not wait for a finished WAV file before showing text.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        sample_rate: int,
        language: str,
        sensitive_words: list[str],
        on_event: RealtimeEventCallback,
        context_text: str = "",
        silence_duration_ms: int = 1200,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.sample_rate = sample_rate
        # Realtime meetings reuse the frozen meeting language. Legacy snapshots can contain the
        # Chinese UI label, so normalize here before building `session.update`; otherwise the same
        # invalid language enum that breaks import ASR also prevents a realtime session from starting.
        self.language = normalize_asr_language(language)
        self.sensitive_words = sensitive_words
        self.on_event = on_event
        # DashScope accepts a large corpus, but a short meeting-specific vocabulary is faster to validate and
        # less likely to bias ordinary words incorrectly. The caller provides title, selected dictionaries and
        # known speaker names; this bridge enforces a hard limit at the provider boundary.
        self.context_text = str(context_text or "").strip()[:1200]
        # DashScope 文档允许 200-6000ms。会议场景默认 1200ms，给自然停顿留出空间；同时
        # 在 provider 边界钳制，防止旧前端、手写 WebSocket 或异常配置导致会话创建失败。
        try:
            requested_silence_ms = int(silence_duration_ms)
        except (TypeError, ValueError):
            requested_silence_ms = 1200
        self.silence_duration_ms = min(6000, max(200, requested_silence_ms))
        self.websocket: Any | None = None
        self.receiver_task: asyncio.Task | None = None
        # A connected WebSocket is not yet a usable ASR session. DashScope validates ``session.update``
        # asynchronously and confirms it with ``session.updated``. These events keep browser audio from racing
        # that validation and preserve the final utterance until ``session.finished`` arrives.
        self.ready_event = asyncio.Event()
        self.finished_event = asyncio.Event()
        self.start_error: str = ""
        self.sent_audio_ms = 0
        self.next_segment_start_ms = 0

    @property
    def url(self) -> str:
        # DashScope lets the model be selected in the realtime URL. Keeping the URL configurable is important
        # for private-region deployments, but this default matches the public API shape used by Model Studio.
        explicit_url = os.getenv("DASHSCOPE_REALTIME_URL", "").strip()
        if explicit_url:
            return explicit_url
        if self.base_url.startswith("https://"):
            ws_base = "wss://" + self.base_url[len("https://") :]
        elif self.base_url.startswith("http://"):
            ws_base = "ws://" + self.base_url[len("http://") :]
        else:
            ws_base = self.base_url
        return f"{ws_base}/api-ws/v1/realtime?model={self.model}"

    async def start(self) -> None:
        """Open provider WebSocket and send the realtime session configuration."""

        if not self.api_key:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法启动 DashScope 实时流式识别")
        self.websocket = await websockets.connect(
            self.url,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
                # DashScope's realtime WebSocket examples require this protocol opt-in header. Without it the
                # connection can open while realtime client events are rejected or interpreted inconsistently.
                "OpenAI-Beta": "realtime=v1",
            },
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        )
        await self.websocket.send(json.dumps(self._session_update_event(), ensure_ascii=False))
        self.receiver_task = asyncio.create_task(self._receive_loop())
        try:
            # Waiting on the provider acknowledgement replaces the previous timing race. Five seconds is long
            # enough for normal network variance but short enough to return a useful startup error to the UI.
            await asyncio.wait_for(self.ready_event.wait(), timeout=5.0)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise RuntimeError("DashScope 实时会话初始化超时，未收到 session.updated") from exc
        if self.start_error:
            error_message = self.start_error
            await self.close()
            raise RuntimeError(error_message)

    def _session_update_event(self) -> dict[str, Any]:
        """Build provider session config with server VAD enabled.

        Server-side VAD/endpointing is the key difference from the legacy frontend fixed-window approach:
        the provider can emit partial text while audio is still arriving and decide final utterance boundaries
        from acoustic activity, which is how commercial realtime transcript UIs avoid long pauses.
        """

        transcription_config: dict[str, Any] = {
            # The ASR model is selected in the WebSocket URL. Qwen-ASR's session schema accepts language
            # and optional corpus text, but not a duplicate model field.
            "language": self.language,
        }
        if self.context_text:
            # Qwen-ASR's documented contextual-biasing field improves names, organizations and domain terms.
            # It remains advisory: the model still transcribes normal speech instead of forcing these words.
            transcription_config["corpus"] = {"text": self.context_text}

        return {
            "event_id": self._event_id(),
            "type": "session.update",
            "session": {
                # Qwen-ASR is a transcription-only session. Declaring the text modality avoids the provider
                # applying defaults intended for audio-generating realtime models.
                "modalities": ["text"],
                # DashScope Qwen-ASR Realtime names raw little-endian 16-bit PCM as "pcm".
                # The browser-to-backend contract still says PCM16 so frontend code is explicit about the
                # byte layout, but the provider schema must use this documented value or the upstream session
                # silently behaves poorly.
                "input_audio_format": "pcm",
                "sample_rate": self.sample_rate,
                "turn_detection": {
                    "type": "server_vad",
                    # 零阈值沿用 provider 推荐配置；断句静音则来自会议配置。1200ms 默认值比
                    # 400ms 更适合多人会议中的思考停顿，仍可由前端在合法范围内显式调整。
                    "threshold": 0.0,
                    "silence_duration_ms": self.silence_duration_ms,
                },
                "input_audio_transcription": transcription_config,
            },
        }

    def _event_id(self) -> str:
        """Return a provider event id for traceable realtime frames.

        DashScope's realtime protocol requires client events to carry an ``event_id``. Including one on every
        frame also gives us a stable breadcrumb when debugging "audio was sent but no text arrived" reports.
        """

        return f"event_{uuid.uuid4().hex}"

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Forward one browser PCM frame to DashScope.

        The browser sends little-endian 16-bit mono PCM. DashScope realtime expects the frame in a JSON event
        as base64 audio. We track approximate elapsed audio time locally so final events without timestamps can
        still be stored on the meeting timeline.
        """

        if not self.websocket:
            return
        await self.websocket.send(
            json.dumps(
                {
                    "event_id": self._event_id(),
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio_bytes).decode("ascii"),
                },
                ensure_ascii=False,
            )
        )
        bytes_per_second = max(1, self.sample_rate * 2)
        self.sent_audio_ms += int((len(audio_bytes) / bytes_per_second) * 1000)

    async def finish(self) -> None:
        """Ask provider to flush buffered speech before closing the session."""

        if self.websocket:
            try:
                # Server VAD sessions are closed with session.finish. The old commit event belongs to a manual
                # buffering flow; using it here can leave the final utterance unflushed or ignored by realtime ASR.
                await self.websocket.send(
                    json.dumps({"event_id": self._event_id(), "type": "session.finish"}, ensure_ascii=False)
                )
                if self.receiver_task:
                    try:
                        # DashScope emits the last ``completed`` transcript before ``session.finished``. Closing
                        # here immediately used to cancel the receive loop and lose that final sentence.
                        await asyncio.wait_for(self.finished_event.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        # A provider/network failure must not leave the browser stop action hanging forever. The
                        # already received transcript remains persisted and close() performs deterministic cleanup.
                        pass
            except Exception:
                pass
        await self.close()

    async def close(self) -> None:
        """Close provider WebSocket and stop the receiver task."""

        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
        if self.receiver_task:
            self.receiver_task.cancel()
            try:
                await self.receiver_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # 浏览器回调或网络边界可能已让 receiver 以异常结束。close 是幂等清理边界，
                # 不能把旧任务异常再次抛给 finish/WebSocket 路由并遮蔽已经持久化的文本。
                pass

    async def _receive_loop(self) -> None:
        """Translate provider events into browser-facing transcript events."""

        assert self.websocket is not None
        async for raw_message in self.websocket:
            try:
                payload = json.loads(raw_message)
            except (TypeError, json.JSONDecodeError):
                continue
            event_type = str(payload.get("type") or "")
            if event_type in {"session.created", "session.updated"}:
                # ``session.updated`` is the documented acknowledgement for our configuration. Accepting
                # ``session.created`` as a readiness signal keeps compatibility with gateways that only expose
                # the creation lifecycle event after applying the initial update.
                self.ready_event.set()
            elif event_type == "session.finished":
                self.finished_event.set()
            elif event_type == "error":
                error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                self.start_error = str(error.get("message") or payload.get("message") or "DashScope 实时会话异常")
                # Release both waiters so startup/finish can fail promptly instead of waiting for a timeout.
                self.ready_event.set()
                self.finished_event.set()
            event = self._map_provider_event(payload)
            if event:
                try:
                    await self.on_event(event)
                except Exception:
                    # 浏览器连接与 provider 连接生命周期不同。浏览器发送失败时继续消费
                    # provider 队列，尤其要等到最后 transcript 与 session.finished；主路由
                    # 的 finalizer 已在发送前持久化文本，因此回调异常不应杀死 receiver。
                    continue

    def _map_provider_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Normalize several compatible realtime event names into our frontend contract."""

        event_type = str(payload.get("type") or "")
        text = self._provider_text(payload, event_type=event_type)
        if not text:
            return None
        is_final = event_type.endswith(".completed") or event_type.endswith(".done") or event_type in {
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.input_audio_transcription.text.done",
        }
        is_partial = event_type.endswith(".delta") or event_type.endswith(".text") or event_type in {
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.text",
        }
        if is_partial and not is_final:
            return {
                "type": "partial_transcript",
                # Partial text is a transient source preview.  It is never persisted and must not
                # use the legacy flat rule list, whose scope cannot express a display-only policy.
                "text": text,
                "startMs": self.next_segment_start_ms,
                "endMs": self.sent_audio_ms,
            }
        if not is_final:
            return None
        start_ms = int(payload.get("start_ms") or payload.get("startMs") or self.next_segment_start_ms)
        end_ms = int(payload.get("end_ms") or payload.get("endMs") or max(start_ms + 300, self.sent_audio_ms))
        self.next_segment_start_ms = max(self.next_segment_start_ms, end_ms)
        return {
            "type": "transcript",
            "segment": {
                "id": f"rt-stream-{uuid.uuid4().hex[:10]}",
                # 文本必须先于后台声纹分析返回。这里先给出稳定匿名身份，随后主路由用
                # speaker_update 精确升级对应 segment；不再暴露含义模糊的“实时发言人”。
                "speakerName": "发言人1",
                "speakerClusterId": "speaker-1",
                "startMs": start_ms,
                "endMs": end_ms,
                # Preserve the provider result as both source fields.  ``main.py`` applies only
                # Task 3's frozen recognition replacements once this final event is persisted;
                # Task 4 masking remains a later target-specific consumer boundary.
                "rawText": text,
                "text": text,
                "language": self.language,
                "speakerSource": "dashscope_realtime",
            },
        }

    def _provider_text(self, payload: dict[str, Any], *, event_type: str) -> str:
        """Extract transcript text from DashScope-compatible event payloads.

        Qwen-ASR realtime preview events split the visible hypothesis into ``text`` (confirmed prefix) and
        ``stash`` (current unstable suffix). Showing only ``text`` makes the UI look frozen and then overwritten;
        showing ``text + stash`` matches commercial realtime transcript behavior where users see speech appear
        immediately while the tail is still being refined.
        """

        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        text = (
            payload.get("delta")
            or payload.get("text")
            or payload.get("transcript")
            or payload.get("content")
            or item.get("text")
            or item.get("transcript")
            or ""
        )
        stash = payload.get("stash") or item.get("stash") or ""
        if event_type == "conversation.item.input_audio_transcription.text":
            return f"{text}{stash}".strip()
        return str(text).strip()


def create_realtime_stream_session(
    *,
    meeting_id: str,
    sample_rate: int,
    language: str,
    sensitive_words: list[str],
    on_event: RealtimeEventCallback,
    context_text: str = "",
    silence_duration_ms: int = 1200,
) -> DashScopeRealtimeStreamSession:
    """Factory used by ``main.py`` and tests.

    The ``meeting_id`` parameter is reserved for future provider metadata and logging. Keeping it in the
    signature makes the test fake mirror the production contract exactly.
    """

    _ = meeting_id
    return DashScopeRealtimeStreamSession(
        api_key=os.getenv("DASHSCOPE_API_KEY", ""),
        base_url=os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com"),
        # Pin the newest documented snapshot. The unversioned stable alias currently points at the older
        # 2025-10-27 build, so an explicit snapshot makes recognition quality predictable across deployments.
        model=os.getenv("DASHSCOPE_REALTIME_MODEL", "qwen3-asr-flash-realtime-2026-02-10"),
        sample_rate=sample_rate,
        language=language,
        sensitive_words=sensitive_words,
        on_event=on_event,
        context_text=context_text,
        silence_duration_ms=silence_duration_ms,
    )
