"""声纹注册与匹配服务。

ASR 主模型只负责“听写文字”，不承担说话人身份识别。本模块定义声纹业务对象，
真实模型推荐接 CAM++ 或兼容声纹服务；首版提供确定性 mock，确保前后端全流程可跑。
"""

from __future__ import annotations

import hashlib
from typing import Any


def has_valid_embedding_id(profile: dict[str, Any]) -> bool:
    """Return whether a business voiceprint record has a real durable embedding reference.

    Status alone is insufficient because historic mock flows could label a person registered
    without persisting an embedding. Matching therefore accepts a root ID written by successful
    registration or a valid ID attached to an uploaded sample record.
    """

    # Keep genuine records from before the marker was introduced, but reject every record that
    # explicitly identifies itself as mock/fallback. This is the business-layer safety net.
    model_name = str(profile.get("modelStatus") or profile.get("model") or "").lower()
    if profile.get("realModel") is False or profile.get("mockMode") is True or profile.get("fallbackReason") or "fallback" in model_name:
        return False
    if str(profile.get("embeddingId") or "").strip():
        return True
    return any(
        str(sample.get("embeddingId") or "").strip()
        for sample in profile.get("sampleFiles") or []
        if isinstance(sample, dict)
    )


def build_voiceprint_registration(
    speaker_name: str,
    meeting_id: str,
    source_file_id: str,
    selected_text: str,
    audio_window: dict[str, int],
) -> dict[str, Any]:
    """构造“选中文本注册声纹”的记录。

    Args:
        speaker_name: 用户录入或从左侧声纹库搜索选择的姓名。
        meeting_id: 当前会议 ID。
        source_file_id: 选区对应的音频/视频文件 ID。
        selected_text: 用于辅助确认的转写文本。
        audio_window: 根据字音对齐反查出的音频区间。

    Returns:
        声纹注册记录。真实 embedding 由模型服务异步补齐。
    """
    start_ms = int(audio_window["start_ms"])
    end_ms = int(audio_window["end_ms"])
    if end_ms <= start_ms:
        raise ValueError("声纹注册音频区间无效")

    fingerprint_seed = f"{speaker_name}|{meeting_id}|{source_file_id}|{start_ms}|{end_ms}"
    return {
        "id": hashlib.sha1(fingerprint_seed.encode("utf-8")).hexdigest()[:16],
        "speakerName": speaker_name,
        "meetingId": meeting_id,
        "sourceFileId": source_file_id,
        "selectedText": selected_text,
        "startMs": start_ms,
        "endMs": end_ms,
        "durationMs": end_ms - start_ms,
        "source": "selection",
        # mockEmbeddingId 用于本地联调；生产环境会换成声纹模型返回的 embedding/vector id。
        # A text selection identifies a candidate audio window but does not create a CAM++
        # embedding. Keep the record pending so a demo marker can never become a real match.
        "registerStatus": "pending_sample",
        "modelStatus": "waiting_sample",
    }


def match_speaker_by_mock_energy(audio_chunk: bytes, registered_profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """本地联调用的确定性声纹匹配。

    真实部署时，audio_chunk 会送入 CAM++ 声纹服务，返回最相似的 speaker。
    这里用音频字节和注册列表做稳定哈希，保证没有模型时页面仍能展示说话人区分流程。
    """
    if not registered_profiles:
        return {"speakerId": "", "speakerName": "未登记发言人", "confidence": 0.0}

    digest = hashlib.sha1(audio_chunk or b"default").digest()[0]
    profile = registered_profiles[digest % len(registered_profiles)]
    return {
        "speakerId": profile.get("id", ""),
        "speakerName": profile.get("speakerName", "未知发言人"),
        "confidence": 0.82,
    }
