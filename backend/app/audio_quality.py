"""实时音频质量门控。

浏览器实时转写会持续发送短 WAV 分片。这里不做完整 VAD，只做一层“明显静音/底噪”
防线：纯静音不进入 ASR，避免模型幻听；安静环境里的正常人声要放行，避免用户看到
“识别中”却没有任何文本。函数同时返回诊断指标，前端可以把问题显示成“麦克风音量偏低”
而不是误报成“实时转写已暂停”。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import struct
import wave
from io import BytesIO


# 这些阈值按 16-bit PCM 整数幅度计算。原先峰值门限接近 650，安静会议室里离麦克风稍远
# 的人声容易刚好被拦掉；这里降低基础门限，只拦截纯静音和非常轻微的底噪。
REALTIME_MIN_RMS_INT16 = 80
REALTIME_MIN_PEAK_INT16 = 320
REALTIME_ACTIVE_SAMPLE_LEVEL_INT16 = 180
REALTIME_MIN_ACTIVE_RATIO = 0.002
REALTIME_MIN_DURATION_MS = 1200


@dataclass(frozen=True)
class AudioQualityResult:
    """实时音频质量诊断结果。

    `has_voice` 是业务决策字段；其余指标用于 WebSocket status 和日志排查。RMS 反映整段
    音量，peak 反映瞬时峰值，active_ratio 反映超过有效声门限的采样比例。
    """

    has_voice: bool
    rms: float
    peak: int
    active_ratio: float
    duration_ms: int
    reason: str

    def to_status_payload(self) -> dict[str, float | int | str | bool]:
        """转换成可 JSON 序列化的 status 字段，避免路由层了解 dataclass 细节。"""

        return asdict(self)


def analyze_realtime_chunk_quality(audio_bytes: bytes) -> AudioQualityResult:
    """分析实时 WAV 分片是否包含可送 ASR 的人声能量。

    如果音频格式不是当前前端约定的 PCM16 WAV，函数保守放行。未知格式误拦截会直接导致
    无文本；放行最多让 ASR 再判断一次，因此在实时会议场景里风险更低。
    """

    try:
        with wave.open(BytesIO(audio_bytes), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            pcm = wav_file.readframes(frame_count)
    except (EOFError, wave.Error):
        return AudioQualityResult(True, 0.0, 0, 0.0, 0, "unsupported_format")

    if sample_width != 2 or channels < 1 or frame_rate <= 0 or frame_count <= 0:
        return AudioQualityResult(True, 0.0, 0, 0.0, 0, "unsupported_format")

    duration_ms = int((frame_count / frame_rate) * 1000)
    if duration_ms < REALTIME_MIN_DURATION_MS:
        return AudioQualityResult(False, 0.0, 0, 0.0, duration_ms, "too_short")

    sample_count = len(pcm) // 2
    if not sample_count:
        return AudioQualityResult(False, 0.0, 0, 0.0, duration_ms, "empty")

    square_sum = 0
    peak = 0
    active_samples = 0
    for (sample,) in struct.iter_unpack("<h", pcm[: sample_count * 2]):
        absolute = abs(sample)
        square_sum += absolute * absolute
        peak = max(peak, absolute)
        if absolute >= REALTIME_ACTIVE_SAMPLE_LEVEL_INT16:
            active_samples += 1

    rms = math.sqrt(square_sum / sample_count)
    active_ratio = active_samples / sample_count
    has_voice = (
        rms >= REALTIME_MIN_RMS_INT16
        and peak >= REALTIME_MIN_PEAK_INT16
        and active_ratio >= REALTIME_MIN_ACTIVE_RATIO
    )
    return AudioQualityResult(
        has_voice=has_voice,
        rms=round(rms, 3),
        peak=peak,
        active_ratio=round(active_ratio, 6),
        duration_ms=duration_ms,
        reason="voice" if has_voice else "low_volume",
    )


def realtime_chunk_has_voice(audio_bytes: bytes) -> bool:
    """兼容旧调用方的布尔接口。"""

    return analyze_realtime_chunk_quality(audio_bytes).has_voice
