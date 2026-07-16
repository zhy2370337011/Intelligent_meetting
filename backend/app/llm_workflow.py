"""会议 AI 能力的本地编排层。

本文件刻意不再连接“智能体平台 invoke”接口。五个前端工具能力都在后端本地完成编排：
1. Python 先把会议元数据、转写片段、模板和上下文整理成稳定输入；
2. DeepSeek 负责按指定 JSON Schema 生成内容；
3. DeepSeek 不可用、未配置或返回异常时，立即回退到本地规则结果，保证前端按钮始终可用。

这种写法把“业务流程”留在本项目里，后续要调整摘要字段、纪要模板或待办映射时，只需要改这里，
不用再维护外部平台的 workflow id、登录 token 或节点协议。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any, Callable

from app.config import (
    AI_MOCK_MODE,
    AI_PROVIDER,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
)
from app.sensitive_policy import apply_sensitive_policy


Urlopen = Callable[..., Any]


class WorkflowError(RuntimeError):
    """会议 AI 编排失败。

    历史代码和测试里使用过 WorkflowError 这个异常名，这里保留名称，但语义已经从“智能体平台失败”
    调整为“本地 AI 编排或 DeepSeek 调用失败”。业务路由不会把该异常直接抛给前端，而是降级返回
    可展示内容，避免用户点击工具按钮后页面中断。
    """


class DeepSeekWorkflowClient:
    """DeepSeek OpenAI 兼容接口客户端。

    该客户端只做三件事：组装 Chat Completions 请求、读取模型回复、提取 JSON。它不包含任何会议
    业务逻辑，会议摘要/纪要/待办等流程全部放在本文件下方的本地编排函数中，便于单元测试替换
    urlopen 并验证不会访问旧的智能体平台地址。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEEPSEEK_BASE_URL,
        model: str = DEEPSEEK_MODEL,
        timeout_seconds: int = DEEPSEEK_TIMEOUT_SECONDS,
        urlopen: Urlopen = urllib.request.urlopen,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.urlopen = urlopen

    def complete_json(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """调用 DeepSeek 并把回复解析为 dict。

        DeepSeek 的接口兼容 OpenAI Chat Completions。这里显式要求模型“只输出 JSON”，但仍然保留
        Markdown 代码块和夹杂说明文字的容错解析，因为实际大模型偶尔会把 JSON 包进 ```json。
        """

        if not self.api_key:
            raise WorkflowError("未配置 DEEPSEEK_API_KEY")

        request_body = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "你必须只返回一个合法 JSON 对象，不要输出 Markdown、解释或额外文字。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with self.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WorkflowError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - 网络、证书、DNS 等错误都要统一降级给业务层处理。
            raise WorkflowError(f"DeepSeek 调用失败: {exc}") from exc

        try:
            payload = json.loads(body or "{}")
            content = payload["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - 第三方返回结构异常时需要带原始片段方便排查。
            preview = body[:500]
            raise WorkflowError(f"DeepSeek 响应结构异常: {preview}") from exc
        return extract_json_from_workflow_message(content)


def extract_json_from_workflow_message(message: Any) -> dict[str, Any]:
    """从模型输出中提取 JSON 对象。

    兼容三类常见情况：已经是 dict、纯 JSON 字符串、Markdown JSON 代码块。最后再尝试截取第一组
    大括号内容，尽量把模型偶发的解释性前后缀消化掉，减少前端工具按钮失败概率。
    """

    if isinstance(message, dict):
        return message
    text = str(message or "").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    code_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if code_match:
        parsed = json.loads(code_match.group(1).strip())
        if isinstance(parsed, dict):
            return parsed

    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        parsed = json.loads(text[first : last + 1])
        if isinstance(parsed, dict):
            return parsed
    raise WorkflowError("无法从 DeepSeek 输出中提取 JSON")


def _deepseek_enabled() -> bool:
    """判断当前是否应该真实调用 DeepSeek。

    `AI_MOCK_MODE=true` 是给无网络/不想消耗额度的开发环境准备的硬开关；只要打开，就算写了 key 也会
    走本地规则结果。除此之外，必须显式使用 deepseek provider 且配置 key，才会发起外部请求。
    """

    return AI_PROVIDER == "deepseek" and bool(DEEPSEEK_API_KEY) and not AI_MOCK_MODE


def _deepseek_client() -> DeepSeekWorkflowClient:
    """创建 DeepSeek 客户端。

    单独封装是为了测试时可以 monkey patch 这个函数或 DeepSeekWorkflowClient.urlopen；生产运行则
    始终使用 config.py 从 backend/.env 读取到的模型名和密钥。
    """

    return DeepSeekWorkflowClient(api_key=DEEPSEEK_API_KEY)


def _run_deepseek_or_fallback(
    task_name: str,
    system_prompt: str,
    workflow_input: dict[str, Any],
    fallback: dict[str, Any],
    normalizer: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行本地编排后的 DeepSeek 任务，失败时回退到本地规则结果。

    normalizer 用于把模型返回的自由字段收敛成前端已经依赖的固定字段。例如摘要必须有 keywords、
    topic、overview、keyPoints，纪要必须有 title/templateName/content。这样即使模型漏字段，页面
    仍能展示完整结构。
    """

    if not _deepseek_enabled():
        result = dict(fallback)
        result["_aiStatus"] = "mock"
        result["_aiTask"] = task_name
        return result
    try:
        raw = _deepseek_client().complete_json(system_prompt, workflow_input)
        result = normalizer(raw, fallback) if normalizer else {**fallback, **raw}
        result["_aiStatus"] = "deepseek"
        result["_aiTask"] = task_name
        return result
    except Exception as exc:  # noqa: BLE001 - 第三方模型不可用时必须保护前端闭环。
        result = dict(fallback)
        result["_aiStatus"] = "fallback"
        result["_aiTask"] = task_name
        result["_aiWarning"] = str(exc)
        return result


def _meeting_text(segments: list[dict[str, Any]], max_chars: int = 18000) -> str:
    """把转写片段压缩成适合大模型读取的文本。

    DeepSeek 请求体不应无节制增长。这里保留发言人和时间信息，同时截断到会议工具常用的上下文长度；
    原始片段仍会作为结构化字段传入，模型可以按需引用。
    """

    lines = []
    for segment in segments:
        speaker = segment.get("speakerName") or "发言人"
        start_ms = int(segment.get("startMs") or 0)
        minute, second = divmod(start_ms // 1000, 60)
        text = str(segment.get("text") or "").strip()
        if text:
            lines.append(f"[{minute:02d}:{second:02d}] {speaker}: {text}")
    compact = "\n".join(lines)
    return compact[:max_chars]


def prepare_sensitive_ai_text(text: str, rules: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Create a detached AI-target text value and auditable policy evidence.

    This is intentionally distinct from Task 3 recognition normalization: it runs only at the
    model boundary and never writes to a transcript segment's ``rawText`` or stored ``text``.
    """

    result = apply_sensitive_policy(text, rules, "ai")
    return result.text, {"target": "ai", "ruleVersion": result.rule_version, "hits": [dict(hit) for hit in result.hits]}


def prepare_sensitive_ai_meeting(meeting: dict[str, Any], rules: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the only meeting object that AI workflows may receive for a policy-enabled run.

    The copy keeps the original meeting usable by persistence and export code.  We replace only
    consumer-facing text fields; provider raw text and recognition ``normalizationEdits`` remain
    untouched, so policy masking cannot be mistaken for ASR correction.
    """

    safe_meeting = deepcopy(meeting)
    all_hits: list[dict[str, Any]] = []
    rule_version = apply_sensitive_policy("", rules, "ai").rule_version
    for index, segment in enumerate(safe_meeting.get("segments", [])):
        result = apply_sensitive_policy(str(segment.get("text") or ""), rules, "ai")
        segment["text"] = result.text
        all_hits.extend(dict(hit, segmentId=str(segment.get("id") or ""), field="segments.text") for hit in result.hits)
        rule_version = result.rule_version

    # Minutes/todo workflows may use a previously generated summary as supplemental context.
    # Mask that detached context too, preventing an older pre-policy artifact from bypassing the
    # current meeting's frozen AI rule set.  These values are never written back to the meeting.
    for field in ("meetingName", "meetingLocation", "summary", "minutes"):
        safe_meeting[field], field_hits, field_version = _mask_ai_value(safe_meeting.get(field), rules, field)
        all_hits.extend(field_hits)
        rule_version = field_version
    return safe_meeting, {"target": "ai", "ruleVersion": rule_version, "hits": all_hits}


def _mask_ai_value(value: Any, rules: list[dict[str, Any]], field: str) -> tuple[Any, list[dict[str, Any]], str]:
    """Recursively mask JSON-shaped auxiliary AI context while preserving its public shape."""

    if isinstance(value, str):
        result = apply_sensitive_policy(value, rules, "ai")
        return result.text, [dict(hit, field=field) for hit in result.hits], result.rule_version
    if isinstance(value, list):
        masked_items: list[Any] = []
        hits: list[dict[str, Any]] = []
        version = apply_sensitive_policy("", rules, "ai").rule_version
        for index, item in enumerate(value):
            masked, item_hits, version = _mask_ai_value(item, rules, f"{field}[{index}]")
            masked_items.append(masked)
            hits.extend(item_hits)
        return masked_items, hits, version
    if isinstance(value, dict):
        masked_values: dict[str, Any] = {}
        hits = []
        version = apply_sensitive_policy("", rules, "ai").rule_version
        for key, item in value.items():
            masked, item_hits, version = _mask_ai_value(item, rules, f"{field}.{key}")
            masked_values[key] = masked
            hits.extend(item_hits)
        return masked_values, hits, version
    return value, [], apply_sensitive_policy("", rules, "ai").rule_version


def prepare_sensitive_ai_template(template: dict[str, Any], rules: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a detached, fully masked template payload for the minutes workflow.

    Template records can contain sensitive text in their name, body, tag bindings, or future
    nested configuration fields.  Walking the entire JSON-shaped record keeps the AI boundary
    complete without mutating the frozen template snapshot that meeting provenance relies on.
    """

    safe_template, hits, rule_version = _mask_ai_value(deepcopy(template), rules, "template")
    # ``_mask_ai_value`` preserves dictionaries, but retain a defensive empty mapping for a
    # malformed legacy template so callers never hand a non-template value to the workflow.
    return (
        safe_template if isinstance(safe_template, dict) else {},
        {"target": "ai", "ruleVersion": rule_version, "hits": hits},
    )


def generate_mock_summary(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """根据转写片段生成可联调的本地摘要结构。"""

    full_text = " ".join(str(segment.get("text", "")) for segment in segments)
    todos = []
    if "完成" in full_text or "负责" in full_text:
        todos.append(
            {
                "title": "完成智能会议系统联调",
                "content": "完成 ASR、声纹、纪要模板和普通会议系统待办推送联调。",
                "ownerDept": "信息中心",
                "cooperateDept": "办公室",
                "dueDate": "2026-12-31",
                "milestones": [
                    {"time": "2026-08-31", "content": "完成 Qwen3-ASR 网关接入"},
                    {"time": "2026-10-31", "content": "完成声纹注册与字音对照联调"},
                ],
            }
        )

    speakers = sorted({segment.get("speakerName", "发言人") for segment in segments})
    return {
        "keywords": ["Qwen3-ASR", "声纹区分", "会议纪要", "待办推送"],
        "topic": "智能会议系统建设与联调安排",
        "overview": "会议围绕实时转写、离线转写、声纹识别、摘要纪要和外部会议系统对接展开。",
        "keyPoints": [
            "ASR 链路先使用 DashScope Qwen3-ASR API。",
            "字音同步回听由强制对齐服务补齐。",
            "声纹注册通过选中文本反查音频片段完成。",
        ],
        "decisionItems": ["本地先部署 VAD、声纹和强制对齐小模型服务。"],
        "riskFlags": ["DashScope ASR 属于外部 API，涉密场景需要切换为内网服务。"],
        "todos": todos,
        "speakerSummaries": [
            {"speakerName": name, "summary": f"{name} 参与了系统能力确认与任务讨论。"}
            for name in speakers
        ],
    }


def _normalize_summary(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """把 DeepSeek 摘要输出收敛成前端固定结构。"""

    result = {**fallback, **raw}
    for key in ("keywords", "keyPoints", "decisionItems", "riskFlags", "todos", "speakerSummaries"):
        value = result.get(key)
        result[key] = value if isinstance(value, list) else fallback.get(key, [])
    result["topic"] = str(result.get("topic") or fallback["topic"])
    result["overview"] = str(result.get("overview") or fallback["overview"])
    return result


def generate_summary_with_workflow(meeting: dict[str, Any]) -> dict[str, Any]:
    """生成会议摘要。

    本地编排步骤：
    1. 收集会议名称、时间、地点、创建人等元数据；
    2. 按发言人和时间戳整理转写文本；
    3. 要求 DeepSeek 输出关键词、概要、要点、决策、风险、待办和发言人总结；
    4. 校验字段，缺失部分用本地规则兜底。
    """

    segments = meeting.get("segments", [])
    fallback = generate_mock_summary(segments)
    workflow_input = {
        "meeting_meta": {
            "meetingId": meeting.get("id", ""),
            "meetingName": meeting.get("meetingName") or meeting.get("fileName", ""),
            "createdAt": meeting.get("createdAt", ""),
            "location": meeting.get("meetingLocation", ""),
            "creator": meeting.get("creator", ""),
        },
        "transcript_text": _meeting_text(segments),
        "transcript_segments": segments,
        "keyword_libraries": meeting.get("keywordLibraryNames", []),
        "sensitive_hits": meeting.get("sensitiveHits", []),
        "output_schema": {
            "keywords": ["字符串数组"],
            "topic": "会议主题",
            "overview": "150字以内概要",
            "keyPoints": ["会议要点数组"],
            "decisionItems": ["决策事项数组"],
            "riskFlags": ["风险提醒数组"],
            "todos": [
                {
                    "title": "待办标题",
                    "content": "待办内容",
                    "ownerDept": "责任部门",
                    "cooperateDept": "配合部门",
                    "dueDate": "YYYY-MM-DD 或空字符串",
                    "milestones": [{"time": "YYYY-MM-DD", "content": "里程碑"}],
                }
            ],
            "speakerSummaries": [{"speakerName": "发言人", "summary": "观点总结"}],
        },
    }
    prompt = "你是政企智慧会议系统的摘要编排器，请基于转写内容输出可落库的会议摘要 JSON。"
    return _run_deepseek_or_fallback("meeting_summary", prompt, workflow_input, fallback, _normalize_summary)


def generate_mock_meeting_minutes(
    meeting_name: str,
    template_name: str,
    summary: dict[str, Any],
) -> dict[str, str]:
    """按模板生成本地纪要文本。"""

    key_points = "\n".join(f"- {item}" for item in summary.get("keyPoints", []))
    todos = "\n".join(
        f"- {todo.get('title', '未命名待办')}：{todo.get('content', '')}"
        for todo in summary.get("todos", [])
    )
    content = (
        f"会议主题：{summary.get('topic', meeting_name)}\n\n"
        f"一、会议要点\n{key_points or '- 暂无'}\n\n"
        f"二、待办事项\n{todos or '- 暂无'}\n\n"
        "三、结论\n请相关责任部门按节点推进，并在后续会议中反馈进展。"
    )
    return {
        "title": f"{meeting_name}会议纪要",
        "templateName": template_name,
        "content": content,
    }


def _normalize_minutes(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """把 DeepSeek 纪要输出收敛成前端和 docx 导出需要的字段。"""

    return {
        "title": str(raw.get("title") or fallback["title"]),
        "templateName": str(raw.get("templateName") or fallback["templateName"]),
        "content": str(raw.get("content") or fallback["content"]),
        "tagValues": raw.get("tagValues") if isinstance(raw.get("tagValues"), dict) else {},
        "exportBlocks": raw.get("exportBlocks") if isinstance(raw.get("exportBlocks"), list) else [],
    }


def generate_minutes_with_workflow(
    meeting: dict[str, Any],
    template_name: str,
    template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成会议纪要。

    本地编排步骤：
    1. 读取会议摘要，不存在则先用规则摘要兜底；
    2. 读取用户选择的纪要模板内容、标签和绑定字段；
    3. 要求 DeepSeek 输出正文、标签填充值和导出块；
    4. 模型输出不完整时用规则纪要补齐。
    """

    summary = meeting.get("summary") or generate_mock_summary(meeting.get("segments", []))
    fallback = generate_mock_meeting_minutes(meeting.get("meetingName", ""), template_name, summary)
    workflow_input = {
        "meeting_meta": {
            "meetingId": meeting.get("id", ""),
            "meetingName": meeting.get("meetingName", ""),
            "createdAt": meeting.get("createdAt", ""),
            "location": meeting.get("meetingLocation", ""),
        },
        "transcript_text": _meeting_text(meeting.get("segments", [])),
        "summary": summary,
        "template": template or {"name": template_name},
        "output_schema": {
            "title": "纪要标题",
            "templateName": "模板名称",
            "content": "可编辑纪要正文，按模板章节组织",
            "tagValues": {"模板标签": "填充值"},
            "exportBlocks": [{"heading": "章节标题", "body": "章节正文"}],
        },
    }
    prompt = "你是政企会议纪要秘书，请按模板生成正式、可编辑、可导出的会议纪要 JSON。"
    return _run_deepseek_or_fallback("meeting_minutes", prompt, workflow_input, fallback, _normalize_minutes)


def _normalize_todos(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """把 DeepSeek 待办输出收敛成普通会议系统 taskSave 可映射结构。"""

    items = raw.get("todos") or raw.get("items") or fallback.get("todos", [])
    if not isinstance(items, list):
        items = fallback.get("todos", [])
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "title": str(item.get("title") or item.get("taskName") or "未命名待办"),
                "content": str(item.get("content") or item.get("description") or ""),
                "ownerDept": str(item.get("ownerDept") or item.get("responsibleDept") or ""),
                "cooperateDept": str(item.get("cooperateDept") or ""),
                "dueDate": str(item.get("dueDate") or item.get("completeDate") or ""),
                "milestones": item.get("milestones") if isinstance(item.get("milestones"), list) else [],
                "taskType": item.get("taskType", "meeting"),
                "childNodes": item.get("childNodes", []),
            }
        )
    return {"todos": normalized}


def extract_todos_with_workflow(meeting: dict[str, Any]) -> list[dict[str, Any]]:
    """抽取待办事项。

    本地编排步骤：
    1. 将全文和摘要一起交给模型，避免只看摘要漏掉责任人或时间；
    2. 要求输出与普通会议系统 taskSave 容易映射的字段；
    3. 若模型不可用，则直接复用摘要中的 todos。
    """

    summary = meeting.get("summary") or generate_mock_summary(meeting.get("segments", []))
    fallback = {"todos": summary.get("todos", [])}
    workflow_input = {
        "meeting_meta": {
            "meetingId": meeting.get("id", ""),
            "meetingName": meeting.get("meetingName", ""),
        },
        "transcript_text": _meeting_text(meeting.get("segments", [])),
        "summary": summary,
        "output_schema": {
            "todos": [
                {
                    "title": "待办标题",
                    "content": "任务说明",
                    "ownerDept": "责任部门",
                    "cooperateDept": "配合部门",
                    "dueDate": "YYYY-MM-DD 或空",
                    "milestones": [{"time": "YYYY-MM-DD", "content": "节点说明"}],
                    "taskType": "meeting",
                    "childNodes": [],
                }
            ]
        },
    }
    prompt = "你是会议待办抽取器，请只提取明确需要落实的任务，输出 taskSave 友好的 JSON。"
    result = _run_deepseek_or_fallback("todo_extract", prompt, workflow_input, fallback, _normalize_todos)
    return result.get("todos") or []


def translate_text(text: str, target_language: str) -> str:
    """翻译文本。

    本地编排步骤：
    1. 将前端传入文本包装成 segment，后续可以自然扩展为逐段翻译；
    2. 要求 DeepSeek 保留原段落语义，输出 text 和 segments；
    3. 路由为了兼容现有前端仍只返回最终 text。
    """

    fallback = {
        "text": (
            f"[English draft generated locally] {text}"
            if target_language.lower().startswith("en")
            else f"[中文译文由本地规则生成] {text}"
        )
    }
    workflow_input = {
        "direction": "zh-en" if target_language.lower().startswith("en") else "en-zh",
        "segments": [{"segmentId": "manual", "speakerName": "", "startMs": 0, "text": text}],
        "output_schema": {
            "text": "合并后的译文",
            "segments": [{"segmentId": "manual", "translatedText": "译文", "speakerName": "", "startMs": 0}],
        },
    }
    prompt = "你是会议同传翻译助手，请忠实翻译并保留发言人、时间戳和段落结构。"
    result = _run_deepseek_or_fallback("translate", prompt, workflow_input, fallback)
    if "text" in result:
        return str(result["text"])
    segments = result.get("segments") or []
    if segments:
        return "\n".join(str(item.get("translatedText", "")) for item in segments if item.get("translatedText"))
    return str(fallback["text"])


def reorganize_discourse(text: str) -> str:
    """语篇规整。

    本地编排步骤：
    1. 先清理空白和明显口语停顿；
    2. 请求 DeepSeek 输出标题、规整正文和章节；
    3. 前端现有接口只展示 text，因此这里返回 normalizedText。
    """

    cleaned = "\n".join(part.strip() for part in re.split(r"[\n\r]+", text or "") if part.strip())
    fallback = {
        "title": "会议内容规整",
        "normalizedText": cleaned.replace("嗯，", "").replace("呃，", ""),
        "sections": [],
    }
    workflow_input = {
        "transcript_text": text,
        "style": "政企会议纪要",
        "output_schema": {
            "title": "标题",
            "normalizedText": "清洗口语化后、按段落组织的正文",
            "sections": [{"heading": "章节标题", "body": "章节正文"}],
        },
    }
    prompt = "你是会议语篇规整助手，请删除口语冗余、保留事实、按章节输出正式文本 JSON。"
    result = _run_deepseek_or_fallback("discourse_rewrite", prompt, workflow_input, fallback)
    return str(result.get("normalizedText") or result.get("text") or fallback["normalizedText"])
