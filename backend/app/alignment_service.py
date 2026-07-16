"""字音对照与选区音频反查。

`Qwen3-ASR-1.7B` 负责生成转写文本；字/词级时间戳由
`Qwen3-ForcedAligner-0.6B` 或兼容服务补齐。本模块定义应用侧需要的
时间戳数据结构和选区反查规则，方便前端做“选中文本 -> 截取对应音频”。
"""

from __future__ import annotations

from typing import Any


def find_audio_window_for_selection(
    transcript_text: str,
    selected_text: str,
    word_timestamps: list[dict[str, Any]],
    padding_ms: int = 500,
) -> dict[str, int]:
    """根据选中文本和字/词时间戳，计算需要截取的音频时间范围。

    Args:
        transcript_text: 当前转写片段完整文本。
        selected_text: 用户在页面中选中的文本。
        word_timestamps: 强制对齐输出的字/词时间戳列表。
        padding_ms: 前后预留音频，避免截断发音。

    Returns:
        `{"start_ms": int, "end_ms": int}`，可直接传给音频切片服务。

    约定：
    - 首版按顺序拼接 `word_timestamps[*].text`，适合中文逐字或短词对齐。
    - 如果选区找不到，抛出 ValueError，让接口层给出清晰提示。
    """
    if not selected_text:
        raise ValueError("selected_text 不能为空")

    # 强制对齐服务可能返回“逐字”时间戳，也可能返回“词/短语”时间戳。
    # 因此前端选区不能直接用字符下标切 list，而要先把每个 token 展开成字符范围。
    aligned_text = "".join(str(item.get("text", "")) for item in word_timestamps)
    search_text = aligned_text or transcript_text
    start_char = search_text.find(selected_text)
    if start_char < 0:
        raise ValueError("选中文本没有在转写结果中找到，无法反查音频区间")

    end_char = start_char + len(selected_text)
    selected_items = []
    cursor = 0
    for item in word_timestamps:
        token_text = str(item.get("text", ""))
        token_start = cursor
        token_end = cursor + len(token_text)
        cursor = token_end
        # token 与选区字符范围有交集，即认为这段音频需要纳入截取窗口。
        if token_start < end_char and start_char < token_end:
            selected_items.append(item)
    if not selected_items:
        raise ValueError("选中文本缺少字音对齐时间戳")

    raw_start = min(int(item["start_ms"]) for item in selected_items)
    raw_end = max(int(item["end_ms"]) for item in selected_items)
    return {
        "start_ms": max(0, raw_start - padding_ms),
        "end_ms": raw_end + padding_ms,
    }


def mock_align_text(text: str, start_ms: int = 0, step_ms: int = 240) -> list[dict[str, int | str]]:
    """生成可用于本地联调的模拟字级时间戳。

    真实环境中该函数会被强制对齐模型服务替代；保留 mock 是为了让页面、
    导出和声纹选区注册在没有模型服务时仍能完整联调。
    """
    timestamps: list[dict[str, int | str]] = []
    cursor = start_ms
    for char in text:
        if char.isspace():
            continue
        timestamps.append({"text": char, "start_ms": cursor, "end_ms": cursor + step_ms})
        cursor += step_ms
    return timestamps
