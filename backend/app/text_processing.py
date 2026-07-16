"""文本处理工具：敏感词屏蔽、热词提示、会议文本清洗。

这里放不依赖大模型的确定性规则。敏感词屏蔽必须在后端完成，
原因是它属于合规展示规则，不能依赖 ASR 或大模型“记得替换”。
"""

from __future__ import annotations


def apply_sensitive_words(text: str, sensitive_words: list[str]) -> str:
    """将命中的敏感词替换为等长星号。

    Args:
        text: 待展示或导出的会议文本。
        sensitive_words: 用户提前维护的敏感词词库。

    Returns:
        已完成敏感词屏蔽的文本。

    设计说明：
    - 使用等长 `*` 替换，方便前端仍然保持文本长度和选区位置基本稳定。
    - 跳过空字符串，避免空词导致无限替换或污染文本。
    - 首版按简单包含匹配处理；后续可在此处扩展正则规则、分级词库和例外白名单。
    """
    masked = text
    for word in sensitive_words:
        normalized = word.strip()
        if not normalized:
            continue
        masked = masked.replace(normalized, "*" * len(normalized))
    return masked


def normalize_transcript_text(text: str) -> str:
    """对 ASR 原始文本做轻量规整，真正的语篇重构仍交给大模型工作流。

    这里不做激进改写，只处理常见空白，避免影响字音对齐。
    """
    return " ".join(text.split())

