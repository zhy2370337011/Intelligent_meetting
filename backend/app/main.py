"""智能会议系统 FastAPI 后端。

启动方式：
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

首版目标不是把模型推理塞进 Web 服务进程，而是提供稳定业务 API：
页面 -> FastAPI -> DashScope ASR / 本地小模型服务 / 本地编排 DeepSeek / 普通会议系统。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import html
import json
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from app.alignment_service import find_audio_window_for_selection
from app.audio_quality import analyze_realtime_chunk_quality
from app.asr_gateway import MockQwenAsrGateway, create_asr_gateway
from app.config import (
    ALIGNMENT_GATEWAY_BASE_URL,
    AI_MOCK_MODE,
    AI_PROVIDER,
    ASR_GATEWAY_BASE_URL,
    ASR_GATEWAY_MODE,
    AUDIO_CLIP_DIR,
    DASHSCOPE_BASE_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
    MODEL_MOCK_MODE,
    UPLOAD_DIR,
    VAD_GATEWAY_BASE_URL,
    VOICEPRINT_GATEWAY_BASE_URL,
    ensure_data_dirs,
)
from app.export_service import build_meeting_docx, build_meeting_text, build_word_list_docx, read_audio_for_playback
from app.integration_service import build_task_save_request
from app.llm_workflow import (
    extract_todos_with_workflow,
    generate_minutes_with_workflow,
    generate_mock_meeting_minutes,
    generate_mock_summary,
    generate_summary_with_workflow,
    prepare_sensitive_ai_meeting,
    prepare_sensitive_ai_text,
    prepare_sensitive_ai_template,
    reorganize_discourse,
)
from app.meeting_domain import build_processing_snapshot, get_meeting_asr_inputs
from app.minutes_service import generate_minutes_version, resolve_minutes_template
from app.model_clients import LocalAlignmentClient, LocalModelServiceError, LocalVadClient, LocalVoiceprintClient
from app.recognition_policy import (
    EffectiveVocabulary,
    apply_final_replacements,
    build_effective_vocabulary,
    build_realtime_context,
    extract_document_terms,
    filter_realtime_ai_segments,
    filter_realtime_context_items,
    freeze_recognition_policy_snapshot,
    is_realtime_context_echo,
)
from app.realtime_lease import RealtimeLeaseRegistry
from app.sensitive_policy import apply_sensitive_policy, freeze_sensitive_rule_snapshot, policy_snapshot_rules
from app.realtime_speaker import (
    REALTIME_FINAL_SEGMENT_CLUSTER_COSINE_THRESHOLD,
    RealtimeSpeakerTracker,
    SpeakerIdentity,
)
from app.realtime_stream import Pcm16TimelineBuffer, Pcm16WaveRecorder, create_realtime_stream_session
from app.store import format_datetime, store
from app.voiceprint_service import build_voiceprint_registration, has_valid_embedding_id


ensure_data_dirs()
app = FastAPI(title="智能会议系统", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

asr_gateway = create_asr_gateway(ASR_GATEWAY_MODE, ASR_GATEWAY_BASE_URL)

# 同一 meeting 只允许最后声明配置的 WebSocket 写入实时正文。该对象只在当前 Python
# 进程内有效；多 worker/多实例部署必须改用 Redis 或数据库条件租约，详见 realtime_lease.py。
_realtime_leases = RealtimeLeaseRegistry()


def workflow_runtime_status() -> dict[str, Any]:
    """返回会议 AI 本地编排运行状态。

    这个接口沿用历史路径 `/api/workflows/status`，但语义已经改为“本地编排 + DeepSeek”。
    返回值只说明 provider、模型、密钥是否已配置和五个能力是否由本项目后端承载，不返回任何 API Key
    或旧智能体平台地址，避免前端日志、截图或验收输出泄漏敏感信息。
    """

    tools = {
        "meetingSummary": "local_orchestration_deepseek",
        "meetingMinutes": "local_orchestration_deepseek",
        "todoExtract": "local_orchestration_deepseek",
        "discourseRewrite": "local_orchestration_deepseek",
    }
    deepseek_configured = AI_PROVIDER == "deepseek" and bool(DEEPSEEK_API_KEY)
    runtime_ready = deepseek_configured and not AI_MOCK_MODE
    fallback_reasons: list[str] = []
    if AI_PROVIDER != "deepseek":
        fallback_reasons.append("AI_PROVIDER is not deepseek")
    if not DEEPSEEK_API_KEY:
        fallback_reasons.append("DEEPSEEK_API_KEY is not configured")
    if AI_MOCK_MODE:
        fallback_reasons.append("AI_MOCK_MODE is enabled")
    return {
        "mode": "local_deepseek" if runtime_ready else "local_fallback",
        "provider": AI_PROVIDER,
        "model": DEEPSEEK_MODEL,
        "baseUrlConfigured": bool(DEEPSEEK_BASE_URL),
        "apiKeyConfigured": bool(DEEPSEEK_API_KEY),
        "mockMode": AI_MOCK_MODE,
        "timeoutSeconds": DEEPSEEK_TIMEOUT_SECONDS,
        "remoteReady": runtime_ready,
        "fallbackReason": "; ".join(fallback_reasons) if fallback_reasons else "",
        "workflows": {
            name: {
                "configured": True,
                # 每个能力现在都是后端代码里的本地编排，不再依赖外部 workflow id。
                "engine": engine,
            }
            for name, engine in tools.items()
        },
    }


class MeetingCreateRequest(BaseModel):
    """创建会议请求。

    字段和前端创建会议弹窗保持一致：主题、语言、翻译方向、音源、纪要模板、
    关键词库和声纹开关都会在这里收口。真实接入模型时，这些字段会继续传给
    ASR 网关和智能体工作流。
    """

    meetingName: str = "未命名会议"
    meetingLocation: str = ""
    # Keep the public API's historical display default for clients that omit this field. Provider
    # adapters normalize it immediately before ASR, so compatibility does not leak an invalid enum
    # to DashScope or unexpectedly change which language-scoped optimization records are selected.
    language: str = "中文普通话"
    translateDirection: str = "无"
    audioSource: str = "麦克风阵列"
    templateId: str = "tpl-001"
    keywordLibraryIds: list[str] = []
    enableDiarization: bool = True
    participantNames: list[str] = []
    voiceprintGroupId: str = ""
    optimizationProfile: dict[str, Any] = {}
    notes: str = ""
    attachments: list[dict[str, Any]] = []
    documentKeywordDocumentIds: list[str] = []
    confirmedSmartTerms: list[str] = []


class MeetingUpdateRequest(BaseModel):
    """会议局部更新请求，用于状态、模板、词库等字段的轻量修改。"""

    fileName: str | None = None
    meetingName: str | None = None
    minutesStatus: str | None = None
    processStatus: str | None = None
    status: str | None = None
    templateId: str | None = None
    keywordLibraryIds: list[str] | None = None


class TranscribeRequest(BaseModel):
    startMs: int | None = None
    endMs: int | None = None
    enableDiarization: bool = True


class AlignRequest(BaseModel):
    transcriptText: str
    selectedText: str
    words: list[dict[str, Any]]
    paddingMs: int = 500


class VoiceprintRegisterRequest(BaseModel):
    speakerName: str
    meetingId: str
    sourceFileId: str
    transcriptText: str
    selectedText: str
    words: list[dict[str, Any]]
    paddingMs: int = 500


class DictionaryRequest(BaseModel):
    words: list[str]


class MinutesRequest(BaseModel):
    # ``None`` is intentionally different from an empty string. Omission selects the immutable
    # meeting-bound template; a supplied blank ID is rejected instead of silently using a default.
    templateId: str | None = None
    templateName: str = "标准会议纪要"


class MinutesDraftRequest(BaseModel):
    """把右侧 AI 工具面板里用户编辑后的正文写回会议纪要。

    AI 工具结果在前端是一个可编辑草稿，而不是不可修改的只读 JSON。
    这里单独建请求模型，避免复用生成纪要的模板请求，后续接入审计或版本历史时也更容易扩展。
    """

    sourceTool: str = ""
    content: str = ""
    # Omitting the ID keeps the prior "edit current minutes" behavior. Supplying one lets the
    # history UI edit an older version without changing the current-version pointer.
    versionId: str | None = None


class ToolDraftRequest(BaseModel):
    """保存右侧 AI 工具面板的用户编辑草稿。

    草稿按 meetingId + tool 存储，用来解决“切到其他工具再回来又要重新生成”的体验问题。
    它不替代摘要/纪要/待办等正式业务字段，只保存详情面板里的可编辑结果。
    """

    title: str = ""
    content: str = ""


class ExportRequest(BaseModel):
    exportKind: str = "all"


class BatchMeetingExportRequest(BaseModel):
    """批量会议导出请求；服务端统一打包，避免浏览器连续下载被拦截。"""

    meetingIds: list[str]
    exportKind: str = "transcript"


class SensitivePolicyRevisionRequest(BaseModel):
    """显式把当前或所选禁忌词规则快照应用到一场既有会议。"""

    ruleIds: list[str] | None = None


class RetranscriptionRequest(BaseModel):
    """既有录音重新转写配置；省略字段时沿用原会议冻结设置。"""

    language: str | None = None
    enableDiarization: bool | None = None
    optimizationProfile: dict[str, Any] | None = None
    documentKeywordDocumentIds: list[str] | None = None
    confirmedSmartTerms: list[str] | None = None


class TodoPatchRequest(BaseModel):
    """待办人工校正字段；sourceRanges 由系统维护，客户端不能改写。"""

    title: str | None = None
    content: str | None = None
    owner: str | None = None
    ownerDept: str | None = None
    deadline: str | None = None
    dueDate: str | None = None
    status: str | None = None


class KeywordLibraryRequest(BaseModel):
    """关键词库配置请求，对应前端“关键词优化”页面。"""

    name: str
    words: list[str] = []
    enabled: bool = True
    scope: str = "通用会议"


class SensitiveRuleRequest(BaseModel):
    """敏感词规则请求，对应前端“敏感词屏蔽”页面。"""

    word: str
    replacement: str = "stars"
    # ``None`` distinguishes an omitted modern field from an explicit value. Older clients only
    # send ``replacement``/``scope``; concrete defaults here previously overrode those legacy
    # choices before the route could determine which spelling the caller actually supplied.
    displayMode: str | None = None
    enabled: bool = True
    scope: str = "展示与导出"
    remark: str = ""
    caseSensitive: bool = False
    language: str = "zh"
    applyScope: str | None = None


class TemplateRequest(BaseModel):
    """纪要模板请求，对应前端“纪要模板”页面。"""

    name: str
    type: str = "通用会议"
    isDefault: bool = False
    sections: list[str] = []
    source: str = "my"
    previewType: str = "custom"
    tags: list[str] = []
    description: str = ""
    # 这些字段用于“本地模板导入 -> 解析模板内容 -> 配置文本标签 -> 语音识别后自动填充”的闭环。
    # content 保存从 docx/txt/pptx 中抽取到的纯文本；后续可升级为带坐标的版式 JSON。
    content: str = ""
    # originFilename 记录原始模板文件名，便于页面回显、审计和重新下载模板源文件。
    originFilename: str = ""
    # tagBindings 描述业务标签与模板区域的绑定关系，例如“会议主题”绑定 ASR/摘要生成出的 title 字段。
    tagBindings: list[dict[str, Any]] = []
    # fillStrategy 用来区分当前 mock 自动填充和后续大模型自动填充，接口稳定后只替换内部实现。
    fillStrategy: str = "mock_auto_fill"


class TemplateImportRequest(TemplateRequest):
    """导入模板请求。

    当前版本先保存模板元数据和章节结构；后续若解析真实 docx 模板，只需要把解析出的结构填入 sections。
    """

    pass


class VoiceprintRequest(BaseModel):
    """声纹库人员请求。

    这里不是模型注册接口，只维护页面上的人员资料；真正从选中文本注册声纹
    仍走 `/api/voiceprints/register-from-selection`。
    """

    name: str
    department: str = "未分配部门"
    samples: int = 1
    enabled: bool = True
    remark: str = ""
    groupId: str | None = None


class VoiceprintGroupRequest(BaseModel):
    """声纹分组请求，支撑声纹库管理页左侧分组。"""

    name: str
    description: str = ""


class BatchVoiceprintRequest(BaseModel):
    """声纹批量操作请求，供批量删除和批量下载复用。"""

    ids: list[str]


class ManualKeywordRequest(BaseModel):
    """识别优化中心“关键词手动优化”请求。"""

    language: str = "zh"
    words: list[str] = []
    enabled: bool = True
    applyScope: str = "全部会议"


class DocumentKeywordConfirmRequest(BaseModel):
    """确认文档抽取候选，只有确认后的词才允许进入会议识别快照。"""

    keywords: list[str] = []


class RoomReserveRequest(BaseModel):
    """会议室预定请求，对齐讯飞首页的“预定会议室”。"""

    meetingName: str
    reservedTime: str = ""
    reservedBy: str = "管理员"


class ReplacementRuleRequest(BaseModel):
    """识别优化中心“关键词强制替换”请求。"""

    wrongWord: str
    correctWord: str
    enabled: bool = True
    applyScope: str = "后续识别"


class SegmentPatchRequest(BaseModel):
    """会议详情转写片段编辑请求。

    前端编辑转写文本、调整发言人或重点标记时，只提交发生变化的字段。
    """

    text: str | None = None
    speakerName: str | None = None
    speakerRole: str | None = None
    marked: bool | None = None


class SegmentBatchUpdate(BaseModel):
    """逐字稿批量保存中的单个片段修改。

    合并发言框只是前端展示效果，后端仍按稳定 segment id 保存原始片段。这样既能让用户
    一次保存整个编辑区，也不会破坏音频时间轴、敏感词审计和 AI 来源引用。
    """

    segmentId: str
    text: str | None = None
    speakerName: str | None = None


class SegmentBatchPatchRequest(BaseModel):
    """带乐观锁的逐字稿批量保存请求。"""

    expectedTranscriptRevision: int
    updates: list[SegmentBatchUpdate] = []


class SpeakerCorrectionRequest(BaseModel):
    """Apply one speaker correction to the selected meeting and optionally sync library metadata."""

    oldName: str
    name: str
    department: str = ""
    syncMode: str = "meeting_only"


class ConfigPatchRequest(BaseModel):
    """通用配置局部更新请求。

    三类配置页面字段很接近，测试和接口复用这个模型可以减少样板代码。
    """

    name: str | None = None
    words: list[str] | None = None
    enabled: bool | None = None
    scope: str | None = None
    word: str | None = None
    replacement: str | None = None
    wrongWord: str | None = None
    correctWord: str | None = None
    displayMode: str | None = None
    caseSensitive: bool | None = None
    language: str | None = None
    applyScope: str | None = None
    remark: str | None = None
    type: str | None = None
    isDefault: bool | None = None
    sections: list[str] | None = None
    source: str | None = None
    previewType: str | None = None
    tags: list[str] | None = None
    description: str | None = None
    content: str | None = None
    originFilename: str | None = None
    tagBindings: list[dict[str, Any]] | None = None
    fillStrategy: str | None = None
    department: str | None = None
    samples: int | None = None
    groupId: str | None = None


@app.get("/api/health")
def health() -> dict[str, Any]:
    """健康检查，供前端启动时确认后端可用。"""
    return {
        "status": "ok",
        "mainAsrModel": "Qwen3-ASR-1.7B",
        "asrGatewayMode": ASR_GATEWAY_MODE,
        "modelMockMode": MODEL_MOCK_MODE,
        # 部署自检信息：用于确认当前是否已经接入 DashScope、本地 VAD、声纹、
        # 强制对齐和 DeepSeek 本地编排。这里不返回任何密钥，只返回服务地址和模式。
        "modelGateways": {
            "dashscopeBaseUrl": DASHSCOPE_BASE_URL if ASR_GATEWAY_MODE == "dashscope" else "",
            "asrRemoteBaseUrl": ASR_GATEWAY_BASE_URL,
            "vadBaseUrl": VAD_GATEWAY_BASE_URL,
            "voiceprintBaseUrl": VOICEPRINT_GATEWAY_BASE_URL,
            "alignmentBaseUrl": ALIGNMENT_GATEWAY_BASE_URL,
        },
        "aiRuntime": workflow_runtime_status(),
        # 为了兼容老验收脚本，仍保留 workflowRuntime 这个键；内容已经是本地 DeepSeek 编排状态。
        "workflowRuntime": workflow_runtime_status(),
    }


@app.get("/api/workflows/status")
def get_workflow_status() -> dict[str, Any]:
    """查询摘要、纪要、待办、翻译、语篇规整 5 个 AI 能力状态。

    路径名为了兼容历史前端仍叫 workflows；当前不再读取 workflow id，也不会访问智能体平台。
    五个能力均由后端本地编排，DeepSeek 可用时返回模型结果，不可用时返回 fallback 结果。
    """

    return workflow_runtime_status()


def _probe_local_model_health(base_url: str) -> dict[str, Any]:
    """Probe one local-model endpoint through the shared bounded HTTP client.

    This tiny indirection keeps health aggregation independently testable. It also ensures every
    capability follows the same timeout/error translation as real VAD, voiceprint, and alignment
    calls instead of treating a configured URL as an implicit success.
    """

    return LocalVadClient(base_url).health(timeout=3)


def _probe_local_model_routes(base_url: str) -> set[str]:
    """读取当前监听进程的真实路由表，识别未重启的旧模型服务。"""

    return LocalVadClient(base_url).openapi_paths(timeout=3)


def _model_capability_status(capability: str, endpoint: str) -> dict[str, Any]:
    """Report one capability without allowing a failed sibling probe to poison the others."""

    checked_at = format_datetime()
    if not endpoint:
        return {
            "ready": False,
            "mode": "unavailable",
            "message": f"{capability} endpoint is not configured",
            "endpoint": "",
            "checkedAt": checked_at,
        }
    try:
        health = _probe_local_model_health(endpoint)
    except LocalModelServiceError as exc:
        return {
            "ready": False,
            "mode": "unavailable",
            "message": str(exc),
            "endpoint": endpoint,
            "checkedAt": checked_at,
        }
    except Exception as exc:  # noqa: BLE001 - status must degrade instead of breaking settings UI.
        return {
            "ready": False,
            "mode": "unavailable",
            "message": f"health probe failed: {exc}",
            "endpoint": endpoint,
            "checkedAt": checked_at,
        }

    if health.get("status") != "ok":
        return {
            "ready": False,
            "mode": "unavailable",
            "message": f"health returned status={health.get('status') or 'missing'}",
            "endpoint": endpoint,
            "checkedAt": checked_at,
        }
    if health.get("service") != "intelligent-meeting-local-model-service":
        return {
            "ready": False,
            "mode": "unavailable",
            "message": "health response is missing the intelligent-meeting model service identity",
            "endpoint": endpoint,
            "checkedAt": checked_at,
        }
    if health.get("mockMode") is True:
        # Mock endpoints are useful diagnostics, but accepting them as ready would let the UI
        # advertise a fake embedding as a real enrolled voiceprint.
        return {
            "ready": False,
            "mode": "mock",
            "message": f"{capability} is running in mock mode and cannot register genuine embeddings",
            "endpoint": endpoint,
            "checkedAt": checked_at,
        }
    capabilities = health.get("capabilities")
    capability_health = capabilities.get(capability) if isinstance(capabilities, dict) else None
    if not isinstance(capability_health, dict):
        return {
            "ready": False,
            "mode": "unavailable",
            "message": f"health response has no probed {capability} capability",
            "endpoint": endpoint,
            "checkedAt": checked_at,
            "service": health.get("service"),
        }
    if capability_health.get("ready") is not True or capability_health.get("mode") != "real":
        # Model IDs are configuration metadata, not proof of installed weights. Preserve the model
        # service's explicit unprobed/unavailable reason so the UI cannot enable false enrollment.
        return {
            "ready": False,
            "mode": capability_health.get("state") or capability_health.get("mode") or "unavailable",
            "message": str(capability_health.get("message") or f"{capability} is not ready"),
            "endpoint": endpoint,
            "checkedAt": checked_at,
            "service": health.get("service"),
        }
    if capability == "voiceprint":
        # CAM++ 权重加载成功不等于当前 8100 进程支持实时说话人 embedding。曾经出现旧进程
        # health=ready、实际 POST /v1/speakers/embedding=404 的假阳性，因此声纹能力必须同时
        # 通过真实 OpenAPI 路由检查；探针失败也按不可用处理，不能乐观放行。
        required_route = "/v1/speakers/embedding"
        try:
            routes = _probe_local_model_routes(endpoint)
        except LocalModelServiceError as exc:
            return {
                "ready": False,
                "mode": "unavailable",
                "message": f"voiceprint embedding route probe failed: {exc}",
                "endpoint": endpoint,
                "checkedAt": checked_at,
                "service": health.get("service"),
            }
        if required_route not in routes:
            return {
                "ready": False,
                "mode": "unavailable",
                "message": f"model service is missing required route {required_route}; restart the 8100 service",
                "endpoint": endpoint,
                "checkedAt": checked_at,
                "service": health.get("service"),
            }
    return {
        "ready": True,
        "mode": "real",
        "message": str(capability_health.get("message") or f"{capability} health probe succeeded"),
        "endpoint": endpoint,
        "checkedAt": checked_at,
        "lastCheckAt": checked_at,
        "service": health.get("service"),
    }


def get_model_services_status() -> dict[str, Any]:
    """Aggregate independent VAD, voiceprint, and alignment capability health states."""

    return {
        "vad": _model_capability_status("vad", VAD_GATEWAY_BASE_URL),
        "voiceprint": _model_capability_status("voiceprint", VOICEPRINT_GATEWAY_BASE_URL),
        "alignment": _model_capability_status("alignment", ALIGNMENT_GATEWAY_BASE_URL),
    }


@app.get("/api/model-services/status")
def model_services_status() -> dict[str, Any]:
    """Expose capability truth for settings UI without starting or masking local models."""

    return get_model_services_status()


def _voiceprint_runtime_status() -> dict[str, Any]:
    """Return the one capability status that gates genuine sample registration."""

    return get_model_services_status()["voiceprint"]


def _upsert_voiceprint_group_for_department(department: str) -> dict[str, Any]:
    """Reuse an exact department group or create it once for an explicit library sync."""

    normalized = department.strip()
    if not normalized:
        return store.voiceprint_groups.get("vg-ungrouped", {"id": "vg-ungrouped", "name": "Ungrouped"})
    for group in sorted(store.voiceprint_groups.values(), key=lambda item: str(item.get("id") or "")):
        if str(group.get("name") or "").strip() == normalized:
            return group
    return store.create_config_item(
        "voiceprint_groups",
        "vg",
        {"name": normalized, "description": "Created by speaker correction sync", "isSystem": False},
    )


def _sync_voiceprint_person(name: str, department: str) -> dict[str, Any]:
    """Upsert people metadata after a durable correction, never inventing an embedding.

    The selected meeting has already been saved by the caller. This function deliberately only
    maintains library metadata; it returns a warning until a real sample registration succeeds,
    making failure isolation explicit rather than rolling the transcript back.
    """

    group = _upsert_voiceprint_group_for_department(department)
    existing = next(
        (
            item
            for item in sorted(store.voiceprints.values(), key=lambda item: str(item.get("id") or ""))
            if str(item.get("name") or item.get("speakerName") or "").strip() == name
        ),
        None,
    )
    patch = {
        "name": name,
        "speakerName": name,
        "department": department,
        "groupId": group.get("id", "vg-ungrouped"),
        "groupName": group.get("name", "Ungrouped"),
        "enabled": True,
    }
    if existing:
        profile = store.update_config_item("voiceprints", str(existing["id"]), patch)
    else:
        profile = store.save_voiceprint(
            {
                "id": f"vp-{uuid.uuid4().hex[:8]}",
                **patch,
                "samples": 0,
                "sampleFiles": [],
                "registerStatus": "pending_sample",
                "modelStatus": "waiting_sample",
                "remark": "Created by speaker correction sync; upload a real sample to enroll",
            }
        )
    runtime = _voiceprint_runtime_status()
    if not runtime.get("ready"):
        return {
            "status": "warning",
            "voiceprint": profile,
            "message": str(runtime.get("message") or "voiceprint runtime is unavailable"),
        }
    if not has_valid_embedding_id(profile or {}):
        return {
            "status": "warning",
            "voiceprint": profile,
            "message": "voiceprint metadata saved; upload a real sample to create an embedding",
        }
    return {"status": "synced", "voiceprint": profile, "message": "voiceprint metadata is enrolled"}


def correct_meeting_speaker(meeting_id: str, req: SpeakerCorrectionRequest) -> dict[str, Any]:
    """Correct one speaker across a single meeting, then optionally perform isolated library sync."""

    old_name = req.oldName.strip()
    new_name = req.name.strip()
    if not old_name or not new_name:
        raise HTTPException(status_code=422, detail="oldName and name are required")
    if req.syncMode not in {"meeting_only", "sync_voiceprint"}:
        raise HTTPException(status_code=422, detail="syncMode must be meeting_only or sync_voiceprint")

    # ``rename_meeting_speaker`` owns the durable segment update and calls the revision helper once
    # for the entire affected set. It neither reads nor mutates another meeting mode, preserving the
    # accepted realtime/import isolation boundary.
    changed_segments = store.rename_meeting_speaker(meeting_id, old_name, new_name)
    result: dict[str, Any] = {
        "meetingId": meeting_id,
        "segments": changed_segments,
        "syncMode": req.syncMode,
        "voiceprintSync": {"status": "not_requested"},
    }
    if req.syncMode == "meeting_only":
        return result
    try:
        result["voiceprintSync"] = _sync_voiceprint_person(new_name, req.department.strip())
        if result["voiceprintSync"].get("status") == "warning":
            result["warning"] = result["voiceprintSync"].get("message", "voiceprint sync needs attention")
    except Exception as exc:  # noqa: BLE001 - transcript save must survive all secondary sync failures.
        result["voiceprintSync"] = {"status": "warning", "message": str(exc)}
        result["warning"] = str(exc)
    return result


@app.post("/api/meetings/{meeting_id}/speaker-correction")
def speaker_correction(meeting_id: str, req: SpeakerCorrectionRequest) -> dict[str, Any]:
    """Persist the selected meeting correction before attempting optional voiceprint sync."""

    return correct_meeting_speaker(meeting_id, req)


AI_TOOL_UI_TITLES = {
    "reorganize": "语篇规整结果",
    "summary": "AI 摘要",
    "minutes": "会议纪要",
    "todos": "会议待办",
    "mark": "标记结果",
}

AI_TOOL_PROGRESS_STAGES = {
    "reorganize": ["读取转写片段", "重组语篇层次", "整理规整正文", "完成生成"],
    "summary": ["分析会议上下文", "提炼关键要点", "生成摘要正文", "完成生成"],
    "minutes": ["匹配纪要模板", "填充会议段落", "整理纪要正文", "完成生成"],
    "todos": ["识别任务表达", "抽取负责人和期限", "整理待办列表", "完成生成"],
    "mark": ["读取选中文本", "保存重点标记", "整理标记说明", "完成生成"],
}


def _text_lines(values: list[Any]) -> list[str]:
    """把工作流里的列表字段整理成可编辑的多行文本。

    前端右侧面板需要展示“可以直接修改”的草稿；后端各工作流返回的结构不同，
    这里集中做轻量归一化，避免前端为了摘要、纪要、待办分别硬编码太多结构细节。
    """

    lines: list[str] = []
    for value in values:
        if isinstance(value, str):
            text = value.strip()
        elif isinstance(value, dict):
            text = (
                value.get("title")
                or value.get("taskName")
                or value.get("content")
                or value.get("body")
                or value.get("name")
                or ""
            )
            owner = value.get("owner") or value.get("assignee") or value.get("ownerDept") or ""
            due = value.get("dueDate") or value.get("deadline") or ""
            suffix = " ".join(part for part in [owner, due] if part)
            text = f"{text}（{suffix}）" if suffix else text
        else:
            text = str(value)
        if text:
            lines.append(text)
    return lines


def _ai_tool_editable_text(tool: str, payload: dict[str, Any]) -> str:
    """为五个 AI 工具生成可编辑正文，服务前端的在线修改、复制和写入纪要。"""

    if tool == "summary":
        lines = [payload.get("overview") or payload.get("text") or ""]
        key_points = _text_lines(payload.get("keyPoints") or payload.get("highlights") or [])
        decisions = _text_lines(payload.get("decisionItems") or [])
        risks = _text_lines(payload.get("riskFlags") or [])
        if key_points:
            lines += ["", "关键要点：", *[f"{index + 1}. {item}" for index, item in enumerate(key_points)]]
        if decisions:
            lines += ["", "决策事项：", *[f"{index + 1}. {item}" for index, item in enumerate(decisions)]]
        if risks:
            lines += ["", "风险提醒：", *[f"{index + 1}. {item}" for index, item in enumerate(risks)]]
        return "\n".join(line for line in lines if line is not None).strip()
    if tool == "minutes":
        return str(payload.get("content") or payload.get("text") or "").strip()
    if tool == "todos":
        items = payload.get("items") or payload.get("todos") or []
        lines = _text_lines(items)
        if not lines and payload.get("text"):
            return str(payload.get("text") or "").strip()
        return "\n".join(f"{index + 1}. {item}" for index, item in enumerate(lines)).strip()
    if tool == "reorganize":
        return str(payload.get("text") or payload.get("content") or "").strip()
    if tool == "mark":
        return str(payload.get("text") or payload.get("content") or "标记已保存").strip()
    return str(payload.get("message") or payload.get("text") or "").strip()


def attach_ai_tool_ui(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """给 AI 工具接口补充前端生成面板所需的统一 UI 契约。

    这个函数只追加 `ui` 字段，不删除或改名原有业务字段；老调用仍然可以按原结构读取，
    新前端则可以用 title/editableText/progressStages/actions 渲染参考图式的生成面板。
    """

    result = dict(payload)
    result["ui"] = {
        "tool": tool,
        "title": AI_TOOL_UI_TITLES.get(tool, "AI 结果"),
        "editableText": _ai_tool_editable_text(tool, payload),
        "progressStages": AI_TOOL_PROGRESS_STAGES.get(tool, ["准备上下文", "调用 AI", "整理结果", "完成生成"]),
        "actions": {
            "copy": True,
            "regenerate": True,
            "applyToMinutes": True,
        },
    }
    return result


EMPTY_TRANSCRIPT_AI_MESSAGE = "请先开始会议识别或导入音视频文件，生成转写内容后再使用 AI 工具。"


def meeting_has_transcript_text(meeting: dict[str, Any]) -> bool:
    """判断会议是否已有可供 AI 工具分析的真实转写文本。

    快速会议刚创建时可能只有会议主题、时间等元数据，没有任何 ASR 片段。此时摘要/纪要/待办
    如果继续走 mock 或大模型兜底，会生成看似真实但并未来自本场会议的内容，所以接口层也要兜底。
    """

    return any(str(segment.get("text") or "").strip() for segment in meeting.get("segments", []))


def next_realtime_segment_id(meeting: dict[str, Any]) -> str:
    """生成跨 WebSocket 重连仍唯一的实时片段 ID。

    暂停/继续实时识别会创建新的 WebSocket，连接内的 `chunk_index` 会重新从 0 开始；
    如果直接相信 ASR 网关返回的 `rt-{meeting_id}-0`，同一场会议的新片段就会和旧片段撞 ID，
    前端按 `data-segment-id` 渲染时看起来像“下一次识别覆盖上一次”。这里以已落库会议为准
    分配下一个可用编号，保证每个实时结果都是独立片段。
    """

    meeting_id = str(meeting.get("id") or "meeting")
    existing_ids = {str(segment.get("id") or "") for segment in meeting.get("segments", [])}
    index = sum(1 for segment in meeting.get("segments", []) if str(segment.get("id") or "").startswith(f"rt-{meeting_id}-"))
    while True:
        candidate = f"rt-{meeting_id}-{index}"
        if candidate not in existing_ids:
            return candidate
        index += 1


# 这是 WebSocket 侧最后一道“不要把短碎片落库”的保护，必须和前端实时端点策略保持同一量级。
# 前端会先做能量/VAD 判断并尽量在 3 秒左右发稳定块；这里防御异常客户端或旧页面直接发送 1 秒碎片。
# 不能设置得太高，否则用户已经说完一句话还要等很久才看到文字，实时会议会退化成慢速离线转写。
REALTIME_MIN_FINAL_SPEECH_MS = 1200
REALTIME_MIN_FINAL_SEGMENT_MS = 3000


def empty_transcript_ai_payload(tool: str) -> dict[str, Any]:
    """为空会议构造与各 AI 工具兼容的提示 payload。"""

    if tool == "summary":
        return {
            "keywords": [],
            "overview": EMPTY_TRANSCRIPT_AI_MESSAGE,
            "keyPoints": [],
            "decisionItems": [],
            "riskFlags": [],
            "todos": [],
        }
    if tool == "minutes":
        return {"title": "暂无会议纪要", "content": EMPTY_TRANSCRIPT_AI_MESSAGE, "sections": []}
    if tool == "todos":
        return {"items": [], "text": EMPTY_TRANSCRIPT_AI_MESSAGE}
    return {"text": EMPTY_TRANSCRIPT_AI_MESSAGE}


@app.get("/api/dashboard/overview")
def get_dashboard_overview() -> dict[str, Any]:
    """首页概览条。

    前端概览条需要今日会议、待生成纪要、待推送待办、声纹匹配率和敏感词规则数；
    这些都是管理者打开系统第一眼要看的运行状态。
    """
    return {"items": store.dashboard_overview()}


@app.get("/api/meeting-rooms")
def list_meeting_rooms() -> dict[str, Any]:
    """列出会议室资源。"""

    return {"items": list(store.meeting_rooms.values()), "total": len(store.meeting_rooms)}


@app.post("/api/meeting-rooms/{room_id}/reserve")
def reserve_meeting_room(room_id: str, req: RoomReserveRequest) -> dict[str, Any]:
    """预定会议室。"""

    room = store.reserve_room(room_id, req.meetingName, req.reservedTime or format_datetime(), req.reservedBy)
    if not room:
        raise HTTPException(status_code=404, detail="会议室不存在")
    return room


@app.get("/api/meetings")
def list_meetings(
    search: str = "",
    status: str = "all",
    minutesStatus: str = "all",
    libraryId: str = "all",
    date: str = "",
) -> dict[str, Any]:
    """会议记录列表，字段直接对齐前端 `MeetingRecord`。"""
    items = store.list_meetings(
        search=search,
        status=status,
        minutes_status=minutesStatus,
        library_id=libraryId,
        date=date,
    )
    return {"items": items, "total": len(items)}


def _create_meeting_with_frozen_recognition_policy(req: MeetingCreateRequest, *, mode: str) -> dict[str, Any]:
    """创建新的智能会议。"""
    # Creation is the first durable template-binding boundary. Reject a blank or unknown explicit
    # ID before the store can apply its legacy default fallback, because a fallback minutes field
    # beside an invalid processing snapshot would be contradictory provenance.
    processing_config = build_processing_snapshot(req, store, mode=mode)
    template_id = str(processing_config.get("templateId") or "").strip()
    # ``tpl-001`` is the request model's long-standing omitted-field default. Some isolated
    # deployments intentionally start without seed templates and rely on the store's compatible
    # default-template fallback, so retain that legacy path only for this exact implicit default.
    # Every other missing ID is an explicit broken binding and must be rejected before persistence.
    if not template_id or (template_id not in store.templates and template_id != "tpl-001"):
        raise HTTPException(status_code=400, detail="Meeting templateId must identify an existing template")
    # The record must exist before meeting-scoped source records can be selected by its durable ID.
    meeting = store.create_meeting(
        req.meetingName,
        req.meetingLocation,
        # ``[]`` is an intentional quick-meeting choice: no domain dictionary. Passing it through instead of
        # collapsing it to None prevents unrelated government/technical defaults from biasing ordinary speech.
        keyword_library_ids=req.keywordLibraryIds,
        template_id=template_id,
        language=req.language,
        translate_direction=req.translateDirection,
        audio_source=req.audioSource,
        enable_diarization=req.enableDiarization,
        processing_config=processing_config,
    )
    # Task 1 keeps a flat sensitive-word list for legacy ASR compatibility.  Task 4 needs the
    # richer rules as well, because display, AI, and export each honor independent scope and
    # replacement settings.  Freeze them alongside the other meeting inputs before any transcript
    # arrives; later global edits must never rewrite a historical meeting's policy behavior.
    processing_config = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
    sensitive_snapshot = freeze_sensitive_rule_snapshot(store.sensitive_rules.values())
    processing_config["sensitivePolicy"] = sensitive_snapshot
    processing_config["sensitiveRuleVersion"] = sensitive_snapshot["ruleVersion"]
    meeting["processingConfig"] = processing_config
    # The store-created ID is now available, so meeting-scoped source records can be captured once
    # into the immutable policy rather than scanned again while realtime finals are arriving.
    freeze_recognition_policy_snapshot(meeting, store)
    return store._save("meetings", meeting)


def _frozen_sensitive_policy_for_meeting(meeting: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Return a meeting's immutable detailed rules, backfilling only legacy records once.

    A meeting created before Task 4 cannot recover the configuration that existed at its original
    creation time.  The first policy consumer therefore records an explicit compatibility snapshot
    and uses it from then on, rather than continuing to read mutable global configuration for every
    display refresh, AI request, or export.
    """

    processing_config = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
    snapshot = processing_config.get("sensitivePolicy")
    rules = policy_snapshot_rules(snapshot)
    if isinstance(snapshot, dict) and isinstance(snapshot.get("rules"), list):
        return meeting, rules, str(snapshot.get("ruleVersion") or processing_config.get("sensitiveRuleVersion") or "")

    snapshot = freeze_sensitive_rule_snapshot(store.sensitive_rules.values())
    # Merge only the compatibility fields into a freshly locked row.  A realtime final may commit
    # while the global rule snapshot is being built; persisting the detached ``meeting`` object
    # would erase that final and roll back transcriptRevision.
    persisted = store.backfill_processing_config(
        str(meeting["id"]),
        {
            "sensitivePolicy": snapshot,
            "sensitiveRuleVersion": snapshot["ruleVersion"],
            "sensitivePolicyFrozenAt": "legacy_first_policy_use",
        },
        guard_key="sensitivePolicy",
    )
    canonical_config = persisted.get("processingConfig") if isinstance(persisted.get("processingConfig"), dict) else {}
    canonical_snapshot = canonical_config.get("sensitivePolicy") or snapshot
    return persisted, policy_snapshot_rules(canonical_snapshot), str(canonical_snapshot.get("ruleVersion") or "")


def _policy_audit(target: str, rule_version: str, hits: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Use one additive audit shape for generated artifacts and durable export records."""

    return {"target": target, "ruleVersion": rule_version, "hits": [dict(hit) for hit in hits]}


def _attach_source_ranges(items: Any, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为章节/待办补充统一 SourceRange；优先文本命中，否则按结果顺序绑定稳定输入片段。"""

    normalized_items = [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    usable_segments = [segment for segment in segments if str(segment.get("id") or "")]
    for index, item in enumerate(normalized_items):
        if item.get("sourceRanges"):
            continue
        evidence = " ".join(
            str(item.get(key) or "").strip() for key in ("title", "content", "summary", "description")
        ).strip()
        matched = next(
            (
                segment for segment in usable_segments
                if evidence and (
                    evidence in str(segment.get("text") or "")
                    or str(segment.get("text") or "") in evidence
                )
            ),
            None,
        )
        if matched is None and usable_segments:
            matched = usable_segments[min(index, len(usable_segments) - 1)]
        item["sourceRanges"] = [] if matched is None else [
            {
                "segmentId": str(matched.get("id") or ""),
                "startMs": int(matched.get("startMs") or 0),
                "endMs": int(matched.get("endMs") or matched.get("startMs") or 0),
            }
        ]
    return normalized_items


def _persist_artifact_policy_audit(meeting_id: str, artifact_type: str, audit: dict[str, Any]) -> dict[str, Any]:
    """Attach policy evidence to an existing artifact envelope without changing legacy payloads."""

    field_by_type = {
        "summary": "summaryArtifact",
        "minutes": "minutesArtifact",
        "todos": "todosArtifact",
        "discourse": "discourseArtifact",
    }
    return store.attach_artifact_policy_audit(meeting_id, field_by_type[artifact_type], deepcopy(audit))


def _record_export_policy_audit(meeting_id: str, export_kind: str, audit: dict[str, Any]) -> None:
    """Persist export evidence separately from transcript and generated-artifact provenance."""

    store.append_export_policy_audit(
        meeting_id,
        {"exportKind": export_kind, "exportedAt": format_datetime(), "sensitivePolicy": deepcopy(audit)},
    )


def _meeting_transcription_mode(meeting: dict[str, Any]) -> str:
    """Read the immutable record mode without guessing for legacy or malformed meetings."""

    processing_config = meeting.get("processingConfig")
    return str(processing_config.get("transcriptionMode") or "") if isinstance(processing_config, dict) else ""


def _require_transcription_mode(meeting: dict[str, Any], expected_mode: str) -> None:
    """Reject a route that would cross-write import and realtime transcript ownership.

    The structured detail is shared by HTTP and WebSocket callers.  Refusing an unknown legacy
    mode is intentional: a permissive fallback would make the two durable transcript paths merge
    again and hide the source of a persisted segment.
    """

    actual_mode = _meeting_transcription_mode(meeting)
    if actual_mode != expected_mode:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "transcription_mode_mismatch",
                "expectedMode": expected_mode,
                "actualMode": actual_mode or "unknown",
                "message": f"Meeting only accepts {actual_mode or 'unknown'} transcription writes",
            },
        )


@app.post("/api/meetings")
def create_meeting(req: MeetingCreateRequest) -> dict[str, Any]:
    """Create a realtime meeting with immutable recognition inputs."""

    return _create_meeting_with_frozen_recognition_policy(req, mode="realtime")


def _recognition_policy_for_processing(meeting: dict[str, Any]) -> tuple[dict[str, Any], EffectiveVocabulary]:
    """Return a frozen policy and lazily backfill only records created before this snapshot existed.

    The legacy write is intentionally one-time. It cannot recreate a historical creation-time
    policy, so the persisted marker says ``legacy_first_processing``; after that point import,
    stream context, and final normalization all use the same immutable content without SQLite
    configuration scans on each final event.
    """

    processing_config = meeting.get("processingConfig")
    if isinstance(processing_config, dict) and isinstance(processing_config.get("recognitionPolicy"), dict):
        return meeting, build_effective_vocabulary(meeting, store)
    freeze_recognition_policy_snapshot(meeting, store, legacy_backfill=True)
    generated_config = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
    persisted = store.backfill_processing_config(
        str(meeting["id"]),
        {"recognitionPolicy": generated_config.get("recognitionPolicy", {})},
        guard_key="recognitionPolicy",
    )
    # Another request may have won the one-time backfill race.  Always rebuild from the canonical
    # locked row so every subsequent ASR chunk uses the same immutable snapshot.
    return persisted, build_effective_vocabulary(persisted, store)


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str) -> dict[str, Any]:
    """获取会议详情，包括文件、转写、摘要、纪要和待办。"""
    try:
        return store.get_meeting(meeting_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会议不存在") from None


@app.get("/api/meetings/{meeting_id}/transcript-view")
def get_transcript_view(meeting_id: str, target: str = "display") -> dict[str, Any]:
    """Return a detached policy-safe transcript view without rewriting stored source fields.

    The normal meeting route remains the editable source contract for existing integrations.  This
    additive view is intentionally separate so a browser can render masked display text without
    ever treating it as ``segment.text`` and accidentally persisting the mask back to the source.
    """

    if target != "display":
        raise HTTPException(status_code=400, detail="Transcript views support only the display target")
    meeting = store.get_or_create_meeting(meeting_id)
    meeting, rules, frozen_version = _frozen_sensitive_policy_for_meeting(meeting)
    display_segments: list[dict[str, Any]] = []
    all_hits: list[dict[str, Any]] = []
    for segment in meeting.get("segments", []):
        # Only read ``text`` here.  ``rawText`` remains provider-originated recognition evidence
        # and Task 3's normalization edits remain a different audit layer entirely.
        result = apply_sensitive_policy(str(segment.get("text") or ""), rules, "display")
        segment_hits = [dict(hit, segmentId=str(segment.get("id") or "")) for hit in result.hits]
        all_hits.extend(segment_hits)
        display_segments.append(
            {
                "id": str(segment.get("id") or ""),
                "startMs": segment.get("startMs", 0),
                "endMs": segment.get("endMs", 0),
                "speakerName": segment.get("speakerName", ""),
                "displayText": result.text,
                "sensitiveHits": segment_hits,
            }
        )
    return {
        "meetingId": meeting_id,
        "target": "display",
        "ruleVersion": frozen_version,
        "hits": all_hits,
        "segments": display_segments,
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    """查询单个后台任务状态。

    前端导入文件页和会议详情页会轮询该接口，展示上传、转码、ASR、声纹、
    对齐、纪要等步骤的进度。真实任务队列接入后，仍保持这个返回结构。
    """
    try:
        return store.get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在") from None


@app.get("/api/meetings/{meeting_id}/jobs")
def list_meeting_jobs(meeting_id: str) -> dict[str, Any]:
    """查询某个会议下所有后台任务。"""
    store.get_or_create_meeting(meeting_id)
    return {"items": store.list_jobs(meeting_id)}


@app.patch("/api/meetings/{meeting_id}")
def update_meeting(meeting_id: str, req: MeetingUpdateRequest) -> dict[str, Any]:
    """更新会议记录。

    前端后续从 localStorage 切到 API 后，可用这个接口保存纪要状态、
    处理状态、词库选择和模板切换。
    """
    try:
        return store.update_meeting(meeting_id, req.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(status_code=404, detail="会议不存在") from None


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: str) -> dict[str, Any]:
    """删除会议记录。"""
    deleted = store.delete_meeting(meeting_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="会议不存在")
    return {"deleted": True, "id": meeting_id}


@app.patch("/api/meetings/{meeting_id}/segments/batch")
def patch_meeting_segments_batch(meeting_id: str, req: SegmentBatchPatchRequest) -> dict[str, Any]:
    """原子保存逐字稿编辑区中的全部改动。

    前端可能把多个连续片段显示在同一个发言框中，但保存时仍提交每个底层片段。Store 会在
    同一个 SQLite 写事务中校验 revision、校验全部 segment id、应用修改并只递增一次版本；
    因此不会出现前几段已保存、后几段失败的半成功状态。
    """

    try:
        return store.update_meeting_segments_batch(
            meeting_id,
            expected_revision=req.expectedTranscriptRevision,
            updates=[item.model_dump(exclude_unset=True) for item in req.updates],
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="会议或转写片段不存在") from None
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "revision" in message.lower() else 400
        raise HTTPException(status_code=status_code, detail=message) from None


@app.post("/api/meetings/{meeting_id}/sensitive-policy/revisions")
def revise_meeting_sensitive_policy(meeting_id: str, req: SensitivePolicyRevisionRequest) -> dict[str, Any]:
    """为既有会议应用新禁忌词快照；只刷新派生视图，不改 ASR 原文。"""

    try:
        store.get_meeting(meeting_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会议不存在") from None
    rules = list(store.sensitive_rules.values())
    if req.ruleIds is not None:
        selected_ids = list(dict.fromkeys(str(item or "").strip() for item in req.ruleIds if str(item or "").strip()))
        known_ids = {str(item.get("id") or "") for item in rules}
        missing = [item for item in selected_ids if item not in known_ids]
        if missing:
            raise HTTPException(status_code=400, detail=f"禁忌词规则不存在：{','.join(missing)}")
        selected_set = set(selected_ids)
        rules = [item for item in rules if str(item.get("id") or "") in selected_set]
    snapshot = freeze_sensitive_rule_snapshot(rules)
    return store.apply_sensitive_policy_snapshot(meeting_id, snapshot)


@app.post("/api/meetings/{meeting_id}/retranscriptions")
def create_retranscription(meeting_id: str, req: RetranscriptionRequest) -> dict[str, Any]:
    """基于会议录音重新识别，成功后原子切换版本，失败时完整保留当前逐字稿。"""

    try:
        meeting = store.get_meeting(meeting_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会议不存在") from None
    if str(meeting.get("status") or meeting.get("processStatus") or "") != "completed":
        raise HTTPException(status_code=409, detail="只有已结束会议可以重新转写")
    files = meeting.get("files") or []
    if not files:
        raise HTTPException(status_code=409, detail="会议没有可用于重新转写的录音")
    file_id = str(files[0].get("id") or "")
    file_record = store.files.get(file_id) or files[0]
    if not file_record.get("path"):
        raise HTTPException(status_code=409, detail="会议录音文件不可用")

    # 在会议副本上组装本次明确选择的识别配置。只有新 ASR 成功后，这份快照才随逐字稿一起原子切换。
    candidate = deepcopy(meeting)
    processing_config = deepcopy(candidate.get("processingConfig") or {})
    processing_config.pop("recognitionPolicy", None)
    if req.language is not None:
        processing_config["language"] = req.language
        candidate["language"] = req.language
    if req.enableDiarization is not None:
        processing_config["enableDiarization"] = req.enableDiarization
    if req.optimizationProfile is not None:
        processing_config["optimizationProfile"] = deepcopy(req.optimizationProfile)
    if req.documentKeywordDocumentIds is not None:
        processing_config["documentKeywordDocumentIds"] = list(req.documentKeywordDocumentIds)
        candidate["documentKeywordDocumentIds"] = list(req.documentKeywordDocumentIds)
    if req.confirmedSmartTerms is not None:
        processing_config["confirmedSmartTerms"] = list(req.confirmedSmartTerms)
        candidate["smartKeywordTerms"] = [
            {"term": term, "confirmed": True} for term in req.confirmedSmartTerms if str(term or "").strip()
        ]
    candidate["processingConfig"] = processing_config
    policy = freeze_recognition_policy_snapshot(candidate, store)
    candidate_config = candidate.get("processingConfig") or {}
    asr_inputs = get_meeting_asr_inputs(candidate)
    job = store.create_job(meeting_id, "retranscription", "重新转写会议录音", ["queued", "asr", "switch", "completed"])
    store.update_job(job["id"], status="running", current_step="asr", progress=35)
    try:
        result = asr_gateway.transcribe_offline(
            meeting_id=meeting_id,
            file_id=file_id,
            enable_diarization=bool(asr_inputs.get("enableDiarization")),
            hotwords=list(policy.words),
            sensitive_words=asr_inputs.get("sensitiveWords") or [],
            file_path=file_record.get("path"),
            language=asr_inputs.get("language") or "zh",
        )
        raw_segments = result.get("segments") or []
        normalized_segments = [_normalize_final_segment_with_policy(segment, policy) for segment in raw_segments]
        if result.get("status") == "failed" or not any(str(item.get("text") or "").strip() for item in normalized_segments):
            raise RuntimeError(result.get("message") or "ASR 未返回有效逐字稿")
        store.update_job(job["id"], status="running", current_step="switch", progress=85)
        switched = store.replace_transcript_from_retranscription(
            meeting_id,
            file_id,
            normalized_segments,
            candidate_config.get("recognitionPolicy") or {},
        )
        store.update_job(job["id"], status="completed", current_step="completed", progress=100)
        return {"status": "completed", "jobId": job["id"], **switched}
    except Exception as exc:  # noqa: BLE001 - 外部 ASR 失败必须转成任务失败且不得触碰当前版本。
        store.update_job(job["id"], status="failed", current_step="asr", progress=100, message=str(exc))
        return {"status": "failed", "jobId": job["id"], "message": str(exc), "currentTranscriptPreserved": True}


@app.patch("/api/meetings/{meeting_id}/segments/{segment_id}")
def patch_meeting_segment(meeting_id: str, segment_id: str, req: SegmentPatchRequest) -> dict[str, Any]:
    """更新会议详情页中单条转写片段。

    该接口服务于兼容旧客户端的单片段编辑；新版工作台使用上方 batch 路由，避免多段编辑
    产生多个 revision 或半成功。静态 batch 路由必须注册在动态 segment_id 路由之前。
    """
    segment = store.update_meeting_segment(meeting_id, segment_id, req.model_dump(exclude_unset=True))
    if not segment:
        raise HTTPException(status_code=404, detail="转写片段不存在")
    return segment


@app.post("/api/meetings/{meeting_id}/tools/speaker-summary")
def generate_speaker_summary(meeting_id: str) -> dict[str, Any]:
    """生成发言人总结。

    当前按发言片段数量和文本长度做轻量汇总；真实接大模型后可按人生成观点、任务和风险。
    """
    meeting = store.get_or_create_meeting(meeting_id)
    meeting, rules, _rule_version = _frozen_sensitive_policy_for_meeting(meeting)
    # Speaker summaries are an AI/tool output surface even when the local fallback performs the
    # grouping.  Build it exclusively from the detached AI target so its behavior matches remote
    # generation and never reads the durable source text directly.
    ai_meeting, policy_audit = prepare_sensitive_ai_meeting(meeting, rules)
    grouped: dict[str, dict[str, Any]] = {}
    for segment in ai_meeting.get("segments", []):
        name = segment.get("speakerName") or "未识别发言人"
        item = grouped.setdefault(name, {"speakerName": name, "segmentCount": 0, "summary": "", "sourceRanges": []})
        item["segmentCount"] += 1
        item["summary"] = (item["summary"] + " " + segment.get("text", "")).strip()[:160]
        # 发言总结必须能回到逐字稿和录音；即使未来替换为大模型总结，也保留输入片段的稳定来源范围。
        item["sourceRanges"].append(
            {
                "segmentId": str(segment.get("id") or ""),
                "startMs": int(segment.get("startMs") or 0),
                "endMs": int(segment.get("endMs") or segment.get("startMs") or 0),
            }
        )
    result = {"items": list(grouped.values()), "source": "mock_llm_workflow", "sensitivePolicy": policy_audit}
    # This tool predates the generic derived-artifact registry.  Persist an additive envelope
    # rather than changing its legacy response shape, retaining the frozen rule version and hits
    # needed to audit exactly what the local or future remote tool received.
    persisted = store.get_or_create_meeting(meeting_id)
    persisted["speakerSummaryArtifact"] = {"generatedAt": format_datetime(), **deepcopy(result)}
    store._save("meetings", persisted)
    return result


async def save_uploaded_audio_file(meeting_id: str, file: UploadFile) -> dict[str, Any]:
    """保存上传音视频并写入文件流水。

    导入转写和会议详情上传都需要同一套落盘逻辑。抽成函数后可以保证安全文件名、上传目录、
    content-type 和 file_pipeline 任务创建完全一致，避免两个入口状态不一致。
    """

    # Validate before consuming the request body so an upload already known to target a deleted
    # meeting does not create a disk artifact. ``store.save_file`` repeats the check transactionally
    # to close the smaller race where deletion commits between this read and the database write.
    try:
        store.get_meeting(meeting_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会议不存在") from None
    safe_name = Path(file.filename or "meeting-audio").name
    file_path = UPLOAD_DIR / f"{meeting_id}-{safe_name}"
    file_path.write_bytes(await file.read())
    try:
        return store.save_file(meeting_id, safe_name, file_path, file.content_type or "")
    except KeyError:
        # Only this exact upload is removed; no directory or recursive deletion is performed.
        if file_path.is_file():
            file_path.unlink()
        raise HTTPException(status_code=404, detail="会议不存在") from None


def _is_generic_speaker_name(name: str | None) -> bool:
    """判断片段里的发言人名是否只是 ASR/实时链路给出的占位名。

    声纹匹配只能覆盖“待匹配发言人、实时发言人、speaker_0”这类没有真实身份的信息；
    如果用户已经手工改成了“张三”或声纹服务已经给出具体姓名，就不要被后续兜底逻辑误覆盖。
    """

    normalized = (name or "").strip().lower()
    if not normalized:
        return True
    generic_keywords = ["待匹配", "未识别", "实时发言人", "发言人", "speaker", "unknown", "unknown speaker"]
    return any(keyword in normalized for keyword in generic_keywords)


def _active_voiceprint_ids() -> set[str]:
    """返回当前业务库里仍然有效的声纹 ID。

    本地 CAM++ 服务维护的是向量库，可能还残留已删除人员的 embedding；业务后端必须以自己的声纹库
    为准做二次过滤，避免“已经删除的人”仍被自动识别到会议里。
    """

    return {
        item.get("id", "")
        for item in store.voiceprints.values()
        # Eligibility has three independent conditions. A status string without an embedding is
        # legacy/mock metadata, and an embedding without a registered enabled profile must not
        # re-enter matching merely because the model service still retains that vector.
        if item.get("enabled") is not False
        and item.get("registerStatus") == "registered"
        and has_valid_embedding_id(item)
    }


def _normalize_voiceprint_candidate(candidate: dict[str, Any], source: str) -> dict[str, Any] | None:
    """把声纹服务的一条候选结果统一成会议片段可直接使用的字段。"""

    speaker_name = candidate.get("speakerName") or candidate.get("speaker_name") or candidate.get("name")
    voiceprint_id = candidate.get("voiceprintId") or candidate.get("speakerId") or candidate.get("speaker_id") or ""
    profile = store.voiceprints.get(voiceprint_id)
    if not profile and speaker_name:
        profile = next(
            (
                item
                for item in store.voiceprints.values()
                if item.get("enabled") is not False
                and item.get("registerStatus") == "registered"
                and has_valid_embedding_id(item)
                and (item.get("name") or item.get("speakerName")) == speaker_name
            ),
            None,
        )
    if profile:
        # 以业务声纹库资料为准展示姓名和部门/职位，避免模型服务只返回旧姓名或缺少人员信息。
        speaker_name = profile.get("name") or profile.get("speakerName") or speaker_name
        voiceprint_id = profile.get("id", voiceprint_id)
    if not speaker_name:
        return None
    return {
        "speakerName": speaker_name,
        "speakerTitle": (profile or {}).get("department") or candidate.get("department") or candidate.get("speakerTitle") or "",
        "voiceprintId": voiceprint_id,
        "voiceprintConfidence": float(candidate.get("confidence") or candidate.get("score") or 0),
        "speakerSource": candidate.get("source") or source or "voiceprint_match",
    }


def _first_voiceprint_match(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """从不同声纹服务返回格式里抽取最可信的第一名有效候选人。

    本地 CAM++/3D-Speaker 服务在联调阶段可能返回 `matches[0]`，也可能直接返回
    `speakerName/voiceprintId/confidence`。这里统一归一化，并过滤掉业务库中不存在或已停用的声纹 ID。
    """

    if not payload:
        return None
    candidates = payload.get("matches") or payload.get("items") or [payload]
    if not candidates:
        return None
    active_ids = _active_voiceprint_ids()
    normalized_candidates = [
        _normalize_voiceprint_candidate(candidate, payload.get("source") or "voiceprint_match")
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    for candidate in normalized_candidates:
        if candidate and candidate.get("voiceprintId") in active_ids:
            return candidate
    # 少数网关只返回姓名不返回 ID，这种情况下无法做 ID 校验，只能在姓名仍存在于业务库时接受。
    active_names = {
        item.get("name") or item.get("speakerName")
        for item in store.voiceprints.values()
        if item.get("enabled") is not False
        and item.get("registerStatus") == "registered"
        and has_valid_embedding_id(item)
    }
    for candidate in normalized_candidates:
        if candidate and not candidate.get("voiceprintId") and candidate.get("speakerName") in active_names:
            return candidate
    return None


def match_voiceprint_for_audio(audio_path: str) -> dict[str, Any] | None:
    """把一段音频提交给本地声纹服务，返回最匹配的已注册人员。

    没有配置本地声纹服务时不做猜测；配置了服务但临时不可用时，如果系统里只有一个已注册且启用的声纹，
    才使用低置信度兜底，保证单人演示和本地联调仍能看到“提前录入声纹 -> 自动识别人”的闭环。
    """

    if not audio_path:
        return None
    if VOICEPRINT_GATEWAY_BASE_URL:
        try:
            # 模型服务的向量库可能残留历史 embedding，所以不能只取 top1；取一组候选后再按业务声纹库过滤。
            matched = LocalVoiceprintClient(VOICEPRINT_GATEWAY_BASE_URL).match(audio_path=audio_path, top_k=10)
            return _first_voiceprint_match(matched)
        except Exception:
            # 声纹服务失败不应该中断 ASR 文本交付；下面只在单一注册人员场景做可解释兜底。
            pass
    # Do not guess a realtime/import speaker merely because the business library has one registered person.
    # That fallback made demos look convenient, but in real meetings it can label a new speaker as an unrelated
    # existing person. Without a voiceprint gateway result, leave the speaker as ASR/diarization produced it.
    return None


def _analyze_realtime_speaker_wav(wav_bytes: bytes) -> tuple[list[float] | None, dict[str, Any] | None]:
    """同步调用本地模型得到 embedding 与可信声纹候选，供 ``asyncio.to_thread`` 使用。

    模型客户端使用普通 HTTP，请求不能直接运行在 FastAPI WebSocket 事件循环里，否则声纹服务
    一次慢响应就会拖住后续 partial/final 文本。临时 WAV 只存在于本函数生命周期，并按精确路径
    单文件删除；embedding 只返回给会议内 tracker，不写入 Store 或浏览器事件。
    """

    if not wav_bytes or not VOICEPRINT_GATEWAY_BASE_URL:
        return None, None
    with tempfile.NamedTemporaryFile(prefix="realtime-speaker-", suffix=".wav", dir=AUDIO_CLIP_DIR, delete=False) as tmp:
        tmp.write(wav_bytes)
        audio_path = tmp.name
    try:
        client = LocalVoiceprintClient(VOICEPRINT_GATEWAY_BASE_URL)
        embedding: list[float] | None = None
        matched: dict[str, Any] | None = None
        try:
            embedding_payload = client.embedding(audio_path=audio_path)
            embedding = list(embedding_payload.get("embedding") or []) or None
        except Exception:
            # embedding 失败时仍允许独立的库匹配返回姓名；tracker 会把可信匹配升级到当前
            # fallback cluster。两条模型能力彼此隔离，任何一条失败都不能阻断 ASR 文本。
            embedding = None
        try:
            matched = _first_voiceprint_match(client.match(audio_path=audio_path, top_k=10))
        except Exception:
            matched = None
        return embedding, matched
    finally:
        Path(audio_path).unlink(missing_ok=True)


def _speaker_identity_event_fields(identity: SpeakerIdentity) -> dict[str, Any]:
    """显式挑选可公开身份字段，确保原始 embedding 永远不会进入 WebSocket。"""

    return {
        "speakerName": identity.speaker_name,
        "speakerTitle": identity.speaker_title or "",
        "speakerClusterId": identity.speaker_cluster_id,
        "speakerSource": identity.speaker_source,
        "voiceprintId": identity.voiceprint_id or "",
        "voiceprintConfidence": identity.confidence,
    }


def _segment_overlap_ms(left: dict[str, Any], right: dict[str, Any]) -> int:
    """计算两个时间片段的重叠毫秒数，用于把 diarization 结果贴回 ASR 文本片段。"""

    left_start = int(left.get("startMs") or left.get("start_ms") or 0)
    left_end = int(left.get("endMs") or left.get("end_ms") or left_start)
    right_start = int(right.get("startMs") or right.get("start_ms") or 0)
    right_end = int(right.get("endMs") or right.get("end_ms") or right_start)
    return max(0, min(left_end, right_end) - max(left_start, right_start))


def _diarization_speaker_key(segment: dict[str, Any]) -> str:
    """从说话人分离结果里提取稳定的 speaker key。

    不同 3D-Speaker/FunASR 封装可能返回 `speaker`、`speaker_id`、`label` 等字段；
    这里统一收敛成一个 key，后续才能稳定映射成“发言人1、发言人2”。
    """

    return str(
        segment.get("speaker")
        or segment.get("speakerId")
        or segment.get("speaker_id")
        or segment.get("label")
        or segment.get("speakerName")
        or segment.get("speaker_name")
        or ""
    ).strip()


def _audio_window_for_voiceprint(audio_path: str, start_ms: int, end_ms: int) -> str:
    """切出某个说话人的代表音频窗口，供 CAM++ 做声纹匹配。

    说话人分离只告诉我们“哪段属于同一个 speaker key”，并不天然知道这个人是谁；
    因此需要从原始音频切一小段代表音频送入声纹库匹配。切片失败时抛异常，由上层保留
    “发言人1/2”的编号展示，不影响转写文本。
    """

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，无法切出声纹匹配音频窗口")
    duration_ms = max(500, end_ms - start_ms)
    with tempfile.NamedTemporaryFile(prefix="voiceprint-window-", suffix=".wav", dir=AUDIO_CLIP_DIR, delete=False) as tmp:
        output_path = tmp.name
    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-i",
        audio_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        output_path,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
        return output_path
    except Exception:
        Path(output_path).unlink(missing_ok=True)
        raise


def _build_diarization_voiceprint_map(
    diarization_segments: list[dict[str, Any]],
    audio_path: str,
) -> dict[str, dict[str, Any]]:
    """按 diarization speaker key 尝试匹配声纹库人员。

    为了避免“一段整场音频 top1 把所有人都覆盖成同一个姓名”，这里按 speaker key 选最长的代表片段
    单独匹配。低置信度兜底命中只允许使用一次，防止多发言人会议被同一个单人兜底全部吞掉。
    """

    if not audio_path or not VOICEPRINT_GATEWAY_BASE_URL:
        return {}
    representatives: dict[str, dict[str, Any]] = {}
    for item in diarization_segments:
        key = _diarization_speaker_key(item)
        if not key:
            continue
        current = representatives.get(key)
        duration = _segment_overlap_ms(
            {"startMs": item.get("startMs") or item.get("start_ms") or 0, "endMs": item.get("endMs") or item.get("end_ms") or 0},
            item,
        )
        current_duration = _segment_overlap_ms(
            {"startMs": (current or {}).get("startMs") or (current or {}).get("start_ms") or 0, "endMs": (current or {}).get("endMs") or (current or {}).get("end_ms") or 0},
            current or {},
        )
        if not current or duration > current_duration:
            representatives[key] = item

    mapped: dict[str, dict[str, Any]] = {}
    used_voiceprint_ids: set[str] = set()
    for key, item in representatives.items():
        start_ms = int(item.get("startMs") or item.get("start_ms") or 0)
        end_ms = int(item.get("endMs") or item.get("end_ms") or start_ms)
        clip_path = ""
        try:
            clip_path = _audio_window_for_voiceprint(audio_path, start_ms, end_ms)
            matched = match_voiceprint_for_audio(clip_path)
        except Exception:
            matched = None
        finally:
            if clip_path:
                Path(clip_path).unlink(missing_ok=True)
        if not matched:
            continue
        voiceprint_id = matched.get("voiceprintId", "")
        confidence = float(matched.get("voiceprintConfidence") or 0)
        if voiceprint_id and voiceprint_id in used_voiceprint_ids and confidence < 0.85:
            continue
        if voiceprint_id:
            used_voiceprint_ids.add(voiceprint_id)
        mapped[key] = matched
    return mapped


def apply_voiceprint_match_to_segments(
    segments: list[dict[str, Any]],
    audio_path: str,
    diarization_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把声纹服务输出合并到 ASR 片段。

    优先使用 diarization 返回的逐时间段人员；如果没有逐段结果，再用整段音频的 top1 匹配补齐占位发言人。
    这样导入转写和实时会议共享同一套“先识别文字、再识别人”的逻辑，前端只需要渲染 segments。
    """

    patched = [dict(segment) for segment in segments]
    diarization_segments = (diarization_result or {}).get("segments") or []
    speaker_aliases: dict[str, str] = {}
    speaker_voiceprints = _build_diarization_voiceprint_map(diarization_segments, audio_path) if diarization_segments else {}
    if diarization_segments:
        for segment in patched:
            best = max(diarization_segments, key=lambda item: _segment_overlap_ms(segment, item), default=None)
            if best and _segment_overlap_ms(segment, best) > 0:
                speaker_key = _diarization_speaker_key(best)
                matched_voiceprint = speaker_voiceprints.get(speaker_key)
                if matched_voiceprint:
                    segment.update(matched_voiceprint)
                    segment["speakerClusterId"] = f"voiceprint-{matched_voiceprint.get('voiceprintId') or speaker_key}"
                    segment["speakerSource"] = "diarization_voiceprint_match"
                    continue
                speaker_name = best.get("speakerName") or best.get("speaker_name") or best.get("name")
                if not speaker_name:
                    # 没有命中声纹库时，仍然按 diarization speaker key 稳定编号，避免多人会议全显示“实时发言人”。
                    speaker_name = speaker_aliases.setdefault(speaker_key or f"unknown-{len(speaker_aliases) + 1}", f"发言人{len(speaker_aliases) + 1}")
                segment["speakerName"] = speaker_name
                # 展示层合并优先使用稳定 cluster ID。若只改姓名而保留每句不同的 pending ID，
                # 同一位发言人的连续段落仍会显示成多个框，违背“底层不合并、展示可合并”的契约。
                segment["speakerClusterId"] = f"diarization-{speaker_key}"
                segment["voiceprintId"] = best.get("voiceprintId") or best.get("speakerId") or best.get("speaker_id") or ""
                segment["voiceprintConfidence"] = float(best.get("confidence") or best.get("score") or 0)
                segment["speakerSource"] = "diarization"
    whole_file_match = match_voiceprint_for_audio(audio_path)
    if whole_file_match:
        for segment in patched:
            # 多人 diarization 已经把片段区分开时，不能再用整场 top1 覆盖所有人；
            # 只有没有分离结果，或单片段场景，才用整段声纹兜底。
            if (not diarization_segments and _is_generic_speaker_name(segment.get("speakerName"))) or len(patched) == 1:
                segment.update(whole_file_match)
    return patched


@app.post("/api/meetings/{meeting_id}/files")
async def upload_file(meeting_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """上传会议音视频文件。

    支持格式由前端提示和后续 ffmpeg 标准化服务保障；这里先保存原文件，
    转写时由 ASR 网关或转码任务读取。
    """

    return await save_uploaded_audio_file(meeting_id, file)


@app.post("/api/meetings/{meeting_id}/attachments")
async def upload_meeting_attachment(meeting_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """保存快速会议的普通附件，不把文档误当成待转写音频。

    旧页面虽然展示附件选择框，却没有上传动作。附件与录音必须使用不同契约，否则一个 docx 会进入
    音频播放和 ASR 文件流水。这里只追加会议内嵌元数据，并使用安全文件名保存单个文件。
    """

    try:
        store.get_meeting(meeting_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会议不存在") from None
    safe_name = Path(file.filename or "meeting-attachment").name
    attachment_id = f"att-{uuid.uuid4().hex[:10]}"
    file_path = UPLOAD_DIR / f"{meeting_id}-{attachment_id}-{safe_name}"
    data = await file.read()
    file_path.write_bytes(data)
    attachment = {
        "id": attachment_id,
        "name": safe_name,
        "contentType": file.content_type or "application/octet-stream",
        "size": len(data),
        "path": str(file_path),
        "uploadedAt": format_datetime(),
    }
    try:
        return store.add_meeting_attachment(meeting_id, attachment)
    except KeyError:
        # 只清理由本次请求创建的单个明确文件，遵守项目禁止递归/批量删除的约束。
        if file_path.is_file():
            file_path.unlink()
        raise HTTPException(status_code=404, detail="会议不存在") from None


@app.post("/api/imports/transcribe")
async def import_and_transcribe_file(
    file: UploadFile = File(...),
    language: str = Form("中文普通话"),
    template_id: str = Form(""),
    keyword_library_ids: str = Form(""),
    enable_diarization: bool = Form(True),
    participant_names: str = Form(""),
    voiceprint_group_id: str = Form(""),
    optimization_profile: str = Form(""),
    notes: str = Form(""),
    attachments: str = Form(""),
    document_keyword_document_ids: str = Form(""),
) -> dict[str, Any]:
    """导入转写页的一站式入口。

    用户在“导入转写”页的心智是提交一个音频转写任务，不是先手工创建会议。后端仍然需要内部会议记录
    来承载转写、纪要、待办和导出，但这个细节只作为返回数据存在，不再要求前端自己编排“创建会议 -> 上传 ->
    转写”三段流程，也不会在页面上暴露“会议已创建”的误导性提示。
    """

    safe_name = Path(file.filename or "meeting-audio").name
    library_ids = [item.strip() for item in keyword_library_ids.split(",") if item.strip()]
    # Import clients historically send only simple form fields. JSON fields remain optional so
    # those requests stay valid while richer clients can freeze the same configuration as realtime.
    def parse_import_json(raw_value: str, expected_type: type, default: Any) -> Any:
        # Direct unit callers invoke this route like an ordinary coroutine. For omitted FastAPI
        # parameters, Python supplies the Form descriptor rather than a submitted string; treat it
        # as the same empty optional value an HTTP request would produce instead of parsing it.
        if not isinstance(raw_value, (str, bytes, bytearray)):
            return default
        try:
            value = json.loads(raw_value) if raw_value else default
        except json.JSONDecodeError:
            return default
        return value if isinstance(value, expected_type) else default

    def optional_import_string(raw_value: str) -> str:
        """Normalize omitted direct-call Form defaults to the empty HTTP form value."""

        return raw_value if isinstance(raw_value, str) else ""

    import_request = MeetingCreateRequest(
        meetingName=safe_name,
        audioSource="上传文件",
        language=language,
        # 旧调用方可能省略模板字段；导入转写应冻结当前默认模板，而不是用空字符串创建无效快照。
        templateId=optional_import_string(template_id)
        or next((item["id"] for item in store.templates.values() if item.get("isDefault")), "tpl-001"),
        keywordLibraryIds=library_ids,
        enableDiarization=enable_diarization,
        participantNames=parse_import_json(participant_names, list, []),
        voiceprintGroupId=optional_import_string(voiceprint_group_id),
        optimizationProfile=parse_import_json(optimization_profile, dict, {}),
        notes=optional_import_string(notes),
        attachments=parse_import_json(attachments, list, []),
        documentKeywordDocumentIds=[
            str(item).strip()
            for item in parse_import_json(document_keyword_document_ids, list, [])
            if str(item).strip()
        ],
    )
    # Import records remain separate from realtime records, but use the same creation-time freeze
    # helper with ``import`` mode so provider hotwords and final normalization stay historical.
    meeting = _create_meeting_with_frozen_recognition_policy(import_request, mode="import")
    file_record = await save_uploaded_audio_file(meeting["id"], file)
    transcription = transcribe_file(file_record["id"], TranscribeRequest(enableDiarization=enable_diarization))
    meeting_detail = store.get_or_create_meeting(meeting["id"])
    # transcribe_file 会把文件 pipeline/status 更新为 completed，这里重新读取一次，避免前端拿到上传瞬间的旧状态。
    return {"meeting": meeting_detail, "file": store.files.get(file_record["id"], file_record), "transcription": transcription}


@app.post("/api/files/{file_id}/transcribe")
def transcribe_file(file_id: str, req: TranscribeRequest) -> dict[str, Any]:
    """对上传文件执行离线转写。"""
    file_record = store.files.get(file_id)
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")
    meeting_id = file_record["meetingId"]
    meeting = store.get_or_create_meeting(meeting_id)
    # Files may also be stored as non-transcription attachments.  Enforce ownership at the
    # transcription boundary so a generic upload can never turn a realtime record into an import
    # transcript or append an import batch into the realtime segment history.
    _require_transcription_mode(meeting, "import")
    # Import work may start after an administrator edits a global dictionary. Read only the
    # already-persisted meeting snapshot so the file keeps the vocabulary and masking rules that
    # were effective when this import record was created, rather than inheriting later edits.
    meeting, recognition_policy = _recognition_policy_for_processing(meeting)
    asr_inputs = get_meeting_asr_inputs(meeting)
    # Offline import and realtime use this exact policy builder. The resulting object is immutable
    # for the duration of this request and its hash is copied onto final segments for later audit.
    frozen_diarization = asr_inputs["enableDiarization"]
    enable_diarization = req.enableDiarization if frozen_diarization is None else bool(frozen_diarization)
    if not MODEL_MOCK_MODE and ASR_GATEWAY_MODE not in {"remote", "dashscope"}:
        job = store.create_job(
            meeting_id=meeting_id,
            job_type="offline_transcribe",
            title=f"离线转写 {file_record.get('filename', file_id)}",
            steps=["uploaded", "waiting_model_config"],
        )
        store.update_job(
            job["id"],
            status="waiting_model_config",
            current_step="waiting_model_config",
            progress=0,
            message="未配置 Qwen3-ASR 模型服务。请设置 ASR_GATEWAY_MODE=dashscope 或 remote，并配置对应 Key/服务地址。",
        )
        return {"status": "waiting_model_config", "jobId": job["id"], "message": job["message"], "segments": []}
    job = store.create_job(
        meeting_id=meeting_id,
        job_type="offline_transcribe",
        title=f"离线转写 {file_record.get('filename', file_id)}",
        steps=["uploaded", "transcoding", "asr", "voiceprint", "alignment", "minutes", "completed"],
    )
    store.update_job(job["id"], status="running", current_step="asr", progress=45)
    vad_result: dict[str, Any] | None = None
    if VAD_GATEWAY_BASE_URL:
        try:
            # VAD 是 ASR 前置切分能力。当前 DashScope 网关仍会按整文件识别；
            # 但这里先把 VAD 服务调用和结果结构打通，后续可按 vadSegments 逐段送 ASR。
            vad_result = LocalVadClient(VAD_GATEWAY_BASE_URL).split(file_record.get("path", ""))
            store.update_job(job["id"], status="running", current_step="vad", progress=30)
        except Exception as exc:
            # VAD 失败不直接阻断 mock/整文件识别，避免本地联调时因为小模型服务未启动导致 ASR 主链路不可用。
            # 关闭 mock 且正式部署时，可把这里升级为 failed 或 waiting_model_config。
            vad_result = {"status": "failed", "message": str(exc)}
    try:
        result = asr_gateway.transcribe_offline(
            meeting_id=meeting_id,
            file_id=file_id,
            enable_diarization=enable_diarization,
            hotwords=list(recognition_policy.words),
            sensitive_words=asr_inputs["sensitiveWords"],
            start_ms=req.startMs,
            end_ms=req.endMs,
            # DashScope、本地 Qwen3-ASR 或 910B 远程服务都需要知道原始文件位置。
            # mock 网关会忽略该字段；真实网关会用它读取本地文件或生成可访问 URL。
            file_path=file_record.get("path"),
            language=asr_inputs["language"] or "zh",
        )
    except Exception as exc:  # noqa: BLE001 - ASR 是外部模型链路，失败时要返回可展示状态而不是 500。
        message = str(exc)
        # 这里以前会用 MockQwenAsrGateway 生成三段假转写并标记 completed，导致用户以为“识别成功但内容不对”。
        # 现在真实 ASR 已在网关层做过瞬断重试；如果仍失败，必须如实标记失败，保留文件和会议记录供用户重试/排查。
        result = {
            "status": "failed",
            "model": "Qwen3-ASR-1.7B",
            "fileId": file_id,
            "meetingId": meeting_id,
            "segments": [],
            "message": f"真实 ASR 调用失败：{message}",
        }
        store.update_job(job["id"], status="failed", current_step="asr", progress=100, message=result["message"])
        file_record["pipelineStatus"] = "failed"
        file_record["status"] = "failed"
        store._save("files", file_record)
        for pipeline_job in store.list_jobs(meeting_id):
            if pipeline_job["type"] == "file_pipeline" and pipeline_job["status"] in {"pending", "running"}:
                store.update_job(
                    pipeline_job["id"],
                    status="failed",
                    current_step="asr",
                    progress=100,
                    message=result["message"],
                )
        store.update_meeting(meeting_id, {"processStatus": "failed", "status": "failed"})
        return {"transcriptId": "", "jobId": job["id"], **result}
    diarization_result: dict[str, Any] | None = None
    if enable_diarization and VOICEPRINT_GATEWAY_BASE_URL:
        try:
            # 说话人分离属于 ASR 之后的增强步骤：ASR 负责文本，3D-Speaker 负责“谁在什么时间段说话”。
            # 当前先把 diarization 结果随接口返回，前端可据此展示声纹区分状态；后续可在这里把分段结果
            # 和 ASR 片段做时间重叠合并，自动把“说话人 1/2”替换为具体声纹库姓名。
            store.update_job(job["id"], status="running", current_step="voiceprint", progress=65)
            diarization_result = LocalVoiceprintClient(VOICEPRINT_GATEWAY_BASE_URL).diarize(
                audio_path=file_record.get("path", ""),
                min_speakers=None,
                max_speakers=None,
            )
        except Exception as exc:
            # 声纹分离失败时不丢弃 ASR 文本。离线转写可先交付文字，页面再提示管理员检查 3D-Speaker 服务。
            diarization_result = {"status": "failed", "message": str(exc)}
    if enable_diarization:
        # ASR 结果里的 speakerName 往往只是“speaker_0/待匹配发言人”。这里统一贴上声纹库匹配结果，
        # 让导入转写页和实时会议详情页都能直接显示“王忠/张三”这类提前录入的人名。
        result["segments"] = apply_voiceprint_match_to_segments(
            result.get("segments", []),
            file_record.get("path", ""),
            diarization_result,
        )
    # ASR adapters return provider-originated text. Replacement rules must run only after an
    # utterance is final and immediately before durable import persistence; doing this earlier
    # would mutate unstable partial hypotheses or erase the original provider transcript.
    result["segments"] = [
        _normalize_final_segment_with_policy(segment, recognition_policy) for segment in result.get("segments", [])
    ]
    result["recognitionPolicyHash"] = recognition_policy.snapshot_hash
    transcript = store.add_transcript(meeting_id, file_id, result["segments"])
    store.update_job(job["id"], status="completed", current_step="completed", progress=100)
    if vad_result is not None:
        result["vad"] = vad_result
    if diarization_result is not None:
        result["diarization"] = diarization_result
    return {"transcriptId": transcript["id"], "jobId": job["id"], **result}


def _transcribe_realtime_chunk_with_context(
    meeting_id: str,
    chunk_index: int,
    audio_chunk: bytes,
    sensitive_words: list[str],
    mime_type: str,
    duration_ms: int,
    context_text: str,
) -> dict[str, Any]:
    """调用实时 ASR 网关，并兼容仍未支持 context_text 的测试/自部署实现。

    这层兼容看起来有点“绕”，但它能让我们在生产 DashScope 网关上立刻使用上下文提示，
    同时不破坏旧的 Mock/Remote/单元测试假网关。等所有自部署实时网关都实现了
    context_text 参数后，可以把 TypeError 兜底删掉，直接强制契约。
    """

    try:
        return asr_gateway.transcribe_realtime_chunk(
            meeting_id,
            chunk_index,
            audio_chunk,
            sensitive_words,
            mime_type=mime_type,
            duration_ms=duration_ms,
            context_text=context_text,
        )
    except TypeError as exc:
        if "context_text" not in str(exc):
            raise
        return asr_gateway.transcribe_realtime_chunk(
            meeting_id,
            chunk_index,
            audio_chunk,
            sensitive_words,
            mime_type=mime_type,
            duration_ms=duration_ms,
        )


def _is_realtime_context_echo(text: str, meeting: Mapping[str, Any]) -> bool:
    """兼容旧内部调用，实际规则由 recognition_policy 的统一实现维护。"""

    return is_realtime_context_echo(text, meeting)


def _finalize_realtime_transcript_event(
    meeting_id: str,
    event: dict[str, Any],
    session_token: str,
    *,
    lease_owner_id: str | None = None,
) -> dict[str, Any]:
    """Store one final realtime transcript event and make it frontend-safe.

    Both realtime implementations use this helper: the new provider-native stream path and the old synchronous
    chunk fallback. It centralizes ID/session handling so partial preview can be fast while final text remains
    durable and does not overwrite earlier segments after reconnects.
    """

    event.setdefault("meetingId", meeting_id)
    event["sessionToken"] = session_token
    if event.get("type") != "transcript" or not event.get("segment", {}).get("text"):
        return event
    if lease_owner_id is not None and not _realtime_leases.is_owner(
        meeting_id,
        owner_id=lease_owner_id,
        session_token=session_token,
    ):
        # 后来的连接已经接管同一 meeting 时，旧 provider 仍可能迟到一个 final。这里是所有
        # 实时路径共用的最终写入边界，拒绝旧租约可确保不会产生重复正文或错误 revision。
        return {
            "type": "status",
            "code": "realtime_session_superseded",
            "meetingId": meeting_id,
            "sessionToken": session_token,
            "message": "当前实时连接已被同一会议的新连接接管，旧结果未写入",
        }
    meeting = store.get_or_create_meeting(meeting_id)
    _require_transcription_mode(meeting, "realtime")
    # Context echoes are rejected before policy construction, final normalization, durable segment
    # ID allocation, and store mutation. Keeping this guard at the persistence boundary protects
    # both provider-native streaming and the synchronous fallback without consuming a transcript
    # revision for metadata that was never spoken.
    if _is_realtime_context_echo(str(event["segment"].get("text") or ""), meeting):
        return {
            "type": "status",
            "code": "context_echo_filtered",
            "meetingId": meeting_id,
            "sessionToken": session_token,
            "message": "已过滤与会议标题相同的实时识别上下文回声",
        }
    # Realtime final events and import segments share the same final-only normalization helper.
    # The two flows still create independent records through their existing store operations.
    meeting, recognition_policy = _recognition_policy_for_processing(meeting)
    event["segment"] = _normalize_final_segment_with_policy(event["segment"], recognition_policy)
    event["segment"]["realtimeSessionToken"] = session_token
    event["segment"]["id"] = next_realtime_segment_id(meeting)
    store.add_realtime_segment(meeting_id, event["segment"])
    return event


def _normalize_final_segment_with_policy(segment: dict[str, Any], policy: EffectiveVocabulary) -> dict[str, Any]:
    """Copy one final ASR segment with raw provider text and replacement audit metadata.

    ``rawText`` is intentionally set before any forced replacement and retained even when no rule
    matches.  Partial websocket events never reach this helper, so they cannot acquire a revision,
    normalization audit, or durable transformed text.
    """

    final_segment = dict(segment)
    provider_text = str(final_segment.get("rawText") or final_segment.get("text") or "")
    # Provider source is the only legal input to Task 3 normalization.  Earlier Task 4 code used
    # gateway ``text`` when ``rawText`` existed, allowing an old flat display mask to become a
    # permanent transcript mutation.  This keeps ``rawText`` and normalized ``text`` reversible.
    normalized = apply_final_replacements(provider_text, policy.replacement_rules, policy.rule_ids)
    final_segment["rawText"] = provider_text
    final_segment["text"] = normalized.text
    final_segment["normalizationEdits"] = list(normalized.normalization_edits)
    final_segment["recognitionPolicyHash"] = policy.snapshot_hash
    return final_segment


@app.websocket("/api/meetings/{meeting_id}/realtime")
async def realtime_meeting(websocket: WebSocket, meeting_id: str) -> None:
    """实时会议 WebSocket。

    前端可发送二进制音频块；后端返回 JSON 转写片段。
    真实部署时此处会接入 VAD/流式 ASR 服务，当前 mock 确保页面可联调。
    """
    await websocket.accept()
    # owner_id 标识物理 WebSocket，sessionToken 标识该连接中的一次识别会话。两者一起比较，
    # 可以阻止另一个页面复用相同 token，也能让 finally 做安全的 compare-and-release。
    connection_owner_id = f"ws-{uuid.uuid4().hex}"
    meeting = store.get_or_create_meeting(meeting_id)
    try:
        _require_transcription_mode(meeting, "realtime")
    except HTTPException as exc:
        # A WebSocket has no HTTP response body after upgrade, so send the same machine-readable
        # mode error before closing with the policy-violation code.  There is deliberately no
        # fallback to the import writer because that would violate record ownership.
        await websocket.send_text(json.dumps({"type": "error", "meetingId": meeting_id, **dict(exc.detail)}, ensure_ascii=False))
        await websocket.close(code=1008)
        return
    chunk_index = 0
    realtime_mime_type = "audio/wav"
    realtime_chunk_duration_ms = 3000
    realtime_endpoint_config: dict[str, Any] = {}
    pending_chunk_meta: dict[str, Any] | None = None
    active_session_token = ""
    stream_session = None
    streaming_unavailable = False
    send_lock = asyncio.Lock()
    client_connected = True
    stream_sample_rate = 16000
    pcm_timeline = Pcm16TimelineBuffer(sample_rate=stream_sample_rate)
    session_recorder: Pcm16WaveRecorder | None = None
    # 实时最终句比声纹注册样本短、又比亚秒级 VAD 片段长，使用经真实会议录音校准的独立
    # 阈值，避免低阈值通过质心链式效应把多个人不断吸收到 speaker-1。
    speaker_tracker = RealtimeSpeakerTracker(
        cluster_threshold=REALTIME_FINAL_SEGMENT_CLUSTER_COSINE_THRESHOLD,
    )
    # 最终文本先发送，声纹分析再由单消费者队列顺序执行。顺序队列既避免阻塞 ASR 接收循环，
    # 也保证 speaker-1/2 的首次出现顺序与会议时间线一致，不会因并发模型响应快慢而反转编号。
    speaker_jobs: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=32)

    async def send_realtime_event(event: dict[str, Any]) -> None:
        """Serialize outgoing WebSocket messages from route and provider receiver task.

        The DashScope receiver runs in a background task while the browser receive loop is still active. A small
        send lock prevents partial/final events and route-level status messages from writing to the Starlette
        WebSocket at the same instant.
        """

        nonlocal client_connected
        payload = json.dumps(event, ensure_ascii=False)
        async with send_lock:
            # 连接状态可能在等待锁期间变化，所以必须在锁内复查。发送失败只关闭浏览器
            # 输出通道，不能向上抛到 DashScope receiver；provider 仍需继续 flush 并落库末句。
            if not client_connected:
                return
            try:
                await websocket.send_text(payload)
            except Exception:
                client_connected = False

    async def run_speaker_enrichment() -> None:
        """串行消费已落库片段，后台补齐匿名聚类或声纹库姓名。"""

        while True:
            job = await speaker_jobs.get()
            try:
                if job is None:
                    return
                job_token = str(job.get("sessionToken") or "")
                segment_id = str(job.get("segmentId") or "")
                if not job_token or job_token != active_session_token:
                    continue
                if not _realtime_leases.is_owner(
                    meeting_id,
                    owner_id=connection_owner_id,
                    session_token=job_token,
                ):
                    # 新连接接管后，旧连接队列中的声纹任务也必须失效；否则虽然正文没新增，
                    # 迟到的 speaker_update 仍可能改写新会话的显示身份。
                    continue
                # 队列任务可能在用户暂停后才开始执行。只有目标 segment 仍属于该 token 时才
                # 允许写回；这比“更新最后一段”稳定，也不会污染同一会议的新 WebSocket 会话。
                latest_meeting = store.get_or_create_meeting(meeting_id)
                target = next(
                    (
                        segment
                        for segment in latest_meeting.get("segments", [])
                        if str(segment.get("id") or "") == segment_id
                        and str(segment.get("realtimeSessionToken") or "") == job_token
                    ),
                    None,
                )
                if target is None:
                    continue
                embedding, voiceprint_match = await asyncio.to_thread(
                    _analyze_realtime_speaker_wav,
                    job.get("wavBytes") or b"",
                )
                job_tracker = job.get("tracker")
                # 模型 HTTP 运行期间用户可能暂停后重开，active token 与 tracker 都会更换。
                # 返回后必须再次校验两者，旧任务不能占用新会话的匿名编号或写回任何片段。
                if job_token != active_session_token or job_tracker is not speaker_tracker:
                    continue
                identity = job_tracker.identify(embedding, voiceprint_match)
                # 文本必须先显示，所以片段初始使用互不相同的 pending ID。CAM++ 返回有效
                # embedding 后，再由会议级 tracker 把相近短句收敛到同一 canonical cluster；
                # 这样实时阶段即可区分多人，同时连续同人的片段会获得同一稳定身份键并在
                # 展示层合成一个正文框。模型不可用且没有声纹命中时仍保留 pending，不能把
                # 所有未知片段武断归入同一个 fallback 发言人。
                if embedding is None and identity.speaker_source != "voiceprint":
                    continue
                speaker_fields = _speaker_identity_event_fields(identity)
                affected = store.update_realtime_speaker_identity(
                    meeting_id,
                    segment_id=segment_id,
                    cluster_id=identity.speaker_cluster_id,
                    session_token=job_token,
                    patch=speaker_fields,
                )
                if not affected:
                    continue
                await send_realtime_event(
                    {
                        "type": "speaker_update",
                        "meetingId": meeting_id,
                        "sessionToken": job_token,
                        "segmentId": segment_id,
                        "affectedSegmentIds": [str(item.get("id") or "") for item in affected],
                        **speaker_fields,
                    }
                )
            except Exception:
                # 说话人分析是增强链路。任何模型、存储或已关闭 WebSocket 异常都不能反向
                # 终止实时 ASR；最终文本已经在入队前发送并持久化。
                continue
            finally:
                speaker_jobs.task_done()

    speaker_worker = asyncio.create_task(run_speaker_enrichment())

    async def handle_stream_event(
        event: dict[str, Any],
        *,
        event_session_token: str | None = None,
        event_timeline: Pcm16TimelineBuffer | None = None,
        event_tracker: RealtimeSpeakerTracker | None = None,
    ) -> None:
        """Forward provider-native streaming events to the browser and persist final transcript."""

        session_token = str(event_session_token or active_session_token or "")
        if not session_token or session_token != active_session_token:
            return
        if not _realtime_leases.is_owner(
            meeting_id,
            owner_id=connection_owner_id,
            session_token=session_token,
        ):
            # Provider receiver 是后台任务，旧连接被接管后仍可能收到 partial/final。partial 不应
            # 再推给浏览器，final 更不能进入统一落库 helper，因此在回调入口先快速拒绝。
            return
        event.setdefault("meetingId", meeting_id)
        event["sessionToken"] = session_token
        if event.get("type") == "transcript":
            segment = event.get("segment") or {}
            if _is_generic_speaker_name(segment.get("speakerName")):
                # 供应商/旧测试桩可能仍返回“实时发言人”。在文本首次送达前就收敛成稳定匿名
                # 占位，随后异步 speaker_update 再按 embedding 分成发言人1/2或库中姓名。
                segment["speakerName"] = "发言人1"
                # 在 embedding 完成前不能让所有新片段共享真正的 speaker-1 cluster，否则第一条
                # 声纹回填会把尚未分析的第二条一起改名。pending ID 只用于持久化隔离，UI 仍显示
                # “发言人1”；后台 tracker 返回 canonical cluster 后会替换它。
                segment["speakerClusterId"] = f"pending-{segment.get('id') or uuid.uuid4().hex[:10]}"
            event = _finalize_realtime_transcript_event(
                meeting_id,
                event,
                session_token,
                lease_owner_id=connection_owner_id,
            )
        # 文本交付必须位于任何声纹 HTTP 调用之前。用户先看到 final 逐字稿，后台任务稍后只
        # 发送 speaker_update；这消除了“为了等 CAM++，识别结果迟迟不出现”的额外延迟。
        await send_realtime_event(event)
        if event.get("type") != "transcript" or not event.get("segment", {}).get("id"):
            return
        if not store.get_or_create_meeting(meeting_id).get("enableDiarization", True):
            return
        segment = event["segment"]
        timeline = event_timeline or pcm_timeline
        tracker = event_tracker or speaker_tracker
        wav_bytes = timeline.extract_wav(
            int(segment.get("startMs") or 0),
            int(segment.get("endMs") or segment.get("startMs") or 0),
        )
        if not wav_bytes:
            return
        try:
            speaker_jobs.put_nowait(
                {
                    "segmentId": segment["id"],
                    "sessionToken": session_token,
                    "wavBytes": wav_bytes,
                    "tracker": tracker,
                }
            )
        except asyncio.QueueFull:
            # 极端模型拥塞时宁可暂时保留匿名名，也不能让队列无限增长拖垮实时文本链路。
            pass

    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message and message["bytes"] is not None:
                if not active_session_token:
                    # 兼容旧浏览器和既有集成客户端：它们可能直接发送第一块 WAV，而没有先发
                    # realtime_config/realtime_chunk。第一块音频到达时为本连接惰性创建 token 与
                    # lease，之后仍执行同一套所有权校验，不能因为兼容路径绕开单活保护。
                    active_session_token = f"rt-{uuid.uuid4().hex}"
                    _realtime_leases.claim(
                        meeting_id,
                        owner_id=connection_owner_id,
                        session_token=active_session_token,
                    )
                if active_session_token and not _realtime_leases.is_owner(
                    meeting_id,
                    owner_id=connection_owner_id,
                    session_token=active_session_token,
                ):
                    # 后来的同 meeting 连接已接管。继续向上游送音频只会浪费配额，并可能在旧
                    # receiver 中形成迟到结果；旧 socket 保持可关闭，但不再消费音频。
                    continue
                if streaming_unavailable:
                    continue
                if stream_session:
                    # Provider-native streaming path: browser frames are already PCM16, so do not run the legacy
                    # WAV quality gate or synchronous chunk ASR. The upstream provider handles VAD/endpointing and
                    # returns partial/final events through handle_stream_event.
                    pcm_timeline.append(message["bytes"])
                    if session_recorder:
                        session_recorder.append(message["bytes"])
                    await stream_session.send_audio(message["bytes"])
                    continue
                try:
                    audio_quality = analyze_realtime_chunk_quality(message["bytes"])
                    if not audio_quality.has_voice:
                        # 低音量不是连接错误，也不代表用户暂停了会议；它只是“这一段暂时没有可识别语音”。
                        # 因此后端只回传结构化 status，连接继续保持，前端用内联状态提醒用户检查麦克风。
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "status",
                                    "code": "low_volume",
                                    "meetingId": meeting_id,
                                    "sessionToken": active_session_token,
                                    "message": "当前音频分片音量过低，已跳过实时转写",
                                    "rms": audio_quality.rms,
                                    "peak": audio_quality.peak,
                                    "activeRatio": audio_quality.active_ratio,
                                    "durationMs": audio_quality.duration_ms,
                                    "reason": audio_quality.reason,
                                },
                                ensure_ascii=False,
                            )
                        )
                        pending_chunk_meta = None
                        continue
                    effective_duration_ms = realtime_chunk_duration_ms
                    effective_speech_ms = audio_quality.duration_ms
                    if pending_chunk_meta:
                        # 前端会在 WAV 前发送真实时间轴和累计人声时长。这里先用这些元数据做
                        # “最终正文门槛”判断：短停顿可以继续采集，但不能直接送 ASR 变成会议正文。
                        effective_duration_ms = max(
                            100,
                            int(pending_chunk_meta.get("endMs", 0)) - int(pending_chunk_meta.get("startMs", 0)),
                        )
                        effective_speech_ms = max(0, int(pending_chunk_meta.get("speechMs", effective_speech_ms) or 0))
                    if pending_chunk_meta and (
                        effective_speech_ms < REALTIME_MIN_FINAL_SPEECH_MS
                        or effective_duration_ms < REALTIME_MIN_FINAL_SEGMENT_MS
                    ):
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "status",
                                    "code": "collecting_context",
                                    "meetingId": meeting_id,
                                    "sessionToken": active_session_token,
                                    "message": "正在积累更完整的语音上下文，暂不写入正文",
                                    "durationMs": effective_duration_ms,
                                    "speechMs": effective_speech_ms,
                                    "minDurationMs": REALTIME_MIN_FINAL_SEGMENT_MS,
                                    "minSpeechMs": REALTIME_MIN_FINAL_SPEECH_MS,
                                },
                                ensure_ascii=False,
                            )
                        )
                        pending_chunk_meta = None
                        continue
                    # 前端发送的是短 WAV 分片；DashScope 网关会按分片真实识别。
                    # 只有识别出 transcript 时才写入会议，错误/静音状态只回传给前端，不再制造假文本。
                    # 实时分片既要送 ASR，也要送声纹匹配。先把前端传来的短 WAV 保存成临时文件，
                    # 后续 CAM++/3D-Speaker 客户端才能按统一 audio_path 协议识别“这是谁的声音”。
                    chunk_path = AUDIO_CLIP_DIR / f"{meeting_id}-realtime-{chunk_index}.wav"
                    chunk_path.write_bytes(message["bytes"])
                    meeting_context = store.get_or_create_meeting(meeting_id)
                    asr_inputs = get_meeting_asr_inputs(meeting_context)
                    # The browser tail improves continuity, while participant names and policy
                    # words come from the same policy used by import hotwords. The realtime context
                    # builder intentionally excludes meeting titles/imported filenames so provider
                    # bias cannot turn record metadata into transcript text. Both inputs stay
                    # bounded so a broken browser cannot create an unbounded corpus.
                    meeting_context, recognition_policy = _recognition_policy_for_processing(meeting_context)
                    raw_browser_context = str((pending_chunk_meta or {}).get("contextText") or "")
                    # Browser continuity arrives as newline-separated prior segments. Pass the full
                    # untruncated block into the shared helper so each complete line can still match
                    # meetingName/title/fileName. Truncating first would turn a long identity into an
                    # unmatched suffix; substring deletion would instead damage legitimate text.
                    context_items = filter_realtime_context_items(
                        meeting_context,
                        [
                            build_realtime_context(meeting_context, recognition_policy, maximum_characters=700),
                            raw_browser_context,
                        ],
                    )
                    # Bound only the final, cleaned corpus. This keeps the provider request compact
                    # without changing identity items before comparison or discarding normal lines
                    # that follow a filtered title line in the browser transcript block.
                    context_text = "\n".join(context_items)[:1200]
                    event = _transcribe_realtime_chunk_with_context(
                        meeting_id,
                        chunk_index,
                        message["bytes"],
                        asr_inputs["sensitiveWords"],
                        mime_type=realtime_mime_type,
                        duration_ms=effective_duration_ms,
                        context_text=context_text,
                    )
                except Exception as exc:  # noqa: BLE001 - WebSocket 长连接里要把模型错误回传给前端展示。
                    event = {"type": "error", "meetingId": meeting_id, "message": f"实时 ASR 调用失败：{exc}"}
                event.setdefault("meetingId", meeting_id)
                event["sessionToken"] = active_session_token
                if event.get("type") == "transcript" and event.get("segment", {}).get("text"):
                    if pending_chunk_meta:
                        # The ASR gateway still returns a compatible segment for older clients. When metadata is
                        # present, replace estimated chunk_index timestamps with real frontend audio clock values.
                        event["segment"]["startMs"] = int(pending_chunk_meta.get("startMs", event["segment"].get("startMs", 0)))
                        event["segment"]["endMs"] = int(pending_chunk_meta.get("endMs", event["segment"].get("endMs", 0)))
                        event["segment"]["flushReason"] = pending_chunk_meta.get("reason", "")
                        event["segment"]["overlapMs"] = int(pending_chunk_meta.get("overlapMs", 0) or 0)
                    event["segment"]["realtimeSessionToken"] = active_session_token
                    meeting = store.get_or_create_meeting(meeting_id)
                    diarization_result = None
                    if meeting.get("enableDiarization", True):
                        if VOICEPRINT_GATEWAY_BASE_URL:
                            try:
                                # 实时会议同样先让 3D-Speaker 判断当前音频块里是否有多个 speaker，
                                # 再把 speaker key 映射成声纹库姓名或“发言人1/2”。失败时保留 ASR 文本。
                                diarization_result = LocalVoiceprintClient(VOICEPRINT_GATEWAY_BASE_URL).diarize(
                                    audio_path=str(chunk_path),
                                    min_speakers=None,
                                    max_speakers=None,
                                )
                            except Exception:
                                diarization_result = None
                        event["segment"] = apply_voiceprint_match_to_segments([event["segment"]], str(chunk_path), diarization_result)[0]
                    # Timestamp and voiceprint enrichment intentionally finish before the common
                    # finalizer so those fields survive on the durable segment. The finalizer is the
                    # sole realtime persistence boundary: it performs title-echo rejection first,
                    # then exactly one policy normalization, ID allocation, and revisioned write.
                    event = _finalize_realtime_transcript_event(
                        meeting_id,
                        event,
                        active_session_token,
                        lease_owner_id=connection_owner_id,
                    )
                    chunk_index += 1
                pending_chunk_meta = None
                await send_realtime_event(event)
            elif "text" in message and message["text"] == "stop":
                if stream_session:
                    await stream_session.finish()
                    stream_session = None
                try:
                    # finish 可能刚产生最后一个 final。给后台身份更新一个短收尾窗口，但
                    # 超时后仍立即结束会议，绝不让声纹服务故障卡住用户的停止操作。
                    await asyncio.wait_for(speaker_jobs.join(), timeout=2.5)
                except asyncio.TimeoutError:
                    pass
                recording_path = session_recorder.finalize() if session_recorder else None
                if recording_path and active_session_token:
                    store.attach_realtime_recording(
                        meeting_id,
                        recording_path.name,
                        recording_path,
                        duration_ms=session_recorder.duration_ms,
                        session_token=active_session_token,
                    )
                    # 少于 5 秒的录音没有足够语音支持稳定聚类，也常见于预检和连接测试；
                    # 这类会话只保留录音，不启动昂贵的整场模型，避免用噪声制造虚假发言人。
                    should_run_whole_session_diarization = session_recorder.duration_ms >= 5_000
                    if (
                        should_run_whole_session_diarization
                        and store.get_or_create_meeting(meeting_id).get("enableDiarization", True)
                        and VOICEPRINT_GATEWAY_BASE_URL
                    ):
                        await send_realtime_event(
                            {
                                "type": "status",
                                "code": "speaker_diarization_running",
                                "meetingId": meeting_id,
                                "sessionToken": active_session_token,
                                "message": "正在结合整场录音整理发言人，请稍候…",
                            }
                        )
                        try:
                            # 短句 CAM++ 已经发现两人以上时，把人数作为 3D-Speaker 的 oracle
                            # 先验。真实短会中自动估计容易欠分离成一人；人数先验只影响会后
                            # 时间段归属，不会把 embedding、原文或底层 segment 合并掉。
                            online_speaker_count = speaker_tracker.evidence_speaker_count
                            diarization_speaker_hint = (
                                min(8, online_speaker_count)
                                if online_speaker_count >= 2
                                else None
                            )
                            diarization_result = await asyncio.to_thread(
                                LocalVoiceprintClient(VOICEPRINT_GATEWAY_BASE_URL).diarize,
                                audio_path=str(recording_path),
                                min_speakers=diarization_speaker_hint,
                                max_speakers=diarization_speaker_hint,
                            )
                            current_segments = [
                                segment
                                for segment in store.get_or_create_meeting(meeting_id).get("segments", [])
                                if str(segment.get("realtimeSessionToken") or "") == active_session_token
                            ]
                            diarization_speaker_keys = {
                                _diarization_speaker_key(item)
                                for item in (diarization_result or {}).get("segments", [])
                                if _diarization_speaker_key(item)
                            }
                            if online_speaker_count >= 2 and len(diarization_speaker_keys) < 2:
                                # 整段模型即使收到多人先验仍只返回一个 speaker，说明本次结果
                                # 明显欠分离。此时保留已经逐句取得 embedding 证据的在线身份，
                                # 绝不能再用一个整场标签覆盖成“全是发言人1”。
                                patched_segments = [dict(segment) for segment in current_segments]
                            else:
                                patched_segments = apply_voiceprint_match_to_segments(
                                    current_segments,
                                    str(recording_path),
                                    diarization_result,
                                )
                            speaker_patch_fields = {
                                "speakerName",
                                "speakerTitle",
                                "speakerClusterId",
                                "speakerSource",
                                "voiceprintId",
                                "voiceprintConfidence",
                            }
                            affected = store.apply_realtime_diarization(
                                meeting_id,
                                session_token=active_session_token,
                                speaker_patches={
                                    str(segment.get("id") or ""): {
                                        key: value
                                        for key, value in segment.items()
                                        if key in speaker_patch_fields
                                    }
                                    for segment in patched_segments
                                },
                            )
                            for segment in affected:
                                await send_realtime_event(
                                    {
                                        "type": "speaker_update",
                                        "meetingId": meeting_id,
                                        "sessionToken": active_session_token,
                                        "segmentId": segment.get("id"),
                                        "affectedSegmentIds": [segment.get("id")],
                                        **{
                                            key: segment.get(key)
                                            for key in speaker_patch_fields
                                            if key in segment
                                        },
                                    }
                                )
                        except Exception as exc:
                            # 发言人整理失败不回滚已完成的 ASR 和录音；明确告知用户可在录音保留后重试。
                            await send_realtime_event(
                                {
                                    "type": "status",
                                    "code": "speaker_diarization_failed",
                                    "meetingId": meeting_id,
                                    "sessionToken": active_session_token,
                                    "message": f"发言人整理失败，录音已保留，可稍后重新识别：{exc}",
                                }
                            )
                await send_realtime_event({"type": "closed", "meetingId": meeting_id, "sessionToken": active_session_token})
                break
            elif "text" in message and message["text"]:
                try:
                    config_message = json.loads(message["text"])
                except json.JSONDecodeError:
                    config_message = {}
                if config_message.get("type") == "realtime_config":
                    realtime_mime_type = config_message.get("mimeType") or realtime_mime_type
                    requested_session_token = str(
                        config_message.get("sessionToken")
                        or active_session_token
                        or f"rt-{uuid.uuid4().hex}"
                    )
                    if config_message.get("streamingMode") == "dashscope_realtime":
                        streaming_unavailable = False
                        try:
                            if stream_session:
                                # 重复配置必须先在旧 lease 仍有效时 flush 旧上游。若提前 claim 新
                                # token，旧 provider 的最后一个 final 会被单活守卫当成过期结果丢弃。
                                # finish 完成后再交接租约，旧句仍归旧 token，新句才归新 token。
                                await stream_session.finish()
                                stream_session = None
                            _realtime_leases.claim(
                                meeting_id,
                                owner_id=connection_owner_id,
                                session_token=requested_session_token,
                            )
                            active_session_token = requested_session_token
                            meeting_context = store.get_or_create_meeting(meeting_id)
                            meeting_context, recognition_policy = _recognition_policy_for_processing(meeting_context)
                            asr_inputs = get_meeting_asr_inputs(meeting_context)
                            known_speakers = [
                                str(segment.get("speakerName") or "").strip()
                                for segment in (meeting_context.get("segments") or [])
                                if str(segment.get("speakerName") or "").strip()
                            ]
                            # Final composition applies one complete-item identity filter across the
                            # builder output and known-speaker source. This prevents a title/fileName
                            # accidentally stored as a speaker label from bypassing policy cleanup,
                            # while preserving ordinary names and sentences that only mention it.
                            context_items = filter_realtime_context_items(
                                meeting_context,
                                [
                                    build_realtime_context(
                                        meeting_context,
                                        recognition_policy,
                                        maximum_characters=1000,
                                    ),
                                    *known_speakers,
                                ],
                            )
                            realtime_context_text = "；".join(context_items)[:1200]
                            stream_sample_rate = max(8000, int(config_message.get("sampleRate") or 16000))
                            pcm_timeline = Pcm16TimelineBuffer(sample_rate=stream_sample_rate)
                            session_recorder = Pcm16WaveRecorder(
                                AUDIO_CLIP_DIR / f"{meeting_id}-{active_session_token}-recording.wav",
                                sample_rate=stream_sample_rate,
                            )
                            # 每次新的 provider-native 会话都重新编号匿名说话人；历史片段依靠
                            # realtimeSessionToken 保持隔离，不会与新会话的 speaker-1 合并。
                            # 暂停后重新开始会生成新的 session token 和 tracker；阈值必须与首次
                            # 会话一致，否则同一场会议前后两段会表现出不同的发言人区分能力。
                            speaker_tracker = RealtimeSpeakerTracker(
                                cluster_threshold=REALTIME_FINAL_SEGMENT_CLUSTER_COSINE_THRESHOLD,
                            )
                            session_token_snapshot = active_session_token
                            timeline_snapshot = pcm_timeline
                            tracker_snapshot = speaker_tracker

                            async def handle_current_provider_event(
                                provider_event: dict[str, Any],
                                session_token_snapshot: str = session_token_snapshot,
                                timeline_snapshot: Pcm16TimelineBuffer = timeline_snapshot,
                                tracker_snapshot: RealtimeSpeakerTracker = tracker_snapshot,
                            ) -> None:
                                # 回调捕获不可变的会话对象，而不是读取稍后会变化的 route 变量。
                                # 即使旧 provider 在关闭边界迟到，也只能以自己的 token/timeline/tracker
                                # 进入统一处理器，并会被 active token 守卫拒绝或正确归属。
                                await handle_stream_event(
                                    provider_event,
                                    event_session_token=session_token_snapshot,
                                    event_timeline=timeline_snapshot,
                                    event_tracker=tracker_snapshot,
                                )

                            stream_session = create_realtime_stream_session(
                                meeting_id=meeting_id,
                                sample_rate=stream_sample_rate,
                                # The stream setup mirrors offline ASR: both language and masking
                                # rules are frozen at meeting creation, not re-read from mutable
                                # request/global configuration when a streaming session reconnects.
                                language=asr_inputs["language"] or str(config_message.get("language") or "zh"),
                                sensitive_words=asr_inputs["sensitiveWords"],
                                on_event=handle_current_provider_event,
                                context_text=realtime_context_text,
                                # 前端平衡模式传 silenceEndMs；未传时使用会议场景默认 1200ms。
                                # provider 构造器还会做 200-6000ms 最终钳制，避免非法配置。
                                silence_duration_ms=config_message.get("silenceEndMs", 1200),
                            )
                            await stream_session.start()
                            await send_realtime_event(
                                {
                                    "type": "status",
                                    "code": "streaming_started",
                                    "meetingId": meeting_id,
                                    "sessionToken": active_session_token,
                                    "message": "实时流式转写已启动",
                                }
                            )
                            continue
                        except Exception as exc:
                            stream_session = None
                            streaming_unavailable = True
                            await send_realtime_event(
                                {
                                    "type": "status",
                                    "code": "streaming_unavailable",
                                    "meetingId": meeting_id,
                                    "sessionToken": active_session_token,
                                    "message": f"实时流式转写不可用，请检查 DashScope realtime 配置：{exc}",
                                }
                            )
                            continue
                    # 同步 WAV 配置没有 provider flush 边界，可以在接收配置时直接成为本 meeting
                    # 的最新写入者。compare-and-release 仍保证旧连接 finally 不会删除本租约。
                    _realtime_leases.claim(
                        meeting_id,
                        owner_id=connection_owner_id,
                        session_token=requested_session_token,
                    )
                    active_session_token = requested_session_token
                    realtime_endpoint_config = {
                        "endpointingMode": config_message.get("endpointingMode", "legacy"),
                        "silenceEndMs": config_message.get("silenceEndMs"),
                        "sentenceEndSilenceMs": config_message.get("sentenceEndSilenceMs"),
                        "maxSegmentMs": config_message.get("maxSegmentMs"),
                        "overlapMs": config_message.get("overlapMs"),
                    }
                    realtime_chunk_duration_ms = max(
                        1000,
                        int(config_message.get("chunkDurationMs") or realtime_endpoint_config.get("maxSegmentMs") or realtime_chunk_duration_ms),
                    )
                    await send_realtime_event(
                        {
                            "type": "status",
                            "meetingId": meeting_id,
                            "sessionToken": active_session_token,
                            "message": "实时转写配置已接收",
                        }
                    )
                elif config_message.get("type") == "realtime_chunk":
                    requested_session_token = str(
                        config_message.get("sessionToken")
                        or active_session_token
                        or f"rt-{uuid.uuid4().hex}"
                    )
                    if active_session_token and not _realtime_leases.is_owner(
                        meeting_id,
                        owner_id=connection_owner_id,
                        session_token=active_session_token,
                    ):
                        # 本连接已被另一个同 meeting WebSocket 接管时，迟到的 chunk metadata 不能
                        # 重新 claim 抢回所有权。后续 bytes 也会被上方守卫忽略。
                        continue
                    if requested_session_token != active_session_token:
                        _realtime_leases.claim(
                            meeting_id,
                            owner_id=connection_owner_id,
                            session_token=requested_session_token,
                        )
                        active_session_token = requested_session_token
                    start_ms = max(0, int(config_message.get("startMs", 0) or 0))
                    end_ms = max(start_ms, int(config_message.get("endMs", start_ms) or start_ms))
                    pending_chunk_meta = {
                        "startMs": start_ms,
                        "endMs": end_ms,
                        "reason": config_message.get("reason", ""),
                        "overlapMs": max(0, int(config_message.get("overlapMs", 0) or 0)),
                        "speechMs": max(0, int(config_message.get("speechMs", 0) or 0)),
                        # Preserve complete newline-delimited segment items until meeting identity
                        # filtering runs beside the ASR call. The cleaned final corpus receives its
                        # hard 1200-character cap there; an early tail slice could leak a truncated
                        # title/fileName fragment that no longer equals the persisted identity.
                        "contextText": str(config_message.get("contextText") or ""),
                        "sessionToken": active_session_token,
                    }
    except WebSocketDisconnect:
        client_connected = False
        if stream_session:
            # 浏览器断开不等于供应商没有缓冲文本。仍发送 session.finish，让 receiver 把末句
            # 落库；send_realtime_event 会因 client_connected=False 跳过已失效的浏览器连接。
            await stream_session.finish()
    finally:
        # 浏览器异常断开时也必须关闭并挂载当前单个 WAV；正常 stop 已经完成相同操作，
        # ``finalize`` 与 ``attach_realtime_recording`` 都按 session token 幂等，不会重复记录。
        if session_recorder:
            disconnected_recording_path = session_recorder.finalize()
            if disconnected_recording_path and active_session_token:
                try:
                    store.attach_realtime_recording(
                        meeting_id,
                        disconnected_recording_path.name,
                        disconnected_recording_path,
                        duration_ms=session_recorder.duration_ms,
                        session_token=active_session_token,
                    )
                except KeyError:
                    # 用户在断线收尾同时删除会议时，以会议删除为准；不能重新创建孤立记录。
                    pass
        if active_session_token:
            # 只有仍持有相同 owner+token 的连接才能释放。若本连接早已被新页面接管，此调用
            # 返回 False 并保留新租约，解决旧 WebSocket finally 迟到的经典竞态。
            _realtime_leases.release(
                meeting_id,
                owner_id=connection_owner_id,
                session_token=active_session_token,
            )
        # 精确停止单个后台 worker，不触碰其它会议任务。队列中的身份增强可被丢弃，但已经
        # 持久化并发送的 ASR 文本始终保留。
        if not speaker_worker.done():
            try:
                speaker_jobs.put_nowait(None)
            except asyncio.QueueFull:
                speaker_worker.cancel()
            try:
                await asyncio.wait_for(speaker_worker, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                speaker_worker.cancel()


@app.post("/api/transcripts/{transcript_id}/align")
def align_transcript(transcript_id: str, req: AlignRequest) -> dict[str, int]:
    """根据选中文本返回音频时间范围。"""

    def to_product_window(window: dict[str, Any]) -> dict[str, int]:
        """把模型服务或本地算法的时间窗统一成主产品 camelCase 契约。"""

        # 模型服务沿用 Python/HTTP 服务侧的 snake_case，而主产品前端历史契约使用 camelCase。
        # 两条执行路径都必须经过同一个转换边界，否则服务异常后切到本地降级时会悄悄改变响应结构。
        # HTTP 200 只说明传输成功，不代表 ForcedAligner 返回了可用时间窗；缺任一端点或字段不可转成
        # 整数时都必须视为模型服务失败。抛出 LocalModelServiceError 后，真实服务路径会被下方现有
        # except 捕获并改用 req.words 降级，避免向前端静默制造 0 毫秒或泄漏 ValueError/TypeError。
        start_key = "startMs" if "startMs" in window else "start_ms"
        end_key = "endMs" if "endMs" in window else "end_ms"
        if start_key not in window or end_key not in window:
            raise LocalModelServiceError("强制对齐服务响应缺少 start/end 时间字段")
        try:
            # 不能使用 `or` 选键或选值，因为 0 是合法的音频起点；先确认键存在，再做严格类型转换。
            start_ms = int(window[start_key])
            end_ms = int(window[end_key])
        except (TypeError, ValueError) as exc:
            raise LocalModelServiceError("强制对齐服务响应包含无效的 start/end 时间字段") from exc
        return {"startMs": start_ms, "endMs": end_ms}

    if transcript_id not in store.transcripts:
        raise HTTPException(status_code=404, detail="转写记录不存在")
    if ALIGNMENT_GATEWAY_BASE_URL:
        try:
            # 生产部署时这里会调用算力服务器上的 Qwen3-ForcedAligner-0.6B。
            # 该接口根据全文、选中文本和音频路径直接返回精确时间窗，用于字音回听和选区注册声纹。
            transcript = store.transcripts[transcript_id]
            file_record = store.files.get(transcript.get("fileId", ""), {})
            aligned = LocalAlignmentClient(ALIGNMENT_GATEWAY_BASE_URL).selection_window(
                audio_path=file_record.get("path", ""),
                transcript_text=req.transcriptText,
                selected_text=req.selectedText,
                padding_ms=req.paddingMs,
            )
            return to_product_window(aligned)
        except LocalModelServiceError:
            # 模型服务未启动或返回异常时保留本地时间戳算法兜底，保证页面选区流程可继续演示。
            pass
    return to_product_window(
        find_audio_window_for_selection(
            req.transcriptText,
            req.selectedText,
            req.words,
            padding_ms=req.paddingMs,
        )
    )


@app.post("/api/voiceprints/register-from-selection")
def register_voiceprint(req: VoiceprintRegisterRequest) -> dict[str, Any]:
    """通过页面选中文本完成无感知声纹注册。"""
    file_record = store.files.get(req.sourceFileId, {})
    audio_window = find_audio_window_for_selection(
        req.transcriptText,
        req.selectedText,
        req.words,
        padding_ms=req.paddingMs,
    )
    if ALIGNMENT_GATEWAY_BASE_URL and file_record.get("path"):
        try:
            # 选中文本注册声纹时，优先由强制对齐服务反查 15 秒左右清晰音频片段。
            # 这样页面只需要知道用户选中了哪些文字，不需要自己维护复杂的字级时间戳算法。
            aligned = LocalAlignmentClient(ALIGNMENT_GATEWAY_BASE_URL).selection_window(
                audio_path=file_record.get("path", ""),
                transcript_text=req.transcriptText,
                selected_text=req.selectedText,
                padding_ms=req.paddingMs,
            )
            audio_window = {"startMs": int(aligned.get("start_ms", 0)), "endMs": int(aligned.get("end_ms", 0))}
        except LocalModelServiceError:
            # 对齐服务不可用时，继续用前端已有 words 时间戳兜底，避免注册流程中断。
            pass
    registration = build_voiceprint_registration(
        speaker_name=req.speakerName,
        meeting_id=req.meetingId,
        source_file_id=req.sourceFileId,
        selected_text=req.selectedText,
        audio_window=audio_window,
    )
    return store.save_voiceprint(registration)


@app.get("/api/voiceprints")
def list_voiceprints() -> dict[str, Any]:
    """获取声纹库。"""
    return {"items": list(store.voiceprints.values())}


@app.post("/api/voiceprints")
def create_voiceprint(req: VoiceprintRequest) -> dict[str, Any]:
    runtime = _voiceprint_runtime_status()
    """新增声纹库人员资料。"""
    record = {
        # 声纹人员 ID 必须全局唯一，不能按 len()+1 生成。
        # 因为 SQLite/Kingbase 中记录可能被删除、批量导入或从普通会议系统同步，len()+1
        # 很容易撞到已有 `vp-001` 这类内置人员，导致声纹样本和注册状态覆盖错误对象。
        "id": f"vp-{uuid.uuid4().hex[:8]}",
        "name": req.name,
        "speakerName": req.name,
        "department": req.department,
        # Metadata sample count does not prove enrollment. Only a real model response with an
        # embedding ID may later transition this profile to ``registered``.
        "samples": max(0, req.samples),
        "enabled": req.enabled,
        "lastMatchedAt": "未识别",
        "remark": req.remark,
        "sampleFiles": [],
        "groupId": req.groupId,
        # 新增人员如果还没有上传样本，就明确标记 pending_sample，避免误导为已经注册真实声纹模型。
        "registerStatus": "pending_sample",
        "modelStatus": "waiting_sample" if runtime.get("ready") else "waiting_model_config",
    }
    return store.save_voiceprint(record)


@app.get("/api/voiceprint-groups")
def list_voiceprint_groups() -> dict[str, Any]:
    """获取声纹分组列表，供声纹库管理页左侧栏展示。"""
    return {"items": list(store.voiceprint_groups.values())}


@app.post("/api/voiceprint-groups")
def create_voiceprint_group(req: VoiceprintGroupRequest) -> dict[str, Any]:
    """新增声纹分组。"""
    normalized_name = req.name.strip()
    if not normalized_name:
        raise HTTPException(status_code=400, detail="声纹分组名称不能为空")
    if any(str(item.get("name") or "").strip().casefold() == normalized_name.casefold() for item in store.voiceprint_groups.values()):
        raise HTTPException(status_code=409, detail="声纹分组名称已存在")
    return store.create_config_item(
        "voiceprint_groups",
        "vg",
        {"name": normalized_name, "description": req.description.strip(), "isSystem": False},
    )


@app.patch("/api/voiceprint-groups/{group_id}")
def update_voiceprint_group(group_id: str, req: ConfigPatchRequest) -> dict[str, Any]:
    """更新声纹分组名称或说明。"""
    group = store.voiceprint_groups.get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="声纹分组不存在")
    patch = req.model_dump(exclude_unset=True)
    if "name" in patch:
        normalized_name = str(patch.get("name") or "").strip()
        if not normalized_name:
            raise HTTPException(status_code=400, detail="声纹分组名称不能为空")
        if group.get("isSystem") and normalized_name != group.get("name"):
            raise HTTPException(status_code=400, detail="系统声纹分组不可重命名")
        duplicate = any(
            item_id != group_id and str(item.get("name") or "").strip().casefold() == normalized_name.casefold()
            for item_id, item in store.voiceprint_groups.items()
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="声纹分组名称已存在")
        patch["name"] = normalized_name
    item = store.update_config_item("voiceprint_groups", group_id, patch)
    if not item:
        raise HTTPException(status_code=404, detail="声纹分组不存在")
    if "name" in patch:
        # 声纹记录会冗余 groupName 用于列表展示和导出。分组改名后同步全部成员，避免左侧栏和表格
        # 出现两个不同名称；groupId 始终不变，因此识别配置快照仍可追溯。
        for voiceprint in list(store.voiceprints.values()):
            if voiceprint.get("groupId") == group_id:
                store.update_config_item("voiceprints", voiceprint["id"], {"groupName": patch["name"]})
    return item


@app.delete("/api/voiceprint-groups/{group_id}")
def delete_voiceprint_group(group_id: str) -> dict[str, Any]:
    """删除声纹分组；系统分组不可删除，组内人员会回落到“未分组”。"""
    group = store.voiceprint_groups.get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="声纹分组不存在")
    if group.get("isSystem"):
        raise HTTPException(status_code=400, detail="系统声纹分组不可删除")
    for voiceprint in list(store.voiceprints.values()):
        if voiceprint.get("groupId") == group_id:
            store.update_config_item("voiceprints", voiceprint["id"], {"groupId": "vg-ungrouped", "groupName": "未分组"})
    store.delete_config_item("voiceprint_groups", group_id)
    return {"deleted": True, "id": group_id}


@app.post("/api/voiceprints/batch-delete")
def batch_delete_voiceprints(req: BatchVoiceprintRequest) -> dict[str, Any]:
    """批量删除声纹人员，返回实际删除成功的 ID。"""
    deleted: list[str] = []
    for voiceprint_id in req.ids:
        if store.delete_config_item("voiceprints", voiceprint_id):
            deleted.append(voiceprint_id)
    return {"deleted": deleted, "count": len(deleted)}


@app.post("/api/voiceprints/batch-download")
def batch_download_voiceprints(req: BatchVoiceprintRequest) -> dict[str, Any]:
    """批量下载声纹样本的占位接口。

    当前不生成真实 zip，先返回可下载条目列表；后续接对象存储或打包服务时保持接口不变。
    """
    items = [item for item in store.voiceprints.values() if item["id"] in req.ids]
    return {"items": items, "count": len(items), "status": "ready"}


@app.post("/api/voiceprints/{voiceprint_id}/samples")
async def upload_voiceprint_sample(voiceprint_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """上传声纹样本音频并创建注册任务。

    页面里的“新增/编辑声纹”不应该只填写姓名，因为真实声纹识别需要一段清晰音频生成 embedding。
    当前接口先把样本文件落到 data/audio_clips，再记录一个可查询 job；mock 模式下直接标记完成。
    后续接 CAM++ 或其它声纹服务时，只需要在这里把保存后的音频路径提交给声纹网关，拿到 embedding 后写入
    KingbaseES/向量检索库，前端和声纹库管理页面都不需要改。
    """
    voiceprint = store.voiceprints.get(voiceprint_id)
    if not voiceprint:
        raise HTTPException(status_code=404, detail="声纹不存在")

    # A configured URL is not evidence of readiness. Preserve the sample for diagnosis/retry, but
    # gate every registered state transition on the explicit per-capability health result below.
    runtime = _voiceprint_runtime_status()

    safe_name = Path(file.filename or "voiceprint-sample.wav").name
    sample_path = AUDIO_CLIP_DIR / f"{voiceprint_id}-{safe_name}"
    sample_path.write_bytes(await file.read())

    job = store.create_job(
        meeting_id=f"voiceprint-{voiceprint_id}",
        job_type="voiceprint_register",
        title=f"声纹注册 {voiceprint.get('name', voiceprint_id)}",
        steps=["uploaded", "voiceprint_embedding", "registered"],
    )

    sample_record = {
        "filename": safe_name,
        "path": str(sample_path),
        "contentType": file.content_type or "",
        "registeredAt": format_datetime(),
    }
    sample_files = list(voiceprint.get("sampleFiles", []))
    sample_files.append(sample_record)
    # 老数据可能只有 samples 数字、没有 sampleFiles 明细；上传新样本时以较大的计数为准，
    # 避免“原来 4 段样本，上传 1 段后反而显示 1 段”的回退。
    previous_sample_count = int(voiceprint.get("samples") or 0)
    next_sample_count = max(len(sample_files), previous_sample_count + 1)
    patch = {
        "sampleFiles": sample_files,
        "samples": next_sample_count,
        "lastMatchedAt": "刚刚",
        # 样本上传后先进入处理中状态；mock 模式稍后会推进到 registered。
        "registerStatus": "processing",
        "modelStatus": "registering",
    }
    # 再显式写一次状态，避免旧注释格式导致字段被注释吞掉时影响业务状态。
    patch["registerStatus"] = "processing"
    patch["modelStatus"] = "registering"

    if not runtime.get("ready"):
        patch["registerStatus"] = "waiting_model_config"
        patch["modelStatus"] = "waiting_model_config"
        # 关闭 mock 且未配置声纹网关时，不伪装注册成功，明确告诉前端和部署人员需要配置模型服务。
        updated = store.update_config_item("voiceprints", voiceprint_id, patch)
        job = store.update_job(
            job["id"],
            status="waiting_model_config",
            current_step="voiceprint_embedding",
            progress=30,
            message="未配置声纹模型服务。请设置 VOICEPRINT_GATEWAY_BASE_URL 并接入 CAM++ 注册接口。",
        )
        return {"status": "waiting_model_config", "voiceprint": updated, "job": job, "sample": sample_record}

    if VOICEPRINT_GATEWAY_BASE_URL:
        try:
            # 有声纹网关地址时，真实把样本提交给本地 CAM++ / 3D-Speaker 服务。
            # 服务返回 embeddingId 后写入 sample_record，方便后续排查某个样本是否真的入库。
            voiceprint_result = LocalVoiceprintClient(VOICEPRINT_GATEWAY_BASE_URL).register(
                speaker_id=voiceprint_id,
                speaker_name=voiceprint.get("name", voiceprint_id),
                audio_path=str(sample_path),
                metadata={"department": voiceprint.get("department", ""), "source": "manual_upload"},
            )
            # Keep an untrusted gateway ID local until every real-model marker has passed. The
            # sample record is already referenced by ``patch['sampleFiles']``; assigning first
            # would leak a fallback ID into durable history even when registration is rejected.
            candidate_embedding_id = str(voiceprint_result.get("embeddingId") or "").strip()
            is_real_registration = (
                voiceprint_result.get("status") == "registered"
                and bool(candidate_embedding_id)
                and voiceprint_result.get("realModel") is True
                and not voiceprint_result.get("fallbackReason")
                and voiceprint_result.get("mockMode") is not True
            )
            if not is_real_registration:
                # A 200 response can still be an unsafe fallback. Treat it as a model failure before
                # touching durable match eligibility, even if a gateway regression claims registered.
                raise LocalModelServiceError("voiceprint registration did not confirm a real CAM++ embedding")
            sample_record["embeddingId"] = candidate_embedding_id
            patch["registerStatus"] = "registered"
            patch["modelStatus"] = voiceprint_result.get("model", "CAM++")
            patch["embeddingId"] = sample_record["embeddingId"]
            patch["realModel"] = True
            patch["fallbackReason"] = ""
        except Exception as exc:  # noqa: BLE001 - every gateway/runtime failure shares one truthful product state.
            patch["registerStatus"] = "failed"
            patch["modelStatus"] = "voiceprint_service_failed"
            patch["realModel"] = False
            patch["fallbackReason"] = str(exc)
            updated = store.update_config_item("voiceprints", voiceprint_id, patch)
            job = store.update_job(
                job["id"],
                status="failed",
                current_step="voiceprint_embedding",
                progress=60,
                message=f"声纹模型服务调用失败：{exc}",
            )
            return {"status": "failed", "voiceprint": updated, "job": job, "sample": sample_record}
    else:
        # This branch is normally unreachable because the health gate above requires a ready
        # configured endpoint. Keep it truthful for races where configuration changes mid-request.
        patch["registerStatus"] = "waiting_model_config"
        patch["modelStatus"] = "waiting_model_config"
    updated = store.update_config_item("voiceprints", voiceprint_id, patch)
    job = store.update_job(job["id"], status="completed", current_step="registered", progress=100)
    return {"status": "completed", "voiceprint": updated, "job": job, "sample": sample_record}


@app.patch("/api/voiceprints/{voiceprint_id}")
def update_voiceprint(voiceprint_id: str, req: ConfigPatchRequest) -> dict[str, Any]:
    """更新声纹库人员资料。"""
    if voiceprint_id not in store.voiceprints:
        raise HTTPException(status_code=404, detail="声纹不存在")
    patch = req.model_dump(exclude_unset=True)
    if "name" in patch:
        patch["speakerName"] = patch["name"]
    if "groupId" in patch:
        group = store.voiceprint_groups.get(patch["groupId"]) or store.voiceprint_groups.get("vg-ungrouped", {})
        patch["groupId"] = group.get("id", "vg-ungrouped")
        patch["groupName"] = group.get("name", "未分组")
    existing = store.voiceprints.get(voiceprint_id, {})
    if False and int(patch.get("samples") or 0) > 0 and existing.get("registerStatus") in {"pending_sample", "waiting_sample", None, ""}:
        # Speaker rename/sync uses the current meeting audio as the first usable sample. Mark the profile as
        # registered so the voiceprint library no longer asks the user to record the same person again.
        patch["registerStatus"] = "registered"
        patch["modelStatus"] = "recognized_from_meeting_audio"
    # store.voiceprints 是从持久化库读取出来的快照，不能直接在快照字典上 update。
    # 这里统一走 Store 的 update_config_item，确保 SQLite 当前立即落库，后续 KingbaseES 适配时也只改 Store 层。
    item = store.update_config_item("voiceprints", voiceprint_id, patch)
    if not item:
        raise HTTPException(status_code=404, detail="声纹不存在")
    return item


@app.delete("/api/voiceprints/{voiceprint_id}")
def delete_voiceprint(voiceprint_id: str) -> dict[str, Any]:
    """删除声纹库人员资料。"""
    if voiceprint_id not in store.voiceprints:
        raise HTTPException(status_code=404, detail="声纹不存在")
    # 删除必须进入持久化 Store，而不是 pop 属性快照；否则页面看似删除，重启后数据仍会回来。
    store.delete_config_item("voiceprints", voiceprint_id)
    return {"deleted": True, "id": voiceprint_id}


@app.get("/api/dictionaries/keyword-libraries")
def list_keyword_libraries() -> dict[str, Any]:
    """获取关键词库列表。"""
    return {"items": list(store.keyword_libraries.values())}


@app.post("/api/dictionaries/keyword-libraries")
def create_keyword_library(req: KeywordLibraryRequest) -> dict[str, Any]:
    """新增关键词库。"""
    return store.create_config_item(
        "keyword_libraries",
        "kw",
        {
            "name": req.name,
            "words": req.words,
            "enabled": req.enabled,
            "scope": req.scope,
        },
    )


@app.patch("/api/dictionaries/keyword-libraries/{library_id}")
def update_keyword_library(library_id: str, req: ConfigPatchRequest) -> dict[str, Any]:
    """更新关键词库。"""
    item = store.update_config_item("keyword_libraries", library_id, req.model_dump(exclude_unset=True))
    if not item:
        raise HTTPException(status_code=404, detail="关键词库不存在")
    return item


@app.delete("/api/dictionaries/keyword-libraries/{library_id}")
def delete_keyword_library(library_id: str) -> dict[str, Any]:
    """删除关键词库。"""
    if not store.delete_config_item("keyword_libraries", library_id):
        raise HTTPException(status_code=404, detail="关键词库不存在")
    return {"deleted": True, "id": library_id}


@app.get("/api/optimization/manual-keywords")
def list_manual_keywords(language: str = "all") -> dict[str, Any]:
    """识别优化中心：查询手动关键词。

    language=zh/en/all 对应前端中文/英文切换；这些词会在真实 ASR 网关中作为热词注入。
    """
    items = list(store.manual_keywords.values())
    if language != "all":
        items = [item for item in items if item.get("language") == language]
    return {"items": items, "total": len(items)}


@app.post("/api/optimization/manual-keywords")
def create_manual_keywords(req: ManualKeywordRequest) -> dict[str, Any]:
    """识别优化中心：保存手动关键词词表。"""
    existing = next(
        (item for item in store.manual_keywords.values() if item.get("language") == req.language),
        None,
    )
    payload = {
        "language": req.language,
        "words": list(dict.fromkeys(word.strip() for word in req.words if word.strip())),
        "enabled": req.enabled,
        "applyScope": req.applyScope,
    }
    # 同一语言只维护一个当前词表；反复点击“保存并应用”必须更新原记录，不能累积旧词继续影响 ASR。
    if existing:
        return store.update_config_item("manual_keywords", existing["id"], payload) or existing
    return store.create_config_item(
        "manual_keywords",
        "mk",
        payload,
    )


@app.patch("/api/optimization/manual-keywords/{keyword_id}")
def update_manual_keywords(keyword_id: str, req: ConfigPatchRequest) -> dict[str, Any]:
    """识别优化中心：更新手动关键词词表。"""
    item = store.update_config_item("manual_keywords", keyword_id, req.model_dump(exclude_unset=True))
    if not item:
        raise HTTPException(status_code=404, detail="手动关键词配置不存在")
    return item


@app.delete("/api/optimization/manual-keywords/{keyword_id}")
def delete_manual_keywords(keyword_id: str) -> dict[str, Any]:
    """识别优化中心：删除手动关键词词表。"""
    if not store.delete_config_item("manual_keywords", keyword_id):
        raise HTTPException(status_code=404, detail="手动关键词配置不存在")
    return {"deleted": True, "id": keyword_id}


@app.post("/api/optimization/document-keywords/files")
async def upload_optimization_document(file: UploadFile = File(...)) -> dict[str, Any]:
    """识别优化中心：上传文档用于关键词抽取。

    真实部署时可在这里接入 doc/docx/ppt/pptx 解析服务；当前先保存文件并创建抽取任务。
    """
    safe_name = Path(file.filename or "optimization-document").name
    doc_id = f"doc-{uuid.uuid4().hex[:8]}"
    path = UPLOAD_DIR / f"{doc_id}-{safe_name}"
    path.write_bytes(await file.read())
    document = {
        "id": doc_id,
        "filename": safe_name,
        "path": str(path),
        "contentType": file.content_type or "",
        "status": "uploaded",
    }
    store.create_config_item("optimization_documents", "doc", document)
    return document


@app.get("/api/optimization/document-keywords/files")
def list_optimization_documents() -> dict[str, Any]:
    """列出已上传的文档优化记录，供创建会议和导入转写显式选择。"""

    items = list(store._list("optimization_documents"))
    return {"items": items, "total": len(items)}


@app.post("/api/optimization/document-keywords/extract")
def extract_document_keywords(body: dict[str, Any]) -> dict[str, Any]:
    """识别优化中心：从文档抽取关键词。

    This route uses deterministic parsed text rather than a model/demo fallback. The persisted
    record becomes an explicit recognition-policy source only when the document is attached to a
    meeting, so an administrator's unrelated upload cannot bias another meeting's ASR request.
    """
    document_id = body.get("documentId", "")
    document = store._get("optimization_documents", document_id)
    if not document:
        raise HTTPException(status_code=404, detail="优化文档不存在")
    job = store.create_job("optimization", "document_keyword_extract", f"抽取 {document.get('filename', document_id)}", ["uploaded", "extracting", "completed"])
    parsed_text = _parse_optimization_document_text(document)
    keywords = extract_document_terms(parsed_text)
    if not parsed_text.strip():
        # A missing parser or a binary-only file is an explicit extraction failure. Returning a
        # successful-looking example list here would let invalid vocabulary enter ASR unnoticed.
        job = store.update_job(job["id"], status="failed", current_step="extracting", progress=100, message="文档未解析出可用文本")
        store.update_config_item(
            "optimization_documents",
            document_id,
            {"status": "failed", "parsedText": "", "keywords": [], "message": "文档未解析出可用文本"},
        )
        return {"documentId": document_id, "keywords": [], "status": "failed", "job": job}

    meeting_id = str(body.get("meetingId") or "").strip()
    meeting_ids = list(dict.fromkeys([*document.get("meetingIds", []), *([meeting_id] if meeting_id else [])]))
    document = store.update_config_item(
        "optimization_documents",
        document_id,
        {
            "status": "completed",
            "keywords": keywords,
            "extractedTerms": keywords,
            "parsedText": parsed_text,
            "meetingIds": meeting_ids,
            "extractionSource": "deterministic_parsed_text",
            "confirmed": False,
        },
    )
    if meeting_id:
        meeting = store.get_or_create_meeting(meeting_id)
        # This separate meeting-level link is intentional. The immutable creation snapshot remains
        # intact, while an explicit later attachment is durable evidence that this extracted record
        # is allowed to contribute to this meeting's future recognition policy.
        attached_ids = list(dict.fromkeys([*(meeting.get("documentKeywordDocumentIds") or []), document_id]))
        meeting["documentKeywordDocumentIds"] = attached_ids
        store._save("meetings", meeting)
    job = store.update_job(job["id"], status="completed", current_step="completed", progress=100)
    return {"documentId": document_id, "keywords": keywords, "status": "completed", "document": document, "job": job}


@app.post("/api/optimization/document-keywords/{document_id}/confirm")
def confirm_document_keywords(document_id: str, req: DocumentKeywordConfirmRequest) -> dict[str, Any]:
    """确认并保存可进入识别配置的文档关键词。"""

    document = store._get("optimization_documents", document_id)
    if not document:
        raise HTTPException(status_code=404, detail="优化文档不存在")
    keywords = list(dict.fromkeys(word.strip() for word in req.keywords if word.strip()))
    if not keywords:
        raise HTTPException(status_code=400, detail="请至少确认一个文档关键词")
    return store.update_config_item(
        "optimization_documents",
        document_id,
        {"keywords": keywords, "extractedTerms": keywords, "confirmed": True, "status": "completed"},
    ) or document


def _parse_uploaded_word_list(filename: str, data: bytes) -> list[str]:
    """解析 TXT/DOCX/PPTX 词表，保留用户显式的分隔顺序并去重。"""

    suffix = Path(filename).suffix.lower()
    text = ""
    if suffix in {".docx", ".pptx"}:
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                prefixes = ("word/",) if suffix == ".docx" else ("ppt/slides/",)
                text = _extract_office_xml_text(archive, prefixes)
        except zipfile.BadZipFile:
            return []
    else:
        for encoding in ("utf-8", "utf-16", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    values = [item.strip() for item in re.split(r"[\n\r,，;；、]+", text) if item.strip()]
    return list(dict.fromkeys(values))


@app.post("/api/optimization/word-files/parse")
async def parse_optimization_word_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """解析手动关键词或禁忌词导入文件，不在确认前修改任何配置。"""

    words = _parse_uploaded_word_list(Path(file.filename or "words.txt").name, await file.read())
    if not words:
        raise HTTPException(status_code=400, detail="文件中未解析出有效词条")
    return {"filename": Path(file.filename or "words.txt").name, "words": words}


@app.post("/api/optimization/manual-keywords/export")
def export_manual_keywords(req: DictionaryRequest) -> Response:
    """导出手动关键词 DOCX。"""

    return Response(
        content=build_word_list_docx("识别优化关键词", req.words),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": "attachment; filename=recognition-keywords.docx"},
    )


@app.post("/api/dictionaries/sensitive-rules/export")
def export_sensitive_rule_words(req: DictionaryRequest) -> Response:
    """导出当前禁忌词 DOCX。"""

    return Response(
        content=build_word_list_docx("智能会议禁忌词", req.words),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": "attachment; filename=forbidden-words.docx"},
    )


@app.post("/api/optimization/smart-keywords/generate")
def generate_smart_keywords(body: dict[str, Any]) -> dict[str, Any]:
    """识别优化中心：智能生成关键词。

    Suggestions are deterministic candidates from this meeting's actual title, participants, and
    persisted transcript. They remain inactive until the caller submits ``confirmedTerms``; this
    avoids treating a heuristic or model suggestion as an automatic ASR configuration change.
    """
    meeting_id = str(body.get("meetingId") or "").strip()
    limit = int(body.get("limit", 10) or 10)
    meeting = store.get_or_create_meeting(meeting_id) if meeting_id else {}
    snapshot = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
    source_text = "\n".join(
        item
        for item in [
            str(meeting.get("meetingName") or meeting.get("fileName") or "").strip(),
            *[str(name or "").strip() for name in snapshot.get("participantNames") or []],
            *[str(segment.get("rawText") or segment.get("text") or "").strip() for segment in meeting.get("segments") or []],
        ]
        if item
    )
    keywords = extract_document_terms(source_text, maximum_terms=max(0, limit))
    confirmed_terms = _canonical_recognition_terms(body.get("confirmedTerms") or body.get("confirmedKeywords") or [])
    if meeting_id and confirmed_terms:
        existing_records = meeting.get("smartKeywordTerms") or []
        existing_terms = {
            str(record.get("term") or record.get("word") or "").strip()
            for record in existing_records
            if isinstance(record, dict) and record.get("confirmed") is True
        }
        meeting["smartKeywordTerms"] = [
            *existing_records,
            *({"term": term, "confirmed": True} for term in confirmed_terms if term not in existing_terms),
        ]
        store._save("meetings", meeting)
    return {
        "meetingId": meeting_id,
        "keywords": keywords,
        "confirmedTerms": confirmed_terms,
        "source": "deterministic_meeting_text",
    }


def _canonical_recognition_terms(values: Any) -> list[str]:
    """Normalize user-confirmed smart terms before they become a durable meeting source."""

    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def _parse_optimization_document_text(document: dict[str, Any]) -> str:
    """Parse supported uploaded text/Office content without manufacturing a fallback document."""

    path = Path(str(document.get("path") or ""))
    if not path.is_file():
        return ""
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix in {".docx", ".pptx"}:
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                prefixes = ("word/",) if suffix == ".docx" else ("ppt/slides/",)
                return _extract_office_xml_text(archive, prefixes).strip()
        except zipfile.BadZipFile:
            return ""
    for encoding in ("utf-8", "utf-16", "gb18030"):
        try:
            text = data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
        if text:
            return text
    return ""


@app.get("/api/optimization/replacement-rules")
def list_replacement_rules() -> dict[str, Any]:
    """识别优化中心：查询强制替换规则。"""
    return {"items": list(store.replacement_rules.values())}


@app.post("/api/optimization/replacement-rules")
def create_replacement_rule(req: ReplacementRuleRequest) -> dict[str, Any]:
    """识别优化中心：新增强制替换规则。"""
    return store.create_config_item(
        "replacement_rules",
        "rr",
        {
            "wrongWord": req.wrongWord,
            "correctWord": req.correctWord,
            "enabled": req.enabled,
            "applyScope": req.applyScope,
        },
    )


@app.patch("/api/optimization/replacement-rules/{rule_id}")
def update_replacement_rule(rule_id: str, req: ConfigPatchRequest) -> dict[str, Any]:
    """识别优化中心：更新强制替换规则。"""
    item = store.update_config_item("replacement_rules", rule_id, req.model_dump(exclude_unset=True))
    if not item:
        raise HTTPException(status_code=404, detail="强制替换规则不存在")
    return item


@app.delete("/api/optimization/replacement-rules/{rule_id}")
def delete_replacement_rule(rule_id: str) -> dict[str, Any]:
    """识别优化中心：删除强制替换规则。"""
    if not store.delete_config_item("replacement_rules", rule_id):
        raise HTTPException(status_code=404, detail="强制替换规则不存在")
    return {"deleted": True, "id": rule_id}


@app.get("/api/dictionaries/sensitive-rules")
def list_sensitive_rules() -> dict[str, Any]:
    """获取敏感词规则列表。"""
    return {"items": list(store.sensitive_rules.values())}


@app.post("/api/dictionaries/sensitive-rules")
def create_sensitive_rule(req: SensitiveRuleRequest) -> dict[str, Any]:
    """新增敏感词规则。"""
    display_mode = req.displayMode if req.displayMode is not None else req.replacement
    apply_scope = req.applyScope if req.applyScope is not None else req.scope
    return store.create_config_item(
        "sensitive_rules",
        "sw",
        {
            "word": req.word,
            "replacement": req.replacement,
            "displayMode": display_mode,
            "enabled": req.enabled,
            "scope": req.scope,
            "remark": req.remark,
            "caseSensitive": req.caseSensitive,
            "language": req.language,
            "applyScope": apply_scope,
        },
    )


@app.patch("/api/dictionaries/sensitive-rules/{rule_id}")
def update_sensitive_rule(rule_id: str, req: ConfigPatchRequest) -> dict[str, Any]:
    """更新敏感词规则。"""
    item = store.update_config_item("sensitive_rules", rule_id, req.model_dump(exclude_unset=True))
    if not item:
        raise HTTPException(status_code=404, detail="敏感词规则不存在")
    return item


@app.delete("/api/dictionaries/sensitive-rules/{rule_id}")
def delete_sensitive_rule(rule_id: str) -> dict[str, Any]:
    """删除敏感词规则。"""
    if not store.delete_config_item("sensitive_rules", rule_id):
        raise HTTPException(status_code=404, detail="敏感词规则不存在")
    return {"deleted": True, "id": rule_id}


TEMPLATE_TAG_CANDIDATES = [
    "会议主题",
    "会议时间",
    "会议地点",
    "主持人",
    "记录人",
    "参会人",
    "会议纪要",
    "会议待办",
    "文本输入",
]


def _decode_template_bytes(data: bytes) -> str:
    """把模板文件的原始字节转为文本。

    用户本地模板可能来自 Word、WPS 或纯文本，编码不一定统一。这里先尝试 UTF-8，
    再回退到 GBK，确保政企内网环境里常见的中文 txt 模板也能被识别。
    """
    for encoding in ("utf-8", "gbk", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _extract_office_xml_text(archive: zipfile.ZipFile, prefixes: tuple[str, ...]) -> str:
    """从 docx/pptx 这类 Office Open XML 压缩包中抽取文本节点。

    这里不引入 python-docx 等额外依赖，避免当前工程安装成本上升；后续如果需要保留表格坐标、
    字体和段落样式，可以把这个函数替换成专门的模板解析服务，外部 API 不需要改变。
    """
    parts: list[str] = []
    for name in archive.namelist():
        if not name.endswith(".xml") or not name.startswith(prefixes):
            continue
        xml_text = archive.read(name).decode("utf-8", errors="ignore")
        # w:t/a:t 是 Word/PPT 中常见的文本节点；用正则足够提取原型阶段需要的标签文字。
        parts.extend(html.unescape(match) for match in re.findall(r"<(?:w|a):t[^>]*>(.*?)</(?:w|a):t>", xml_text))
    return "\n".join(part.strip() for part in parts if part.strip())


def extract_template_text(filename: str, data: bytes) -> str:
    """解析用户上传的纪要模板正文。

    支持 txt/docx/pptx；二进制 doc 需要后续接 LibreOffice/WPS 或文档解析服务，本接口会明确返回
    “待接入解析服务”的占位文本，不会伪装成已经完成真实解析。
    """
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        return _decode_template_bytes(data).strip()
    if suffix in {".docx", ".pptx"}:
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                prefixes = ("word/",) if suffix == ".docx" else ("ppt/slides/",)
                extracted = _extract_office_xml_text(archive, prefixes)
                if extracted:
                    return extracted
        except zipfile.BadZipFile:
            pass
    if suffix == ".doc":
        return "该 .doc 模板已上传，二进制解析需后续接入 LibreOffice/WPS 转换服务。会议主题 会议时间 会议地点 主持人 记录人 参会人 会议纪要"
    return _decode_template_bytes(data).strip() or "会议主题 会议时间 会议地点 主持人 记录人 参会人 会议纪要 会议待办"


def infer_template_tags(text: str, explicit_tags: str = "") -> list[str]:
    """从模板文本中识别可配置标签。

    前端允许用户手工勾选标签；如果传入 explicit_tags，则以用户配置为准。否则从模板文本中自动识别，
    再兜底为常见会议纪要字段，保证导入模板保存后一定能参与“识别语音后自动填充”。
    """
    tags = [tag.strip() for tag in re.split(r"[,，;；\n]", explicit_tags or "") if tag.strip()]
    if tags:
        return list(dict.fromkeys(tags))
    detected = [tag for tag in TEMPLATE_TAG_CANDIDATES if tag in text]
    return detected or TEMPLATE_TAG_CANDIDATES[:7]


def build_template_tag_bindings(tags: list[str]) -> list[dict[str, Any]]:
    """把页面标签转换成后续自动填充可消费的绑定结构。"""
    field_map = {
        "会议主题": "meetingName",
        "会议时间": "meetingTime",
        "会议地点": "meetingLocation",
        "主持人": "host",
        "记录人": "recorder",
        "参会人": "participants",
        "会议纪要": "minutes",
        "会议待办": "todos",
        "文本输入": "manualText",
    }
    return [
        {
            "tag": tag,
            "sourceField": field_map.get(tag, "summary"),
            "required": tag in {"会议主题", "会议纪要"},
            "description": f"语音识别和 AI 纪要生成后自动填充“{tag}”区域",
        }
        for tag in tags
    ]


def build_template_preview_content(template: dict[str, Any]) -> str:
    """为内置模板或老数据生成可预览的模板文本。

    系统模板不一定来自真实文件，但前端需要展示类似 Word 模板的预览，并且会议纪要生成时需要知道
    哪些区域可以自动填充。这里按 sections 生成稳定占位内容，后续可以替换为真实模板文件解析结果。
    """
    sections = template.get("sections") or TEMPLATE_TAG_CANDIDATES[:7]
    lines = [template.get("name", "会议纪要模板"), ""]
    for section in sections:
        lines.append(f"【{section}】")
        lines.append(f"{{{{{section}}}}}")
        lines.append("")
    return "\n".join(lines).strip()


def normalize_template_for_response(template: dict[str, Any]) -> dict[str, Any]:
    """补齐模板响应字段，兼容已经落库的旧模板数据。"""
    item = dict(template)
    tags = item.get("tags") or item.get("sections") or TEMPLATE_TAG_CANDIDATES[:7]
    item["tags"] = tags
    item["tagBindings"] = item.get("tagBindings") or build_template_tag_bindings(tags)
    item["content"] = item.get("content") or build_template_preview_content(item)
    item["fillStrategy"] = item.get("fillStrategy") or "mock_auto_fill"
    item["originFilename"] = item.get("originFilename") or ""
    return item


def build_imported_template_payload(
    *,
    safe_name: str,
    content: str,
    name: str = "",
    template_type: str = "自定义会议",
    tags: str = "",
    is_default: bool = False,
) -> dict[str, Any]:
    """把模板文件解析结果整理成前后端共用结构。

    “识别模板”和“保存模板”两条接口都需要执行同样的标签识别、绑定生成和名称兜底。如果分散写在
    两个路由里，很容易出现预览时识别到一套标签、保存后又变成另一套标签的问题。这个函数把解析后
    的业务结构统一下来，前端预览和最终落库看到的字段保持完全一致。
    """

    resolved_tags = infer_template_tags(content, tags)
    template_name = name.strip() or Path(safe_name).stem or "导入会议纪要模板"
    return {
        "name": template_name,
        "type": template_type,
        "isDefault": is_default,
        "sections": resolved_tags,
        "source": "my",
        "isSystem": False,
        "previewType": "imported",
        "tags": resolved_tags,
        "description": f"从本地文件 {safe_name} 解析生成，可配置文本标签并用于识别后自动填充。",
        "content": content,
        "originFilename": safe_name,
        "tagBindings": build_template_tag_bindings(resolved_tags),
        "fillStrategy": "deepseek_auto_fill",
    }


@app.get("/api/minute-templates")
def list_templates(source: str = "all") -> dict[str, Any]:
    """获取纪要模板列表。

    source=my/system/all 对应前端“我的模板/系统模板”双 Tab；系统模板是内置模板资产，
    只能复制到我的模板后编辑，不能直接删除。
    """
    items = [normalize_template_for_response(item) for item in store.list_templates(source)]
    return {"items": items, "total": len(items)}


@app.post("/api/minute-templates")
def create_template(req: TemplateRequest) -> dict[str, Any]:
    """新增纪要模板。"""
    if req.isDefault:
        # 默认模板具有唯一性。先把现有模板默认标记写回数据库，再创建新默认模板。
        for template in store.templates.values():
            if template.get("isDefault"):
                store.update_config_item("templates", template["id"], {"isDefault": False})
    return store.create_config_item(
        "templates",
        "tpl",
        {
            "name": req.name,
            "type": req.type,
            "isDefault": req.isDefault,
            "sections": req.sections,
            "source": req.source,
            "isSystem": False,
            "previewType": req.previewType,
            "tags": req.tags,
            "description": req.description,
            "content": req.content,
            "originFilename": req.originFilename,
            "tagBindings": req.tagBindings,
            "fillStrategy": req.fillStrategy,
        },
    )


@app.post("/api/minute-templates/import")
def import_template(req: TemplateImportRequest) -> dict[str, Any]:
    """导入用户纪要模板。

    当前接口先保存结构化模板信息；后续如需上传 docx 模板文件，可新增文件字段并在这里解析后写入 sections。
    """
    return create_template(
        TemplateRequest(
            name=req.name,
            type=req.type,
            isDefault=req.isDefault,
            sections=req.sections,
            source="my",
            previewType=req.previewType or "custom",
            tags=req.tags,
            description=req.description,
            content=req.content,
            originFilename=req.originFilename,
            tagBindings=req.tagBindings,
            fillStrategy=req.fillStrategy,
        )
    )


@app.post("/api/minute-templates/parse-file")
async def parse_template_file(
    file: UploadFile = File(...),
    name: str = Form(""),
    templateType: str = Form("自定义会议"),
    tags: str = Form(""),
    isDefault: bool = Form(False),
) -> dict[str, Any]:
    """解析本地纪要模板文件但不保存。

    前端导入弹窗的“识别模板”按钮调用该接口。它会真实读取 txt/docx/pptx 内容、识别可填充标签、
    生成 tagBindings，并把同一份结构返回给页面预览。用户确认后再调用 import-file 保存，避免
    “页面显示的是前端猜测，保存后才由后端解析”的割裂体验。
    """

    safe_name = Path(file.filename or "meeting-template.txt").name
    content = extract_template_text(safe_name, await file.read())
    default_flag = isDefault if isinstance(isDefault, bool) else False
    return build_imported_template_payload(
        safe_name=safe_name,
        content=content,
        name=name,
        template_type=templateType,
        tags=tags,
        is_default=default_flag,
    )


@app.post("/api/minute-templates/import-file")
async def import_template_file(
    file: UploadFile = File(...),
    name: str = Form(""),
    templateType: str = Form("自定义会议"),
    tags: str = Form(""),
    isDefault: bool = Form(False),
) -> dict[str, Any]:
    """从本地文件导入纪要模板，并生成可配置文本标签。

    这是模板页从“展示卡片”走向“可用系统”的关键接口：用户上传本地 docx/txt/pptx 后，
    后端抽取模板文字、识别可填充标签、保存标签绑定。真实接入大模型后，
    `/api/meetings/{id}/minutes/generate` 可以直接读取 tagBindings 把 ASR/摘要结果写入模板区域。
    """
    safe_name = Path(file.filename or "meeting-template.txt").name
    content = extract_template_text(safe_name, await file.read())
    # 单元测试会直接调用路由函数，此时 FastAPI 还没有把 Form(False) 解析成 bool；
    # HTTP 请求路径下这里收到的已经是布尔值。统一归一化后两种调用方式都稳定。
    default_flag = isDefault if isinstance(isDefault, bool) else False
    parsed = build_imported_template_payload(
        safe_name=safe_name,
        content=content,
        name=name,
        template_type=templateType,
        tags=tags,
        is_default=default_flag,
    )
    return create_template(
        TemplateRequest(
            name=parsed["name"],
            type=parsed["type"],
            isDefault=parsed["isDefault"],
            sections=parsed["sections"],
            source="my",
            previewType=parsed["previewType"],
            tags=parsed["tags"],
            description=parsed["description"],
            content=parsed["content"],
            originFilename=parsed["originFilename"],
            tagBindings=parsed["tagBindings"],
            fillStrategy=parsed["fillStrategy"],
        )
    )


@app.post("/api/minute-templates/{template_id}/copy")
def copy_template(template_id: str) -> dict[str, Any]:
    """复制系统模板到我的模板。"""
    copied = store.copy_template(template_id)
    if not copied:
        raise HTTPException(status_code=404, detail="纪要模板不存在")
    return copied


@app.patch("/api/minute-templates/{template_id}")
def update_template(template_id: str, req: ConfigPatchRequest) -> dict[str, Any]:
    """更新纪要模板。"""
    patch = req.model_dump(exclude_unset=True)
    if patch.get("isDefault"):
        # 与新增模板一致，切换默认模板时必须持久化清理其它默认标记。
        for template in store.templates.values():
            if template["id"] != template_id and template.get("isDefault"):
                store.update_config_item("templates", template["id"], {"isDefault": False})
    item = store.update_config_item("templates", template_id, patch)
    if not item:
        raise HTTPException(status_code=404, detail="纪要模板不存在")
    return item


@app.delete("/api/minute-templates/{template_id}")
def delete_template(template_id: str) -> dict[str, Any]:
    """删除纪要模板。"""
    try:
        deleted = store.delete_config_item("templates", template_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="纪要模板不存在")
    return {"deleted": True, "id": template_id}


@app.post("/api/dictionaries/hotwords")
def set_hotwords(req: DictionaryRequest) -> dict[str, Any]:
    """维护关键词优化词库。"""
    store.hotwords = [word.strip() for word in req.words if word.strip()]
    return {"items": store.hotwords}


@app.post("/api/dictionaries/sensitive-words")
def set_sensitive_words(req: DictionaryRequest) -> dict[str, Any]:
    """维护敏感词词库。"""
    store.sensitive_words = [word.strip() for word in req.words if word.strip()]
    return {"items": store.sensitive_words}


@app.post("/api/meetings/{meeting_id}/summaries/generate")
def generate_summary(meeting_id: str) -> dict[str, Any]:
    """生成会议关键词、概要、要点、待办和发言总结。"""
    meeting = store.get_or_create_meeting(meeting_id)
    meeting, rules, _rule_version = _frozen_sensitive_policy_for_meeting(meeting)
    # 仅清理送入 AI 的深拷贝：早期实时版本写入的 corpus 回声仍保留在数据库供审计，
    # 但摘要、纪要、待办和规整不能再把这些伪文本当成真实发言。
    meeting = filter_realtime_ai_segments(meeting)
    if not meeting_has_transcript_text(meeting):
        return attach_ai_tool_ui("summary", empty_transcript_ai_payload("summary"))
    # Preserve exactly the transcript provenance supplied to the model.  The workflow may run for
    # seconds, during which realtime finals or user edits can advance the durable meeting revision.
    generation_revision = int(meeting.get("transcriptRevision", 0))
    generation_source_ids = [str(segment.get("id") or "") for segment in meeting.get("segments", [])]
    ai_meeting, policy_audit = prepare_sensitive_ai_meeting(meeting, rules)
    summary = generate_summary_with_workflow(ai_meeting)
    # 章节速览返回底层片段范围，前端才能同时定位逐字稿与录音；该字段不改变既有摘要正文契约。
    if isinstance(summary, dict) and isinstance(summary.get("sections"), list):
        summary["sections"] = _attach_source_ranges(summary["sections"], meeting.get("segments") or [])
        # 标准摘要工作流返回 sections 时一并补充按人总结；兼容仅含 overview 的旧/第三方结果，不扩写其持久化结构。
        speaker_summaries: list[dict[str, Any]] = []
        for speaker_name in dict.fromkeys(
            str(segment.get("speakerName") or "未识别发言人") for segment in ai_meeting.get("segments") or []
        ):
            speaker_segments = [
                segment for segment in ai_meeting.get("segments") or []
                if str(segment.get("speakerName") or "未识别发言人") == speaker_name
            ]
            speaker_summaries.append(
                {
                    "speakerName": speaker_name,
                    "summary": " ".join(str(segment.get("text") or "") for segment in speaker_segments)[:240],
                    "sourceRanges": [
                        {
                            "segmentId": str(segment.get("id") or ""),
                            "startMs": int(segment.get("startMs") or 0),
                            "endMs": int(segment.get("endMs") or segment.get("startMs") or 0),
                        }
                        for segment in speaker_segments
                    ],
                }
            )
        summary["speakerSummaries"] = speaker_summaries
    artifact = store.save_derived_artifact(
        meeting_id,
        "summary",
        summary,
        generation_source_ids,
        generation_transcript_revision=generation_revision,
        sensitive_policy=policy_audit,
    )
    # Retain all prior summary fields while adding a source envelope, keeping the public response
    # backward compatible for current frontend callers and useful to provenance-aware clients.
    return attach_ai_tool_ui("summary", {**summary, **artifact})


@app.get("/api/meetings/{meeting_id}/minutes/versions")
def list_minutes_versions(meeting_id: str) -> dict[str, Any]:
    """Expose ordered immutable generation history without changing the existing minutes path."""

    # The store refreshes stale state before returning the copy, so a history request cannot make
    # an old transcript revision look current merely because no detail page was opened first.
    meeting = store.get_or_create_meeting(meeting_id)
    return {
        "items": store.list_minutes_versions(meeting_id),
        "currentVersionId": meeting.get("minutesCurrentVersionId"),
        "transcriptRevision": meeting.get("transcriptRevision", 0),
    }


@app.post("/api/meetings/{meeting_id}/minutes/generate")
def generate_minutes(meeting_id: str, req: MinutesRequest) -> dict[str, Any]:
    """按内置模板生成一键纪要。"""
    meeting = store.get_or_create_meeting(meeting_id)
    try:
        # Resolve before the model call. The returned snapshot is bound to this version only;
        # explicit switching never rewrites ``processingConfig.templateSnapshot`` for the meeting.
        template_id, template_snapshot = resolve_minutes_template(meeting, req.templateId, store.templates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    meeting, rules, _rule_version = _frozen_sensitive_policy_for_meeting(meeting)
    meeting = filter_realtime_ai_segments(meeting)
    if not meeting_has_transcript_text(meeting):
        return attach_ai_tool_ui("minutes", empty_transcript_ai_payload("minutes"))
    ai_meeting, policy_audit = prepare_sensitive_ai_meeting(meeting, rules)
    ai_template, template_audit = prepare_sensitive_ai_template(template_snapshot, rules)
    # Transcript and every nested template string use the same frozen Task 4 policy. Merge their
    # hit lists before persisting so one version contains complete evidence of AI-bound inputs.
    policy_audit = _policy_audit("ai", policy_audit["ruleVersion"], [*policy_audit["hits"], *template_audit["hits"]])
    ai_template_name = str(ai_template.get("name") or template_snapshot.get("name") or req.templateName)
    minutes = generate_minutes_with_workflow(ai_meeting, ai_template_name, ai_template)
    version = generate_minutes_version(
        meeting,
        template_id,
        int(meeting.get("transcriptRevision", 0)),
        template_snapshot=template_snapshot,
        generated_content=minutes,
        sensitive_policy=policy_audit,
    )
    version = store.save_minutes_version(meeting_id, version)
    # Keep existing top-level minutes fields while exposing the version envelope to newer callers.
    return attach_ai_tool_ui("minutes", {**minutes, **version})


@app.post("/api/meetings/{meeting_id}/minutes/draft")
def save_minutes_draft(meeting_id: str, req: MinutesDraftRequest) -> dict[str, Any]:
    """保存用户在右侧 AI 面板里编辑过的正文为会议纪要。

    前端“添加至纪要”按钮不应该只是复制文本或弹 toast；它要把用户修改后的 AI 草稿落到会议记录里。
    这里复用 store.set_minutes，让会议状态、纪要归档状态和持久化路径与正式生成纪要保持一致。
    """

    content = req.content.strip()
    minutes = {
        "content": content,
        "sourceTool": req.sourceTool,
        "updatedAt": format_datetime(),
    }
    try:
        # A supplied ID targets that immutable generation record. When omitted, the store selects
        # the current pointer for backward-compatible "edit current minutes" behavior.
        artifact = store.set_edited_minutes_version_content(
            meeting_id,
            content,
            version_id=req.versionId,
            legacy_payload=minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if artifact is None:
        # A pre-generation draft has no version to edit yet. Retain the Task 2 draft envelope so
        # existing workflows can save human text before any model-generated minutes exist.
        artifact = store.set_edited_artifact_content(
            meeting_id,
            "minutes",
            content,
            legacy_payload=minutes,
        )
    # The existing draft response remains intact; the envelope keeps edited text separate from
    # generated content so a later stale transition cannot discard a person's changes.
    return attach_ai_tool_ui("minutes", {**minutes, **(artifact or {})})


@app.post("/api/meetings/{meeting_id}/ai-tools/{tool}/draft")
def save_ai_tool_draft(meeting_id: str, tool: str, req: ToolDraftRequest) -> dict[str, Any]:
    """保存右侧 AI 工具面板的当前编辑结果。

    这个接口服务于“保存”按钮：保存后用户可以切换到规整/纪要/待办/标记等其他工具，
    再回到当前工具时直接看到草稿，不需要重新生成。真正写入会议纪要仍由 minutes/draft 负责。
    """

    title = req.title or AI_TOOL_UI_TITLES.get(tool, "AI 结果")
    draft = store.save_ai_tool_draft(meeting_id, tool, title, req.content.strip())
    artifact_type = {"summary": "summary", "minutes": "minutes", "todos": "todos", "reorganize": "discourse"}.get(tool)
    if artifact_type:
        artifact = store.set_edited_artifact_content(meeting_id, artifact_type, req.content.strip())
        if artifact:
            draft["artifact"] = artifact
    return draft


@app.post("/api/meetings/{meeting_id}/todos/extract")
def extract_todos(meeting_id: str) -> dict[str, Any]:
    """从会议内容中抽取待办。"""
    meeting = store.get_or_create_meeting(meeting_id)
    meeting, rules, _rule_version = _frozen_sensitive_policy_for_meeting(meeting)
    meeting = filter_realtime_ai_segments(meeting)
    if not meeting_has_transcript_text(meeting):
        return attach_ai_tool_ui("todos", empty_transcript_ai_payload("todos"))
    # These identifiers and this revision are the pre-model transcript snapshot, not a later
    # reloaded meeting.  The atomic store boundary uses them to classify a late result as stale.
    generation_revision = int(meeting.get("transcriptRevision", 0))
    generation_source_ids = [str(segment.get("id") or "") for segment in meeting.get("segments", [])]
    ai_meeting, policy_audit = prepare_sensitive_ai_meeting(meeting, rules)
    todos = extract_todos_with_workflow(ai_meeting)
    # 标准待办工作流至少返回 content/ownerDept/dueDate 之一；极简旧插件可能只返回 title，
    # 对后者保持原契约，避免兼容字段被无条件扩写。产品工作流则持久化完整闭环字段和来源。
    if any(
        isinstance(item, dict) and any(key in item for key in ("content", "ownerDept", "dueDate", "status", "sourceRanges"))
        for item in todos
    ):
        todos = _attach_source_ranges(todos, meeting.get("segments") or [])
        for item in todos:
            item.setdefault("owner", item.get("ownerDept") or "")
            item.setdefault("deadline", item.get("dueDate") or "")
            item.setdefault("status", "pending")
    payload = {"items": todos}
    artifact = store.save_derived_artifact(
        meeting_id,
        "todos",
        payload,
        generation_source_ids,
        generation_transcript_revision=generation_revision,
        sensitive_policy=policy_audit,
    )
    return attach_ai_tool_ui("todos", {**payload, **artifact})


@app.post("/api/meetings/{meeting_id}/todos/push")
def push_todos(meeting_id: str) -> dict[str, Any]:
    """构造普通会议系统待办推送 payload。

    当前不主动访问外部系统，避免没有 token/base_url 时失败；返回 payload 供联调确认。
    """
    meeting = store.get_or_create_meeting(meeting_id)
    payload = build_task_save_request(
        meeting.get("todos", []),
        meeting_id=meeting_id,
        meeting_name=meeting.get("meetingName", ""),
    )
    return {"target": "/task/management/meeting/taskSave", "payload": payload}


@app.patch("/api/meetings/{meeting_id}/todos/{todo_index}")
def update_todo(meeting_id: str, todo_index: int, req: TodoPatchRequest) -> dict[str, Any]:
    """保存负责人、截止时间和办理状态，保留系统生成的来源范围。"""

    try:
        return store.update_meeting_todo(meeting_id, todo_index, req.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(status_code=404, detail="会议或待办不存在") from None


@app.post("/api/meetings/{meeting_id}/postprocess")
def run_post_meeting_pipeline(meeting_id: str) -> dict[str, Any]:
    """独立执行会后摘要、发言人总结、待办和纪要，每项失败不阻断其他结果。"""

    meeting = store.get_or_create_meeting(meeting_id)
    if not meeting_has_transcript_text(meeting):
        raise HTTPException(status_code=400, detail="会议暂无逐字稿，不能启动会后处理")
    actions: list[tuple[str, str, Any]] = [
        ("summary", "章节与全文摘要", lambda: generate_summary(meeting_id)),
        ("speaker_summary", "发言人总结", lambda: generate_speaker_summary(meeting_id)),
        ("todos", "会议待办", lambda: extract_todos(meeting_id)),
        ("minutes", "模板纪要", lambda: generate_minutes(meeting_id, MinutesRequest())),
    ]
    results: dict[str, Any] = {}
    for item_type, title, action in actions:
        job = store.create_job(meeting_id, f"postprocess_{item_type}", title, ["pending", "generating", "completed"])
        store.update_job(job["id"], status="running", current_step="generating", progress=45)
        try:
            payload = action()
            store.update_job(job["id"], status="completed", current_step="completed", progress=100)
            results[item_type] = {"status": "completed", "jobId": job["id"], "result": payload}
        except Exception as exc:  # noqa: BLE001 - 单项失败必须转成可重试状态，不能中断其余流水线。
            store.update_job(job["id"], status="failed", current_step="generating", progress=100, message=str(exc))
            results[item_type] = {"status": "failed", "jobId": job["id"], "message": str(exc)}
    return {"meetingId": meeting_id, "items": results}


@app.post("/api/meetings/{meeting_id}/discourse/reorganize")
def reorganize_meeting_discourse(meeting_id: str) -> dict[str, Any]:
    """利用大模型工作流做语篇规整；mock 下做轻量分段。"""
    meeting = store.get_or_create_meeting(meeting_id)
    meeting, rules, _rule_version = _frozen_sensitive_policy_for_meeting(meeting)
    meeting = filter_realtime_ai_segments(meeting)
    text = "。".join(segment.get("text", "") for segment in meeting.get("segments", []))
    if not text.strip():
        # 默认冒烟和用户刚创建会议时可能还没有 ASR 片段。此时返回可读提示，
        # 比空字符串更适合前端工具面板展示，也能明确告诉用户下一步要先导入或转写音频。
        return attach_ai_tool_ui("reorganize", {"text": "暂无可规整的转写内容，请先完成实时转写或离线音视频导入。"})
    # The discourse string above and this provenance pair come from the same detached snapshot.
    # Do not derive either from a post-model reload, or an older reorganization can look current.
    generation_revision = int(meeting.get("transcriptRevision", 0))
    generation_source_ids = [str(segment.get("id") or "") for segment in meeting.get("segments", [])]
    ai_text, policy_audit = prepare_sensitive_ai_text(text, rules)
    payload = {"text": reorganize_discourse(ai_text)}
    artifact = store.save_derived_artifact(
        meeting_id,
        "discourse",
        payload,
        generation_source_ids,
        generation_transcript_revision=generation_revision,
        sensitive_policy=policy_audit,
    )
    return attach_ai_tool_ui("reorganize", {**payload, **artifact})


@app.post("/api/meetings/{meeting_id}/highlights")
def add_highlight(meeting_id: str, body: dict[str, str]) -> dict[str, Any]:
    """保存重点标记，后续可随 docx 导出。"""
    try:
        item = store.add_highlight(meeting_id, body.get("text", ""), body.get("segmentId", ""))
    except ValueError as exc:
        # A supplied segment ID must be a persisted segment of this meeting. Returning a client
        # error prevents callers from receiving a seemingly current marker with unusable source
        # provenance, while an intentionally blank ID remains the explicit ``unlinked`` state.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Preserve the legacy marker fields and expose the envelope at the top level like the other
    # AI tools. The nested ``artifact`` remains for existing consumers of the highlights table.
    return attach_ai_tool_ui("mark", {**item, **item["artifact"]})


@app.post("/api/meetings/exports/archive")
def export_meetings_archive(req: BatchMeetingExportRequest) -> Response:
    """把勾选会议的 DOCX 一次性打包成 ZIP，并逐场应用各自冻结的禁忌词策略。"""

    meeting_ids = list(dict.fromkeys(str(item or "").strip() for item in req.meetingIds if str(item or "").strip()))
    if not meeting_ids:
        raise HTTPException(status_code=400, detail="请至少选择一场会议")
    archive_buffer = BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, meeting_id in enumerate(meeting_ids, start=1):
            try:
                meeting = store.get_meeting(meeting_id)
            except KeyError:
                raise HTTPException(status_code=404, detail=f"会议不存在：{meeting_id}") from None
            meeting, rules, _rule_version = _frozen_sensitive_policy_for_meeting(meeting)
            data, policy_audit = build_meeting_docx(
                meeting,
                req.exportKind,
                sensitive_rules=rules,
                include_policy_audit=True,
            )
            _record_export_policy_audit(meeting_id, f"archive-docx:{req.exportKind}", policy_audit)
            # ZIP 内文件名只移除路径分隔符，保留中文标题；重复标题增加序号，避免静默覆盖。
            title = str(meeting.get("meetingName") or meeting.get("fileName") or f"meeting-{index}")
            safe_title = title.replace("/", "_").replace("\\", "_").strip() or f"meeting-{index}"
            member_name = f"{safe_title}-{req.exportKind}.docx"
            if member_name in used_names:
                member_name = f"{safe_title}-{index}-{req.exportKind}.docx"
            used_names.add(member_name)
            archive.writestr(member_name, data)
    return Response(
        content=archive_buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=meeting-exports.zip"},
    )


@app.post("/api/meetings/{meeting_id}/exports/docx")
def export_docx(meeting_id: str, req: ExportRequest) -> Response:
    """导出会议文稿、摘要、翻译、重点标记或纪要 docx。"""
    meeting = store.get_or_create_meeting(meeting_id)
    meeting, rules, _rule_version = _frozen_sensitive_policy_for_meeting(meeting)
    data, policy_audit = build_meeting_docx(
        meeting,
        req.exportKind,
        sensitive_rules=rules,
        include_policy_audit=True,
    )
    _record_export_policy_audit(meeting_id, f"docx:{req.exportKind}", policy_audit)
    filename = f"{meeting.get('meetingName', 'meeting')}-{req.exportKind}.docx"
    # HTTP 响应头只能安全承载 ASCII 字节；会议名经常是中文，直接放进 header 会触发
    # Starlette 的 latin-1 编码错误。这里按 RFC 5987 写入 UTF-8 百分号编码文件名，
    # 浏览器下载时仍会显示中文，单元测试直接调用路由函数也不会因为 header 编码失败。
    encoded_filename = quote(filename, safe="")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "X-Sensitive-Policy-Version": policy_audit["ruleVersion"],
        },
    )


@app.post("/api/meetings/{meeting_id}/exports/text")
def export_text(meeting_id: str, req: ExportRequest) -> Response:
    """Export plain text through the same frozen export target as the DOCX route."""

    meeting = store.get_or_create_meeting(meeting_id)
    meeting, rules, _rule_version = _frozen_sensitive_policy_for_meeting(meeting)
    text, policy_audit = build_meeting_text(
        meeting,
        req.exportKind,
        sensitive_rules=rules,
        include_policy_audit=True,
    )
    _record_export_policy_audit(meeting_id, f"text:{req.exportKind}", policy_audit)
    filename = quote(f"{meeting.get('meetingName', 'meeting')}-{req.exportKind}.txt", safe="")
    return Response(
        content=text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Sensitive-Policy-Version": policy_audit["ruleVersion"],
        },
    )


@app.post("/api/meetings/{meeting_id}/exports/audio")
def export_audio(meeting_id: str) -> Response:
    """导出会议音频。

    返回会议第一个上传文件，并保留真实容器和 MIME；未来若接入 ffmpeg MP3 转码，
    必须同时替换字节、扩展名和响应类型，不能只改文件名。
    """
    try:
        meeting = store.get_meeting(meeting_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会议不存在") from None
    if not meeting.get("files"):
        raise HTTPException(status_code=404, detail="暂无可导出的音频文件")
    data, filename, media_type = read_audio_for_playback(meeting["files"][0])
    # 与 docx 导出保持一致，音频原文件名可能包含中文或空格，必须先编码再写响应头。
    encoded_filename = quote(filename, safe="")
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )
