"""ASR 模型网关。

本模块是智能会议系统调用语音识别模型的统一入口。业务层只关心
`transcribe_offline()` 和 `transcribe_realtime_chunk()`，不直接依赖 DashScope、
910B 本地服务或未来其他推理框架。这样后续把 Qwen3-ASR API 切成本地
Qwen3-ASR-1.7B 服务时，只需要替换本文件内部实现。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any, Callable

from app.asr_language import normalize_asr_language

from app.alignment_service import mock_align_text


MAIN_ASR_MODEL = "Qwen3-ASR-1.7B"
ALIGNMENT_MODEL = "Qwen3-ForcedAligner-0.6B"
VOICEPRINT_MODEL = "CAM++"


Urlopen = Callable[..., Any]


def _dashscope_vocabulary_corpus(hotwords: list[str] | None) -> str:
    """Serialize frozen terms once for the two DashScope offline context mechanisms.

    Qwen3 file transcription accepts ``parameters.corpus.text`` while the local synchronous
    OpenAI-compatible request accepts a system instruction. Both use the same newline-delimited
    corpus so a provider request cannot accidentally reorder, duplicate, or omit policy words.
    """

    terms: list[str] = []
    seen: set[str] = set()
    for value in hotwords or []:
        term = str(value or "").strip()
        if term and term not in seen:
            terms.append(term)
            seen.add(term)
    return "\n".join(terms)


def _request_json(urlopen: Urlopen, request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    """发送 HTTP 请求并解析 JSON。

    这里使用标准库 urllib，避免给当前后端额外增加 requests 依赖。单元测试可以
    注入假的 urlopen，从而验证请求 URL、请求体和 Header，而不触发真实网络访问。
    """

    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # DashScope 这类云端接口在 400/401/429 时会把真正原因放在响应体里。
        # urllib 默认异常只显示 “HTTP Error 400: Bad Request”，会让导入页只能看到 failed；
        # 这里主动读取响应体并包装成 RuntimeError，后续兜底转写和日志都能带上可排查的细节。
        error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        detail = error_body.strip() or str(exc)
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    return json.loads(body or "{}")


def _mime_type_for_file(file_path: str) -> str:
    """根据文件扩展名推断音频 MIME 类型。

    DashScope 同步 ASR 使用 data URI 上传本地小文件，MIME 类型不准确时会影响
    服务端解析，因此这里对常见会议音频格式做兜底。
    """

    guessed, _ = mimetypes.guess_type(file_path)
    return guessed or {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".wma": "audio/x-ms-wma",
        ".opus": "audio/opus",
    }.get(Path(file_path).suffix.lower(), "audio/mpeg")


def _probe_audio_duration_ms(file_path: str) -> int:
    """用 ffprobe 读取音频时长，失败时返回 0 让上层按文件大小兜底。

    DashScope 的同步 `qwen3-asr-flash` 更适合短音频，长会议音频需要先切片。
    这里不引入额外 Python 依赖，优先复用项目启动脚本已经依赖的 ffmpeg/ffprobe；
    如果部署机器暂时没有 ffprobe，也不能阻断转写主链路，因此返回 0 交给后续逻辑处理。
    """

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            completed = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return max(0, int(float(completed.stdout.strip() or "0") * 1000))
        except Exception:
            pass

    # Windows production hosts may have no ffprobe on PATH even though browser/realtime recordings
    # are standard PCM WAV files. The standard-library parser gives an exact frame-based duration,
    # preventing a 40-second file from being mistaken for a short clip solely because a utility is
    # absent. Compressed formats still return 0 and retain the existing byte-size fallback.
    if Path(file_path).suffix.lower() == ".wav":
        try:
            with wave.open(file_path, "rb") as wav_file:
                frame_rate = wav_file.getframerate()
                return max(0, int(wav_file.getnframes() * 1000 / frame_rate)) if frame_rate else 0
        except (OSError, EOFError, wave.Error):
            pass
    return 0


def _wav_requires_sync_normalization(file_path: str) -> bool:
    """Return whether a WAV must be converted before DashScope synchronous ASR.

    Browser capture and historical realtime files can be 44.1/48kHz even when their extension and
    MIME type are valid. DashScope may reject those data URIs as unsupported; the chunk cutter already
    produces the proven mono 16kHz 16-bit contract, so route incompatible WAVs through that path.
    Unreadable WAVs return false here and keep the existing provider error instead of hiding corruption.
    """

    if Path(file_path).suffix.lower() != ".wav":
        return False
    try:
        with wave.open(file_path, "rb") as wav_file:
            return wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2 or wav_file.getframerate() != 16000
    except (OSError, EOFError, wave.Error):
        return False


class MockQwenAsrGateway:
    """本地联调用 ASR 网关。

    该 mock 不伪装为真实识别，只生成稳定的说话人、时间戳、热词和敏感词替换
    结构，保证没有模型服务时前后端仍能跑通完整会议流程。
    """

    def transcribe_offline(
        self,
        meeting_id: str,
        file_id: str,
        enable_diarization: bool,
        hotwords: list[str],
        sensitive_words: list[str],
        start_ms: int | None = None,
        end_ms: int | None = None,
        file_path: str | None = None,
        file_url: str | None = None,
        language: str = "zh",
        enable_itn: bool = True,
    ) -> dict[str, Any]:
        """模拟离线文件转写。"""

        base_start = int(start_ms or 0)
        hotword_text = "、".join(hotwords[:3]) if hotwords else "智能会议"
        raw_segments = [
            {
                "speakerName": "张三" if enable_diarization else "发言人",
                "startMs": base_start + 1000,
                "endMs": base_start + 8200,
                "text": f"我们今天讨论{hotword_text}系统建设，重点是 ASR、声纹和纪要全流程。",
            },
            {
                "speakerName": "李四" if enable_diarization else "发言人",
                "startMs": base_start + 9000,
                "endMs": base_start + 16400,
                "text": "如果识别到糟糕这样的敏感词，后端需要按词库替换后再展示。",
            },
            {
                "speakerName": "王五" if enable_diarization else "发言人",
                "startMs": base_start + 17000,
                "endMs": min(int(end_ms or base_start + 25000), base_start + 24800),
                "text": "请信息中心完成模型网关联调，办公室负责整理会议纪要模板。",
            },
        ]

        segments = []
        for index, item in enumerate(raw_segments, start=1):
            # A gateway returns provider source text only.  ``sensitive_words`` remains an accepted
            # legacy parameter so external adapters do not break, but production ingestion must not
            # apply it here: display/AI/export masking has different scopes and runs after storage.
            raw_text = item["text"]
            text = raw_text
            segments.append(
                {
                    "id": f"seg-{file_id}-{index}",
                    "meetingId": meeting_id,
                    "fileId": file_id,
                    "speakerName": item["speakerName"],
                    "startMs": item["startMs"],
                    "endMs": item["endMs"],
                    "text": text,
                    "rawText": raw_text,
                    "language": language,
                    "words": mock_align_text(text, start_ms=item["startMs"]),
                }
            )

        return {
            "model": MAIN_ASR_MODEL,
            "alignmentModel": ALIGNMENT_MODEL,
            "voiceprintModel": VOICEPRINT_MODEL if enable_diarization else "",
            "fileId": file_id,
            "meetingId": meeting_id,
            "status": "completed",
            "segments": segments,
            "createdAt": int(time.time()),
        }

    def transcribe_realtime_chunk(
        self,
        meeting_id: str,
        chunk_index: int,
        audio_chunk: bytes,
        sensitive_words: list[str],
        mime_type: str = "audio/wav",
        duration_ms: int = 3000,
        context_text: str = "",
    ) -> dict[str, Any]:
        """模拟实时音频块识别。"""

        speaker = ["张三", "李四", "王五"][chunk_index % 3]
        raw_text = f"第 {chunk_index + 1} 段实时发言已由 Qwen3-ASR 网关转写。"
        # See ``transcribe_offline``: realtime gateway output is source text, not a display view.
        text = raw_text
        # mock 也使用前端传入的分片时长，避免测试环境和真实环境的时间轴表现不一致。
        start_ms = chunk_index * duration_ms
        return {
            "type": "transcript",
            "meetingId": meeting_id,
            "segment": {
                "id": f"rt-{meeting_id}-{chunk_index}",
                "speakerName": speaker,
                "startMs": start_ms,
                "endMs": start_ms + duration_ms,
                "text": text,
                "rawText": raw_text,
                "language": "zh",
                "words": mock_align_text(text, start_ms=start_ms),
            },
        }


class DashScopeAsrGateway(MockQwenAsrGateway):
    """DashScope/百炼 Qwen3-ASR API 网关。

    本地测试阶段先复用 `E:\\work\\my-todo\\asr_test` 中验证过的调用方式：
    - 本地小文件或实时 VAD 分片：`qwen3-asr-flash` 同步接口。
    - 可访问 URL 的长音频：`qwen3-asr-flash-filetrans` 异步文件转写接口。

    API Key 只从环境变量或构造参数读取，严禁写死在代码中。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        sync_model: str | None = None,
        filetrans_model: str | None = None,
        urlopen: Urlopen = urllib.request.urlopen,
    ):
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.base_url = (base_url or os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com")).rstrip("/")
        self.sync_model = sync_model or os.getenv("DASHSCOPE_SYNC_MODEL", "qwen3-asr-flash")
        self.filetrans_model = filetrans_model or os.getenv("DASHSCOPE_FILETRANS_MODEL", "qwen3-asr-flash-filetrans")
        self.urlopen = urlopen
        # 百炼同步 ASR 单次请求更适合短音频；长会议录音如果整段上传会触发
        # “The audio is too long” 这类 400 错误。以下阈值统一控制本地切片策略，
        # 现场如果百炼侧限制变化，只需要改环境变量，不需要改业务代码。
        self.sync_chunk_seconds = max(5, int(os.getenv("DASHSCOPE_SYNC_CHUNK_SECONDS", "25") or "25"))
        self.sync_single_max_seconds = max(
            self.sync_chunk_seconds,
            # Keep direct synchronous input inside the same proven boundary as VAD/fixed chunks.
            # The provider may reject 40-50 second WAV data URIs as unsupported even though they are
            # below older documented duration limits; deterministic splitting is slower but reliable.
            int(os.getenv("DASHSCOPE_SYNC_SINGLE_MAX_SECONDS", str(self.sync_chunk_seconds)) or str(self.sync_chunk_seconds)),
        )
        self.sync_single_max_bytes = max(
            512 * 1024,
            int(os.getenv("DASHSCOPE_SYNC_SINGLE_MAX_BYTES", str(7 * 1024 * 1024)) or str(7 * 1024 * 1024)),
        )
        # 长录音会被切成很多同步 ASR 分片，现场网络里偶尔会出现 10053/timeout/连接重置。
        # 这些瞬断不是音频不可识别，应该重试当前分片，而不是让整条导入掉到假转写。
        self.sync_request_max_retries = max(1, int(os.getenv("DASHSCOPE_SYNC_REQUEST_MAX_RETRIES", "3") or "3"))
        # 实时会议和离线导入的等待策略必须分开：离线导入可以为稳定性多等一会儿，实时会议如果
        # 沿用 300 秒超时和多轮重试，用户会看到按钮一直“识别中”却没有文字。这里给实时分片
        # 单独设置较短超时和默认 1 次尝试，失败就尽快把明确错误返回前端，避免长时间假死。
        self.realtime_request_timeout_seconds = max(5, int(os.getenv("DASHSCOPE_REALTIME_REQUEST_TIMEOUT_SECONDS", "25") or "25"))
        self.realtime_request_max_retries = max(1, int(os.getenv("DASHSCOPE_REALTIME_REQUEST_MAX_RETRIES", "1") or "1"))

    @property
    def sync_url(self) -> str:
        return f"{self.base_url}/compatible-mode/v1/chat/completions"

    @property
    def filetrans_url(self) -> str:
        return f"{self.base_url}/api/v1/services/audio/asr/transcription"

    @property
    def task_query_url(self) -> str:
        return f"{self.base_url}/api/v1/tasks"

    def _headers(self, async_request: bool = False) -> dict[str, str]:
        """构造 DashScope 请求头。"""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if async_request:
            headers["X-DashScope-Async"] = "enable"
        return headers

    def _build_segment_from_text(
        self,
        meeting_id: str,
        file_id: str,
        text: str,
        sensitive_words: list[str],
        language: str,
        index: int = 1,
        start_ms: int = 0,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """把 DashScope 返回的纯文本包装成系统统一的转写片段结构。"""

        raw_text = text.strip()
        # DashScope source text must remain reversible for Task 3 normalization and Task 4 targets.
        safe_text = raw_text
        resolved_end_ms = end_ms if end_ms is not None else start_ms + max(1000, len(safe_text) * 240)
        return [
            {
                "id": f"seg-{file_id}-dashscope-{index}",
                "meetingId": meeting_id,
                "fileId": file_id,
                "speakerName": "待匹配发言人",
                "startMs": start_ms,
                "endMs": resolved_end_ms,
                "text": safe_text,
                "rawText": raw_text,
                "language": language,
                "words": mock_align_text(safe_text, start_ms=start_ms),
            }
        ]

    def _transcribe_audio_bytes(
        self,
        audio_data: bytes,
        mime_type: str,
        language: str,
        enable_itn: bool,
        context_text: str = "",
        hotwords: list[str] | None = None,
        timeout_seconds: int = 300,
    ) -> str:
        """调用 qwen3-asr-flash 识别内存中的音频字节。

        离线导入和实时会议都会走这段统一逻辑：离线导入传入文件字节，实时会议传入浏览器编码好的
        WAV 分片。统一封装可以保证请求体格式、语言参数、ITN 开关和错误处理完全一致。
        """

        data_uri = f"data:{mime_type};base64,{base64.b64encode(audio_data).decode('utf-8')}"
        # Qwen3-ASR's dedicated OpenAI-compatible task accepts exactly one user audio item. Although
        # the endpoint resembles chat completions, extra system text, hotword prompts, or a second
        # context item makes the service return ``dedicated task asr ... does not support this input``.
        # Filetrans still uses its documented corpus field; native realtime context is handled by the
        # realtime WebSocket session. Keep these compatibility parameters accepted but never inject
        # them into unsupported synchronous provider grammar.
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": data_uri},
                    }
                ],
            }
        ]
        payload: dict[str, Any] = {
            "model": self.sync_model,
            "messages": messages,
            "stream": False,
            "asr_options": {"enable_itn": enable_itn},
        }
        # Browser forms and historical meeting snapshots may contain labels such as ``中文普通话``.
        # DashScope accepts only provider codes, so normalize at this final boundary even when the
        # current frontend already submits `zh/en/auto`. Keeping the guard here also protects API
        # callers and old persisted records from the same invalid-parameter failure.
        provider_language = normalize_asr_language(language)
        if provider_language != "auto":
            payload["asr_options"]["language"] = provider_language

        request = urllib.request.Request(
            self.sync_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        result = _request_json(self.urlopen, request, timeout=timeout_seconds)
        return str(result.get("choices", [{}])[0].get("message", {}).get("content", ""))

    def _is_transient_asr_error(self, exc: Exception) -> bool:
        """判断 DashScope 同步 ASR 错误是否适合原分片重试。

        只重试连接中断、超时、连接重置这类传输层问题；鉴权、参数错误、音频无效等业务错误继续抛出，
        否则会把真正的配置问题拖慢几轮后才暴露。
        """

        lowered = str(exc).lower()
        transient_markers = [
            "10053",
            "10054",
            "connection reset",
            "connection aborted",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "远程主机强迫关闭",
            "软件中止了一个已建立的连接",
        ]
        return any(marker in lowered for marker in transient_markers)

    def _transcribe_audio_bytes_with_retry(
        self,
        audio_data: bytes,
        mime_type: str,
        language: str,
        enable_itn: bool,
        context: str,
        context_text: str = "",
        hotwords: list[str] | None = None,
        timeout_seconds: int = 300,
        max_retries: int | None = None,
    ) -> str:
        """带瞬断重试的同步 ASR 调用。

        `context` 只用于错误信息，帮助定位是离线整文件、某个切片还是实时分片失败。
        重试间隔很短，目标是覆盖偶发网络抖动；如果连续失败，仍把最后一次真实错误交给上层展示。
        """

        last_error: Exception | None = None
        attempts_made = 0
        retry_limit = max(1, max_retries or self.sync_request_max_retries)
        for attempt in range(1, retry_limit + 1):
            # 单独记录实际发出的请求次数。参数错误等确定性失败会在第一次请求后立即停止，
            # 不能用配置的上限冒充真实重试次数，否则台账会向用户显示误导性的“已重试 3 次”。
            attempts_made = attempt
            try:
                return self._transcribe_audio_bytes(
                    audio_data,
                    mime_type,
                    language,
                    enable_itn,
                    context_text=context_text,
                    hotwords=hotwords,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - 这里要统一判断 urllib/RuntimeError 包装后的传输错误。
                last_error = exc
                if attempt >= retry_limit or not self._is_transient_asr_error(exc):
                    break
                time.sleep(min(2.0, 0.4 * attempt))

        # 首次即失败时直接展示供应商原因；只有确实发生过重试时才报告总尝试次数。
        # 使用“共尝试”而不是“已重试”，避免把首次请求也错误计入重试次数。
        if attempts_made <= 1:
            raise RuntimeError(f"{context}失败：{last_error}") from last_error
        raise RuntimeError(f"{context}失败，共尝试 {attempts_made} 次：{last_error}") from last_error

    def _sync_transcribe_file(
        self,
        file_path: str,
        language: str,
        enable_itn: bool,
        hotwords: list[str] | None = None,
    ) -> str:
        """调用 qwen3-asr-flash 同步识别本地小音频文件。"""

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"音频文件不存在：{file_path}")

        return self._transcribe_audio_bytes_with_retry(
            path.read_bytes(),
            _mime_type_for_file(file_path),
            language,
            enable_itn,
            context=f"DashScope 同步识别 {path.name}",
            hotwords=hotwords,
        )

    def _split_audio_for_sync_asr_fixed(self, file_path: str) -> list[tuple[Path, int, int]]:
        """把长会议音频切成同步 ASR 能稳定接受的短 WAV。

        政企内网导入的音频通常只有本地磁盘路径，没有可被百炼 filetrans 访问的公网 URL；
        因此不能只依赖 filetrans。这里用 ffmpeg 生成临时 16k 单声道 WAV 切片，然后逐段调用
        `qwen3-asr-flash`，既保留真实 ASR，又避免长音频 400。
        """

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("本地长音频需要 ffmpeg 切片，但当前环境未找到 ffmpeg")

        temp_dir = Path(tempfile.mkdtemp(prefix="meeting-asr-chunks-"))
        output_pattern = temp_dir / "chunk-%04d.wav"
        command = [
            ffmpeg,
            "-y",
            "-i",
            file_path,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "segment",
            "-segment_time",
            str(self.sync_chunk_seconds),
            "-reset_timestamps",
            "1",
            str(output_pattern),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            detail = (exc.stderr or exc.stdout or "").strip()
            raise RuntimeError(f"ffmpeg 音频切片失败：{detail}") from exc

        chunks: list[tuple[Path, int, int]] = []
        for index, chunk_path in enumerate(sorted(temp_dir.glob("chunk-*.wav"))):
            start_ms = index * self.sync_chunk_seconds * 1000
            duration_ms = _probe_audio_duration_ms(str(chunk_path)) or self.sync_chunk_seconds * 1000
            chunks.append((chunk_path, start_ms, start_ms + duration_ms))
        if not chunks:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError("ffmpeg 未生成可识别的音频切片")
        return chunks

    def _speech_windows_from_vad_segments(
        self,
        vad_segments: list[dict[str, Any]],
        duration_ms: int,
        merge_gap_ms: int = 500,
        padding_ms: int = 300,
        max_segment_ms: int = 30000,
    ) -> list[tuple[int, int]]:
        """Convert raw VAD speech spans into ASR windows.

        The VAD service gives acoustic speech spans, but ASR wants slightly wider chunks: close spans are
        merged so a tiny pause does not split a phrase, each side receives padding to protect boundary words,
        and very long speech is capped so DashScope's sync endpoint still receives manageable requests.
        """

        normalized: list[tuple[int, int]] = []
        for segment in vad_segments or []:
            start = int(segment.get("start_ms", segment.get("startMs", 0)) or 0)
            end = int(segment.get("end_ms", segment.get("endMs", 0)) or 0)
            if end > start:
                normalized.append((max(0, start), min(duration_ms or end, end)))
        if not normalized:
            return []
        normalized.sort()
        merged: list[tuple[int, int]] = []
        for start, end in normalized:
            if not merged or start - merged[-1][1] > merge_gap_ms:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))

        windows: list[tuple[int, int]] = []
        for start, end in merged:
            padded_start = max(0, start - padding_ms)
            padded_end = min(duration_ms or end + padding_ms, end + padding_ms)
            cursor = padded_start
            while cursor < padded_end:
                next_end = min(padded_end, cursor + max_segment_ms)
                if next_end > cursor:
                    windows.append((cursor, next_end))
                cursor = next_end
        return windows

    def _cut_sync_asr_windows(self, file_path: str, windows: list[tuple[int, int]]) -> list[tuple[Path, int, int]]:
        """Materialize VAD windows as temporary WAV files consumed by the existing sync ASR pipeline."""

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("本地长音频需要 ffmpeg 裁剪 VAD 分段，但当前环境未找到 ffmpeg")
        temp_dir = Path(tempfile.mkdtemp(prefix="meeting-asr-vad-chunks-"))
        chunks: list[tuple[Path, int, int]] = []
        try:
            for index, (start_ms, end_ms) in enumerate(windows):
                output_path = temp_dir / f"chunk-{index:04d}.wav"
                command = [
                    ffmpeg,
                    "-y",
                    "-ss",
                    f"{start_ms / 1000:.3f}",
                    "-to",
                    f"{end_ms / 1000:.3f}",
                    "-i",
                    file_path,
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(output_path),
                ]
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
                chunks.append((output_path, start_ms, end_ms))
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        return chunks

    def _split_audio_for_sync_asr_with_vad(self, file_path: str) -> list[tuple[Path, int, int]]:
        """Prefer VAD-based offline segmentation so imported recordings are split at speech boundaries."""

        vad_base_url = os.getenv("VAD_GATEWAY_BASE_URL", "").strip()
        if not vad_base_url:
            raise RuntimeError("VAD_GATEWAY_BASE_URL 未配置")
        from app.model_clients import LocalVadClient

        duration_ms = _probe_audio_duration_ms(file_path)
        result = LocalVadClient(vad_base_url).split(
            audio_path=file_path,
            min_speech_ms=200,
            max_segment_ms=30000,
        )
        raw_segments = result.get("segments") or result.get("speech_segments") or result.get("items") or []
        windows = self._speech_windows_from_vad_segments(raw_segments, duration_ms)
        if not windows:
            raise RuntimeError("VAD 未返回可用语音片段")
        return self._cut_sync_asr_windows(file_path, windows)

    def _split_audio_for_sync_asr(self, file_path: str) -> list[tuple[Path, int, int]]:
        """Split imported audio by VAD first, then fall back to the legacy fixed ffmpeg windows."""

        self.last_split_strategy_message = ""
        try:
            return self._split_audio_for_sync_asr_with_vad(file_path)
        except Exception as exc:
            # VAD is an optimization layer, not a hard dependency for importing files. Keep the previous fixed
            # chunk strategy as a compatibility fallback and expose the reason to the API result for diagnosis.
            self.last_split_strategy_message = f"VAD 不可用，已使用固定切片兜底：{exc}"
            return self._split_audio_for_sync_asr_fixed(file_path)

    def _sync_transcribe_file_segments(
        self,
        meeting_id: str,
        file_id: str,
        file_path: str,
        language: str,
        enable_itn: bool,
        sensitive_words: list[str],
        hotwords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """识别本地上传文件，短音频直传，长音频自动切片。

        这层是导入转写稳定性的核心：用户上传 5 分钟、30 分钟会议录音时，前端仍然得到
        真实 ASR 文本，而不是因为同步接口超长被迫降级成本地假片段。
        """

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"音频文件不存在：{file_path}")

        duration_ms = _probe_audio_duration_ms(file_path)
        # Splitting is also the normalization path: every generated chunk is mono 16kHz PCM WAV.
        # Therefore a short 48kHz browser recording must take this branch even when duration/size
        # alone would permit direct upload.
        should_split = (
            path.stat().st_size > self.sync_single_max_bytes
            or duration_ms > self.sync_single_max_seconds * 1000
            or _wav_requires_sync_normalization(file_path)
        )
        if not should_split:
            try:
                text = self._sync_transcribe_file(file_path, language, enable_itn, hotwords)
                return self._build_segment_from_text(
                    meeting_id,
                    file_id,
                    text,
                    sensitive_words,
                    language,
                    index=1,
                    start_ms=0,
                    end_ms=duration_ms or None,
                )
            except RuntimeError as exc:
                # 某些编码无法准确探测时长，但云端仍会判定超长；只对这类明确超长错误切片重试。
                # 鉴权、余额、网络等错误继续抛给上层，避免把真实部署问题误报成识别成功。
                lowered = str(exc).lower()
                if "too long" not in lowered and "audio length" not in lowered:
                    raise

        chunks = self._split_audio_for_sync_asr(file_path)
        temp_root = chunks[0][0].parent
        segments: list[dict[str, Any]] = []
        try:
            for index, (chunk_path, start_ms, end_ms) in enumerate(chunks, start=1):
                text = self._transcribe_audio_bytes_with_retry(
                    chunk_path.read_bytes(),
                    "audio/wav",
                    language,
                    enable_itn,
                    context=f"DashScope 分片 {index}",
                    hotwords=hotwords,
                ).strip()
                if not text:
                    continue
                segments.extend(
                    self._build_segment_from_text(
                        meeting_id,
                        file_id,
                        text,
                        sensitive_words,
                        language,
                        index=index,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                )
        finally:
            # 切片文件只服务本次请求，识别完立即清掉，避免批量导入长会议时把磁盘写满。
            shutil.rmtree(temp_root, ignore_errors=True)
        if not segments:
            raise RuntimeError("DashScope ASR 未返回有效转写文本")
        return segments

    def transcribe_realtime_chunk(
        self,
        meeting_id: str,
        chunk_index: int,
        audio_chunk: bytes,
        sensitive_words: list[str],
        mime_type: str = "audio/wav",
        duration_ms: int = 3000,
        context_text: str = "",
    ) -> dict[str, Any]:
        """调用 DashScope 识别实时会议音频分片。

        前端会把麦克风 PCM 编码成短 WAV 分片后通过 WebSocket 发来。这里不再复用 mock 文案，
        而是把每个分片作为一次 qwen3-asr-flash 同步识别请求；如果分片没有可识别语音，返回
        `type=status`，前端只更新状态，不写入假转写文本。
        """

        if not self.api_key:
            return {"type": "error", "meetingId": meeting_id, "message": "未配置 DASHSCOPE_API_KEY，无法实时转写"}

        text = self._transcribe_audio_bytes_with_retry(
            audio_chunk,
            mime_type or "audio/wav",
            "zh",
            True,
            context=f"DashScope 实时分片 {chunk_index + 1}",
            context_text=context_text,
            timeout_seconds=self.realtime_request_timeout_seconds,
            max_retries=self.realtime_request_max_retries,
        ).strip()
        if not text:
            return {
                "type": "status",
                "code": "asr_empty",
                "meetingId": meeting_id,
                "message": "当前音频分片未识别到有效语音",
            }
        raw_text = text
        # Do not apply the legacy flat list to a final realtime provider result.  The route later
        # stores this text unchanged except for Task 3's explicit, audited recognition replacement.
        safe_text = raw_text
        # 前端现在会按更长窗口发送音频，减少半句话被切断的问题；后端按配置时长生成时间轴。
        duration_ms = max(1000, int(duration_ms or 3000))
        start_ms = chunk_index * duration_ms
        return {
            "type": "transcript",
            "meetingId": meeting_id,
            "segment": {
                "id": f"rt-{meeting_id}-{chunk_index}",
                "speakerName": "实时发言人",
                "startMs": start_ms,
                "endMs": start_ms + duration_ms,
                "text": safe_text,
                "rawText": raw_text,
                "language": "zh",
                "words": mock_align_text(safe_text, start_ms=start_ms),
            },
        }

    def _filetrans_transcribe_url(
        self,
        file_url: str,
        language: str,
        enable_itn: bool,
        hotwords: list[str] | None = None,
        poll_interval: float = 2.0,
        max_polls: int = 180,
    ) -> str:
        """调用 qwen3-asr-flash-filetrans 异步识别可访问 URL。

        DashScope 文件转写要求音频文件可被模型服务访问，因此政企内网部署时通常
        需要额外提供 Nginx/MinIO/对象存储的临时 URL。没有 URL 时，业务层会改走
        本地分片同步识别或提示配置文件访问服务。
        """

        payload: dict[str, Any] = {
            "model": self.filetrans_model,
            "input": {"file_url": file_url},
            "parameters": {"channel_id": [0], "enable_itn": enable_itn},
        }
        if language and language != "auto":
            payload["parameters"]["language"] = language
        vocabulary_corpus = _dashscope_vocabulary_corpus(hotwords)
        if vocabulary_corpus:
            # Filetrans accepts contextual vocabulary as a plain corpus text object. Passing this
            # exact frozen term list prevents the URL path from silently losing import hotwords.
            payload["parameters"]["corpus"] = {"text": vocabulary_corpus}

        submit_request = urllib.request.Request(
            self.filetrans_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(async_request=True),
            method="POST",
        )
        submit_result = _request_json(self.urlopen, submit_request, timeout=30)
        task_id = submit_result.get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"DashScope 文件转写任务提交失败：{submit_result}")

        for _ in range(max_polls):
            poll_request = urllib.request.Request(
                f"{self.task_query_url}/{task_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                method="GET",
            )
            poll_result = _request_json(self.urlopen, poll_request, timeout=30)
            output = poll_result.get("output", {})
            status = output.get("task_status")
            if status == "SUCCEEDED":
                return self._extract_filetrans_text(output)
            if status == "FAILED":
                raise RuntimeError(output.get("message", "DashScope 文件转写失败"))
            time.sleep(poll_interval)
        raise TimeoutError("DashScope 文件转写轮询超时")

    def _extract_filetrans_text(self, output: dict[str, Any]) -> str:
        """从文件转写结果中提取文本。

        DashScope 可能返回 transcription_url，也可能返回直接文本。下载
        transcription_url 仍使用同一个 urlopen，便于测试和内网代理统一管理。
        """

        if output.get("text"):
            return str(output["text"])

        texts: list[str] = []
        for item in output.get("results", []) or []:
            url = item.get("transcription_url")
            if not url:
                continue
            request = urllib.request.Request(url, method="GET")
            result = _request_json(self.urlopen, request, timeout=60)
            transcripts = result.get("transcripts", [result]) if isinstance(result, dict) else result
            for transcript in transcripts:
                if isinstance(transcript, dict) and transcript.get("text"):
                    texts.append(str(transcript["text"]))
        return "\n".join(texts)

    def transcribe_offline(
        self,
        meeting_id: str,
        file_id: str,
        enable_diarization: bool,
        hotwords: list[str],
        sensitive_words: list[str],
        start_ms: int | None = None,
        end_ms: int | None = None,
        file_path: str | None = None,
        file_url: str | None = None,
        language: str = "zh",
        enable_itn: bool = True,
    ) -> dict[str, Any]:
        """调用 DashScope 完成离线转写。"""

        if not self.api_key:
            return {
                "status": "waiting_model_config",
                "model": self.sync_model,
                "fileId": file_id,
                "meetingId": meeting_id,
                "segments": [],
                "message": "未配置 DASHSCOPE_API_KEY，无法调用 Qwen3-ASR API。",
            }

        if file_url:
            text = self._filetrans_transcribe_url(file_url, language, enable_itn, hotwords=hotwords)
            model_name = self.filetrans_model
            segments = self._build_segment_from_text(
                meeting_id,
                file_id,
                text,
                sensitive_words,
                language,
            )
        elif file_path:
            segments = self._sync_transcribe_file_segments(
                meeting_id,
                file_id,
                file_path,
                language,
                enable_itn,
                sensitive_words,
                hotwords,
            )
            model_name = self.sync_model
        else:
            raise ValueError("DashScope ASR 需要 file_path 或 file_url")

        result = {
            "model": model_name,
            "alignmentModel": ALIGNMENT_MODEL,
            "voiceprintModel": VOICEPRINT_MODEL if enable_diarization else "",
            "fileId": file_id,
            "meetingId": meeting_id,
            "status": "completed",
            "segments": segments,
            "createdAt": int(time.time()),
        }
        if getattr(self, "last_split_strategy_message", ""):
            # Surface fallback information to import jobs without failing the request; users still get the
            # transcript, and operators can see that fixed chunking was used because VAD was unavailable.
            result["message"] = self.last_split_strategy_message
        return result


class RemoteQwenAsrGateway(MockQwenAsrGateway):
    """自部署 Qwen3-ASR 服务网关。

    这个模式面向后续 910B 或本地 GPU 服务，约定服务暴露
    `POST /v1/asr/transcribe`。如果以后完全离线部署，只需要把
    `ASR_GATEWAY_MODE=remote` 并设置 `ASR_GATEWAY_BASE_URL`。
    """

    def __init__(self, base_url: str, urlopen: Urlopen = urllib.request.urlopen):
        self.base_url = base_url.rstrip("/")
        self.urlopen = urlopen

    def transcribe_offline(
        self,
        meeting_id: str,
        file_id: str,
        enable_diarization: bool,
        hotwords: list[str],
        sensitive_words: list[str],
        start_ms: int | None = None,
        end_ms: int | None = None,
        file_path: str | None = None,
        file_url: str | None = None,
        language: str = "zh",
        enable_itn: bool = True,
    ) -> dict[str, Any]:
        payload = {
            "model": MAIN_ASR_MODEL,
            "meeting_id": meeting_id,
            "file_id": file_id,
            "file_path": file_path,
            "file_url": file_url,
            "enable_diarization": enable_diarization,
            "hotwords": hotwords,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "language": language,
            "enable_itn": enable_itn,
        }
        request = urllib.request.Request(
            f"{self.base_url}/v1/asr/transcribe",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        result = _request_json(self.urlopen, request, timeout=300)

        # 敏感词属于业务展示规则，即使远程模型没有处理，也在网关出口统一兜底。
        for segment in result.get("segments", []):
            raw_text = str(segment.get("rawText") or segment.get("text") or "")
            segment["rawText"] = raw_text
            # External gateway adapters may return only ``text``.  Normalize that into the source
            # pair without reintroducing legacy masking, so Task 3 remains the sole ingestion-time
            # text transformation and Task 4 can independently mask detached consumer views.
            segment["text"] = raw_text
        result.setdefault("model", MAIN_ASR_MODEL)
        return result


def create_asr_gateway(mode: str = "mock", base_url: str = "") -> MockQwenAsrGateway:
    """根据配置创建 ASR 网关实例。"""

    normalized_mode = (mode or "mock").lower()
    if normalized_mode == "remote" and base_url:
        return RemoteQwenAsrGateway(base_url)
    if normalized_mode == "dashscope":
        return DashScopeAsrGateway()
    return MockQwenAsrGateway()
