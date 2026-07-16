"""智能会议本地/算力服务器小模型服务。

该服务独立于主 FastAPI 后端运行，专门承载 CPU/GPU 小模型：
- FSMN-VAD：语音活动检测和长音频切分。
- CAM++：声纹注册、声纹身份匹配。
- 3D-Speaker：多人说话人分离增强。
- Qwen3-ForcedAligner-0.6B：字/词级强制对齐。

设计原则：
1. 主业务后端只通过 HTTP 调用本服务，不直接 import 模型库，方便后续把模型迁到算力服务器。
2. `LOCAL_MODEL_MOCK_MODE=true` 时返回稳定 mock 数据，保证无模型权重也能联调全流程。
3. `LOCAL_MODEL_MOCK_MODE=false` 时优先调用真实模型；模型未配置时返回明确 503，不伪装成功。
4. 强制对齐模型通常部署在 GPU 服务器上，因此本服务支持 `FORCED_ALIGNER_BACKEND_URL`
   转发到真正的 Qwen3-ForcedAligner 服务；如果主后端直接配置 `ALIGNMENT_GATEWAY_BASE_URL`
   指向 GPU 服务，也可以绕过本服务。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import time
import urllib.request
import wave
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.alignment_service import find_audio_window_for_selection


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_backend_env() -> None:
    """加载 backend/.env 中的小模型服务配置。

    模型服务独立于主 FastAPI 后端启动，不会自动执行 `app.config`。
    如果部署人员把 `QWEN_FORCED_ALIGNER_MODEL_ID`、`DIARIZATION_BACKEND_URL`
    这类变量写进 backend/.env，旧版本 8100 服务是读不到的。这里补上同样的轻量加载逻辑：
    只填充当前进程未设置的变量，不覆盖系统环境变量或容器 Secret。
    """

    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_backend_env()


LOCAL_MODEL_MOCK_MODE = os.getenv("LOCAL_MODEL_MOCK_MODE", "true").lower() in {"1", "true", "yes", "on"}
MODEL_SERVICE_DATA_DIR = Path(os.getenv("MODEL_SERVICE_DATA_DIR", Path(__file__).resolve().parent.parent / "data" / "model_service"))
VOICEPRINT_DB_PATH = MODEL_SERVICE_DATA_DIR / "voiceprint_embeddings.json"

# 模型 ID 全部通过环境变量暴露。Windows 本机可用短名先跑通；Linux/算力服务器建议填写 ModelScope 完整 ID。
FSMN_VAD_MODEL_ID = os.getenv("FSMN_VAD_MODEL_ID", "fsmn-vad")
CAMPP_MODEL_ID = os.getenv("CAMPP_MODEL_ID", "iic/speech_campplus_sv_zh-cn_16k-common")
DIARIZATION_MODEL_ID = os.getenv("DIARIZATION_MODEL_ID", "iic/speech_campplus_speaker-diarization_common")
MODEL_DEVICE = os.getenv("MODEL_DEVICE", "cpu")
FORCED_ALIGNER_BACKEND_URL = os.getenv("FORCED_ALIGNER_BACKEND_URL", "").rstrip("/")
MODEL_SERVICE_IDENTITY = "intelligent-meeting-local-model-service"
# Qwen3-ForcedAligner-0.6B 通常应部署在 GPU 服务器上。本地 Windows/CPU 开发机默认不自动下载
# 0.6B 权重，避免首次选区回听时意外下载大模型。服务器部署时设置：
#   QWEN_FORCED_ALIGNER_MODEL_ID=Qwen/Qwen3-ForcedAligner-0.6B
#   QWEN_FORCED_ALIGNER_DEVICE=cuda:0
# 即可让本服务直接加载真实对齐模型；不设置时仍可通过 FORCED_ALIGNER_BACKEND_URL 代理到独立 GPU 服务。
QWEN_FORCED_ALIGNER_MODEL_ID = os.getenv("QWEN_FORCED_ALIGNER_MODEL_ID", "")
QWEN_FORCED_ALIGNER_DEVICE = os.getenv("QWEN_FORCED_ALIGNER_DEVICE", MODEL_DEVICE)
# 3D-Speaker 在正式部署时建议单独跑在算力服务器或 CPU 推理节点上。
# 如果配置该地址，本服务会直接把 `/v1/diarize` 请求转发过去；未配置时才在本进程内加载 ModelScope pipeline。
DIARIZATION_BACKEND_URL = os.getenv("DIARIZATION_BACKEND_URL", "").rstrip("/")

app = FastAPI(title="智能会议小模型服务", version="1.1.0")

_vad_model: Any | None = None
_speaker_model: Any | None = None
_diarization_model: Any | None = None
_forced_aligner_model: Any | None = None


class VadSplitRequest(BaseModel):
    """VAD 切分请求。"""

    audio_path: str
    min_speech_ms: int = 200
    max_segment_ms: int = 30000


class VoiceprintRegisterRequest(BaseModel):
    """声纹注册请求。"""

    speaker_id: str
    speaker_name: str
    audio_path: str
    metadata: dict[str, Any] = {}


class VoiceprintMatchRequest(BaseModel):
    """声纹匹配请求。"""

    audio_path: str
    top_k: int = 1


class SpeakerEmbeddingRequest(BaseModel):
    """仅供业务后端调用的实时片段 embedding 请求。"""

    audio_path: str


class DiarizeRequest(BaseModel):
    """多人说话人分离请求。"""

    audio_path: str
    min_speakers: int | None = None
    max_speakers: int | None = None


class AlignRequest(BaseModel):
    """强制对齐请求。"""

    audio_path: str
    transcript_text: str
    language: str = "zh"


class SelectionWindowRequest(BaseModel):
    """文本选区反查音频窗口请求。"""

    audio_path: str
    transcript_text: str
    selected_text: str
    padding_ms: int = 500


def _ensure_data_dir() -> None:
    """确保模型服务持久化目录存在。

    CAM++ 注册后的 embedding 先存成本地 JSON，便于本地测试；正式部署可替换为 KingbaseES、
    Milvus、Faiss 或其它向量检索服务，但外部 HTTP 接口不用改变。
    """

    MODEL_SERVICE_DATA_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_audio_exists(audio_path: str) -> None:
    """真实模型模式下检查音频路径是否存在。

    跨机器部署时推荐传模型服务器本机可访问的路径，或把主后端改为上传文件/对象存储 URL。
    当前接口先按本机路径处理，保持实现简单明确。
    """

    if not LOCAL_MODEL_MOCK_MODE and not Path(audio_path).exists():
        raise HTTPException(status_code=404, detail=f"音频文件不存在：{audio_path}")


def _lazy_import_automodel():
    """延迟导入 FunASR AutoModel。

    延迟导入可以让 mock 模式启动更快，也避免在未安装模型依赖时影响主后端联调。
    """

    try:
        from funasr import AutoModel  # type: ignore

        return AutoModel
    except Exception as exc:  # noqa: BLE001 - 要把底层依赖错误明确暴露给部署人员
        raise HTTPException(
            status_code=503,
            detail=f"未能导入 FunASR，请确认已安装 funasr/modelscope/torch：{exc}",
        ) from exc


def _get_vad_model():
    """加载并缓存 FSMN-VAD 模型。"""

    global _vad_model
    if _vad_model is None:
        AutoModel = _lazy_import_automodel()
        # disable_update=True 避免启动时反复检查远端更新；模型不存在时仍会按 FunASR/ModelScope 规则下载。
        _vad_model = AutoModel(model=FSMN_VAD_MODEL_ID, device=MODEL_DEVICE, disable_update=True)
    return _vad_model


def _get_speaker_model():
    """加载并缓存 CAM++ 声纹模型。"""

    global _speaker_model
    if _speaker_model is None:
        AutoModel = _lazy_import_automodel()
        _speaker_model = AutoModel(model=CAMPP_MODEL_ID, device=MODEL_DEVICE, disable_update=True)
    return _speaker_model


def _get_diarization_model():
    """加载并缓存 3D-Speaker 说话人日志 pipeline。

    3D-Speaker 的 `speech_campplus_speaker-diarization_common` 不是 FunASR AutoModel 的注册模型，
    正确加载方式是 ModelScope `pipeline(task="speaker-diarization")`。这里独立封装，避免业务接口
    关心底层模型框架；后续迁移到算力服务器时，也只需要配置 `DIARIZATION_BACKEND_URL` 绕过本地加载。
    """

    global _diarization_model
    if _diarization_model is None:
        from modelscope.pipelines import pipeline  # type: ignore

        _diarization_model = pipeline(task="speaker-diarization", model=DIARIZATION_MODEL_ID)
    return _diarization_model


def _get_forced_aligner_model():
    """加载并缓存 Qwen3-ForcedAligner-0.6B。

    该模型属于 GPU 侧能力，本地开发机一般不加载；只有显式配置
    `QWEN_FORCED_ALIGNER_MODEL_ID` 时才会进入这里。这样既满足服务器真实部署，
    又不会让普通页面操作在无 GPU 环境下卡死。
    """

    global _forced_aligner_model
    if not QWEN_FORCED_ALIGNER_MODEL_ID:
        raise HTTPException(
            status_code=503,
            detail=(
                "未配置 QWEN_FORCED_ALIGNER_MODEL_ID。GPU 服务器请设置为 "
                "Qwen/Qwen3-ForcedAligner-0.6B，或配置 FORCED_ALIGNER_BACKEND_URL 指向独立对齐服务。"
            ),
        )
    if _forced_aligner_model is None:
        import torch  # type: ignore
        from qwen_asr.inference.qwen3_forced_aligner import Qwen3ForcedAligner  # type: ignore

        load_kwargs: dict[str, Any] = {}
        if QWEN_FORCED_ALIGNER_DEVICE.startswith("cuda"):
            # 910B 以外的 GPU 测试环境常用 bfloat16；如果未来迁移到昇腾推理服务，
            # 建议把 Qwen3-ForcedAligner 独立封装成远程 HTTP 服务，再由本接口代理。
            load_kwargs["torch_dtype"] = torch.bfloat16
            load_kwargs["device_map"] = QWEN_FORCED_ALIGNER_DEVICE
        _forced_aligner_model = Qwen3ForcedAligner.from_pretrained(
            QWEN_FORCED_ALIGNER_MODEL_ID,
            **load_kwargs,
        )
    return _forced_aligner_model


def _normalize_aligner_language(language: str) -> str:
    """把业务侧语言短名转换为 Qwen3-ForcedAligner 支持的语言名。"""

    normalized = (language or "zh").lower()
    if normalized in {"zh", "cn", "chinese", "中文", "中文普通话"}:
        return "Chinese"
    if normalized in {"en", "english", "英文"}:
        return "English"
    if normalized in {"ja", "japanese", "日文"}:
        return "Japanese"
    if normalized in {"ko", "korean", "韩文"}:
        return "Korean"
    return language or "Chinese"


def _align_with_local_qwen(req: AlignRequest) -> dict[str, Any]:
    """调用本进程加载的 Qwen3-ForcedAligner-0.6B 并转换为统一时间戳结构。"""

    model = _get_forced_aligner_model()
    language = _normalize_aligner_language(req.language)
    results = model.align(req.audio_path, req.transcript_text, language)
    items = list(results[0]) if results else []
    words = [
        {
            "text": item.text,
            # qwen-asr wrapper 返回秒，这里统一转换成毫秒，和前端字音回听字段保持一致。
            "start_ms": int(float(item.start_time) * 1000),
            "end_ms": int(float(item.end_time) * 1000),
        }
        for item in items
    ]
    return {
        "model": QWEN_FORCED_ALIGNER_MODEL_ID,
        "audioPath": req.audio_path,
        "language": language,
        "words": words,
    }


def _normalize_diarization_result(result: Any) -> list[dict[str, Any]]:
    """把 3D-Speaker 输出规整成前后端统一可消费的片段格式。

    ModelScope pipeline 常见输出为 `{"text": [[start_sec, end_sec, speaker_id], ...]}`，其中数值
    可能是 numpy 标量。直接 JSON 序列化 numpy 标量会失败，所以这里统一转为 Python 基础类型。
    """

    raw_segments = result.get("text", []) if isinstance(result, dict) else result
    segments: list[dict[str, Any]] = []
    for index, item in enumerate(raw_segments or []):
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        start_sec = float(item[0])
        end_sec = float(item[1])
        speaker_index = int(item[2])
        segments.append(
            {
                "id": f"diar-{index + 1}",
                "speaker": f"SPEAKER_{speaker_index:02d}",
                "start_ms": int(round(start_sec * 1000)),
                "end_ms": int(round(end_sec * 1000)),
            }
        )
    return segments


def _post_json(url: str, payload: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    """向外部 GPU/算力服务器模型服务转发 JSON 请求。"""

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _mock_word_timestamps(text: str, start_ms: int = 0, step_ms: int = 240) -> list[dict[str, Any]]:
    """生成稳定字级时间戳，供 mock 字音回听和选区截音频使用。"""

    cursor = start_ms
    words: list[dict[str, Any]] = []
    for char in text:
        if char.isspace():
            continue
        words.append({"text": char, "start_ms": cursor, "end_ms": cursor + step_ms})
        cursor += step_ms
    return words


def _normalize_vad_result(result: Any, max_segment_ms: int) -> list[dict[str, Any]]:
    """把 FunASR VAD 输出规整成系统统一格式。

    FunASR 常见输出为 `[{"value": [[start, end], ...]}]`，不同版本字段可能略有差异，
    因此这里做了多种形态兼容。
    """

    if isinstance(result, dict):
        raw_segments = result.get("value") or result.get("segments") or []
    elif isinstance(result, list) and result and isinstance(result[0], dict):
        raw_segments = result[0].get("value") or result[0].get("segments") or []
    else:
        raw_segments = result if isinstance(result, list) else []

    segments: list[dict[str, Any]] = []
    for item in raw_segments:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            start_ms, end_ms = int(item[0]), int(item[1])
        elif isinstance(item, dict):
            start_ms = int(item.get("start") or item.get("start_ms") or item.get("startMs") or 0)
            end_ms = int(item.get("end") or item.get("end_ms") or item.get("endMs") or 0)
        else:
            continue
        if end_ms <= start_ms:
            continue
        # 过长语音段按 max_segment_ms 再切一次，避免 ASR 同步接口单段过长。
        cursor = start_ms
        while cursor < end_ms:
            part_end = min(end_ms, cursor + max_segment_ms)
            segments.append({"start_ms": cursor, "end_ms": part_end, "speech": True})
            cursor = part_end
    return segments


def _extract_embedding(result: Any, audio_path: str) -> list[float]:
    """从 CAM++ 输出中提取 embedding。

    不同模型封装返回字段并不完全一致，常见字段包括 `embedding`、`spk_embedding`、
    `value`。如果当前模型版本没有直接返回向量，则抛出 503，提醒部署人员换成明确
    支持 embedding 输出的声纹服务；不要用随机向量伪装真实声纹。
    """

    candidates: list[Any] = []
    if isinstance(result, dict):
        candidates.extend([result.get("embedding"), result.get("spk_embedding"), result.get("value")])
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                candidates.extend([item.get("embedding"), item.get("spk_embedding"), item.get("value")])
            else:
                candidates.append(item)

    for candidate in candidates:
        if candidate is None:
            continue
        array = np.asarray(candidate, dtype=np.float32).reshape(-1)
        if array.size >= 16:
            norm = float(np.linalg.norm(array))
            if norm > 0:
                return (array / norm).astype(float).tolist()

    raise HTTPException(
        status_code=503,
        detail=(
            "CAM++ 模型已调用，但未返回可用 embedding。请在算力服务器上把声纹服务封装为"
            " /v1/voiceprints/register 返回 embeddingId，或调整 CAMPP_MODEL_ID 为支持向量输出的模型。"
        ),
    )


def _load_voiceprint_db() -> dict[str, Any]:
    """读取本地声纹向量库。"""

    _ensure_data_dir()
    if not VOICEPRINT_DB_PATH.exists():
        return {"items": {}}
    return json.loads(VOICEPRINT_DB_PATH.read_text(encoding="utf-8"))


def _save_voiceprint_db(db: dict[str, Any]) -> None:
    """保存本地声纹向量库。"""

    _ensure_data_dir()
    VOICEPRINT_DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def _speaker_embedding(audio_path: str) -> list[float]:
    """调用真实 CAM++ 并返回归一化向量，任何失败都保持 fail closed。"""

    try:
        model = _get_speaker_model()
        result = model.generate(input=audio_path)
        return _extract_embedding(result, audio_path)
    except HTTPException:
        # Preserve the detailed 503 used when CAM++ returns an unsupported result shape.
        raise
    except Exception as exc:  # noqa: BLE001 - dependency, weight, and inference failures share one API contract.
        # Real mode fails closed: a deterministic audio fingerprint is not a CAM++ embedding and
        # must never be persisted or returned as a registration/match result.
        raise HTTPException(status_code=503, detail=f"CAM++ is unavailable: {exc}") from exc


def _cosine(left: list[float], right: list[float]) -> float:
    """计算两个声纹 embedding 的余弦相似度。"""

    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _health_capability(name: str, model: str, probe: Any) -> dict[str, Any]:
    """Probe a lazy model so health reflects usable dependencies and weights."""

    checked_at = int(time.time())
    if LOCAL_MODEL_MOCK_MODE:
        return {"ready": True, "state": "mock", "mode": "mock", "model": model, "message": f"{name} mock diagnostics are enabled", "checkedAt": checked_at}
    try:
        probe()
    except Exception as exc:  # noqa: BLE001 - health must remain JSON even when a model cannot load.
        return {"ready": False, "state": "unavailable", "mode": "real", "model": model, "message": f"{name} probe failed: {exc}", "checkedAt": checked_at}
    return {"ready": True, "state": "ready", "mode": "real", "model": model, "message": f"{name} probe succeeded", "checkedAt": checked_at}


def _probe_alignment_health() -> None:
    """Probe the configured alignment implementation instead of treating configuration as readiness."""

    if FORCED_ALIGNER_BACKEND_URL:
        request = urllib.request.Request(f"{FORCED_ALIGNER_BACKEND_URL}/v1/health", method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise RuntimeError(f"remote alignment health returned {payload!r}")
        return
    if QWEN_FORCED_ALIGNER_MODEL_ID:
        _get_forced_aligner_model()
        return
    raise RuntimeError("Qwen forced aligner is not configured")


@app.get("/v1/health")
def health() -> dict[str, Any]:
    """Return stable service identity and independently probed capability readiness."""

    voiceprint_capability = _health_capability("voiceprint", CAMPP_MODEL_ID, _get_speaker_model)
    # 实时说话人跟踪不是调用注册接口，而是直接依赖 /v1/speakers/embedding。把该能力写进
    # 健康契约后，启动脚本能区分“旧进程只会注册/匹配”和“当前进程可提供逐段 embedding”。
    # 路由随当前应用版本静态存在，因此真正决定可调用性的仍是 CAM++ 探针是否 ready；mock
    # 模式会保留自身 mode 标识，由业务后端继续拒绝把 mock 能力宣传为真实声纹能力。
    voiceprint_capability["embeddingReady"] = voiceprint_capability.get("ready") is True

    return {
        # Callers must verify this identity before reusing a port or advertising readiness. Another
        # HTTP process returning status=ok cannot safely stand in for this model service.
        "service": MODEL_SERVICE_IDENTITY,
        "status": "ok",
        "mockMode": LOCAL_MODEL_MOCK_MODE,
        "device": MODEL_DEVICE,
        "capabilities": {
            "vad": _health_capability("vad", FSMN_VAD_MODEL_ID, _get_vad_model),
            "voiceprint": voiceprint_capability,
            "alignment": _health_capability("alignment", FORCED_ALIGNER_BACKEND_URL or QWEN_FORCED_ALIGNER_MODEL_ID or "unconfigured", _probe_alignment_health),
        },
        "models": {
            "vad": FSMN_VAD_MODEL_ID,
            "voiceprint": CAMPP_MODEL_ID,
            "diarization": DIARIZATION_MODEL_ID,
            "alignmentProxy": FORCED_ALIGNER_BACKEND_URL,
            "forcedAligner": QWEN_FORCED_ALIGNER_MODEL_ID,
            "forcedAlignerDevice": QWEN_FORCED_ALIGNER_DEVICE,
            "diarizationProxy": DIARIZATION_BACKEND_URL,
        },
    }


@app.post("/v1/vad/split")
def split_vad(req: VadSplitRequest) -> dict[str, Any]:
    """FSMN-VAD 切分接口。"""

    _ensure_audio_exists(req.audio_path)
    if LOCAL_MODEL_MOCK_MODE:
        return {
            "model": "FSMN-VAD",
            "audioPath": req.audio_path,
            "segments": [
                {"start_ms": 0, "end_ms": min(req.max_segment_ms, 15000), "speech": True},
                {"start_ms": 16000, "end_ms": min(req.max_segment_ms + 16000, 31000), "speech": True},
            ],
            "createdAt": int(time.time()),
        }

    model = _get_vad_model()
    result = model.generate(input=req.audio_path)
    return {
        "model": FSMN_VAD_MODEL_ID,
        "audioPath": req.audio_path,
        "segments": _normalize_vad_result(result, req.max_segment_ms),
        "raw": result,
        "createdAt": int(time.time()),
    }


@app.post("/v1/voiceprints/register")
def register_voiceprint(req: VoiceprintRegisterRequest) -> dict[str, Any]:
    """CAM++ 声纹注册接口。"""

    _ensure_audio_exists(req.audio_path)
    if LOCAL_MODEL_MOCK_MODE:
        seed = f"{req.speaker_id}|{req.speaker_name}|{req.audio_path}"
        return {
            "model": "CAM++",
            "speakerId": req.speaker_id,
            "speakerName": req.speaker_name,
            "embeddingId": hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16],
            "status": "registered",
            "confidence": 1.0,
            # Mock output is deliberately marked so the business layer cannot enroll it as real.
            "realModel": False,
            "mockMode": True,
            "fallbackReason": "LOCAL_MODEL_MOCK_MODE is enabled",
        }

    embedding = _speaker_embedding(req.audio_path)
    embedding_id = hashlib.sha1(f"{req.speaker_id}|{time.time()}".encode("utf-8")).hexdigest()[:16]

    db = _load_voiceprint_db()
    db.setdefault("items", {})[embedding_id] = {
        "speakerId": req.speaker_id,
        "speakerName": req.speaker_name,
        "audioPath": req.audio_path,
        "embedding": embedding,
        "metadata": req.metadata,
        "registeredAt": int(time.time()),
        "model": CAMPP_MODEL_ID,
        "realModel": True,
        "fallbackReason": "",
    }
    _save_voiceprint_db(db)
    return {
        "model": CAMPP_MODEL_ID,
        "speakerId": req.speaker_id,
        "speakerName": req.speaker_name,
        "embeddingId": embedding_id,
        "status": "registered",
        "confidence": 1.0,
        "realModel": True,
        "fallbackReason": "",
    }


@app.post("/v1/speakers/embedding")
def speaker_embedding(req: SpeakerEmbeddingRequest) -> dict[str, Any]:
    """返回供会议内存聚类使用的 CAM++ embedding。

    Mock 模式使用音频路径的 SHA-256 摘要生成确定性单位向量，使相同测试片段可以稳定
    复现聚类行为；``realModel=False`` 和明确的 fallbackReason 防止调用方把它当作真实声纹。
    真实模式不提供任何启发式降级，CAM++ 加载、推理或输出解析失败都由
    ``_speaker_embedding`` 以 HTTP 503 暴露。
    """

    _ensure_audio_exists(req.audio_path)
    if LOCAL_MODEL_MOCK_MODE:
        digest = hashlib.sha256(req.audio_path.encode("utf-8")).digest()
        raw_vector = [(byte / 127.5) - 1.0 for byte in digest]
        norm = math.sqrt(sum(value * value for value in raw_vector))
        # SHA-256 摘要不可能让全部映射值同时为零；保留显式保护以避免未来改动除零。
        embedding = [value / norm for value in raw_vector] if norm > 0 else [1.0] + [0.0] * 31
        return {
            "model": "CAM++",
            "embedding": embedding,
            "realModel": False,
            "fallbackReason": "deterministic mock embedding: LOCAL_MODEL_MOCK_MODE is enabled",
        }

    return {
        "model": CAMPP_MODEL_ID,
        "embedding": _speaker_embedding(req.audio_path),
        "realModel": True,
        "fallbackReason": "",
    }


@app.post("/v1/voiceprints/match")
def match_voiceprint(req: VoiceprintMatchRequest) -> dict[str, Any]:
    """CAM++ 声纹匹配接口。"""

    _ensure_audio_exists(req.audio_path)
    if LOCAL_MODEL_MOCK_MODE:
        digest = hashlib.sha1(req.audio_path.encode("utf-8")).digest()[0]
        names = ["张三", "李四", "王五"]
        name = names[digest % len(names)]
        return {
            "model": "CAM++",
            "speakerId": f"mock-{digest % len(names)}",
            "speakerName": name,
            "confidence": 0.86,
            "matches": [{"speakerName": name, "confidence": 0.86}],
            "realModel": False,
            "mockMode": True,
            "fallbackReason": "LOCAL_MODEL_MOCK_MODE is enabled",
        }

    embedding = _speaker_embedding(req.audio_path)
    db = _load_voiceprint_db()
    matches = []
    for embedding_id, item in db.get("items", {}).items():
        score = _cosine(embedding, item.get("embedding", []))
        matches.append(
            {
                "embeddingId": embedding_id,
                "speakerId": item.get("speakerId", ""),
                "speakerName": item.get("speakerName", ""),
                "confidence": round(score, 4),
            }
        )
    matches.sort(key=lambda item: item["confidence"], reverse=True)
    top = matches[: max(1, req.top_k)]
    best = top[0] if top else {"speakerId": "", "speakerName": "未匹配", "confidence": 0.0}
    return {
        "model": CAMPP_MODEL_ID,
        "speakerId": best.get("speakerId", ""),
        "speakerName": best.get("speakerName", "未匹配"),
        "confidence": best.get("confidence", 0.0),
        "matches": top,
        "realModel": True,
        "fallbackReason": "",
    }


@app.post("/v1/diarize")
def diarize(req: DiarizeRequest) -> dict[str, Any]:
    """3D-Speaker / diarization 说话人分离接口。"""

    _ensure_audio_exists(req.audio_path)
    if LOCAL_MODEL_MOCK_MODE:
        return {
            "model": "3D-Speaker",
            "segments": [
                {"speaker": "SPEAKER_00", "start_ms": 0, "end_ms": 8000},
                {"speaker": "SPEAKER_01", "start_ms": 8200, "end_ms": 16000},
            ],
        }

    if DIARIZATION_BACKEND_URL:
        # 算力服务器部署 3D-Speaker 后，主机只负责协议转发，避免 Web 后端和模型进程抢资源。
        return _post_json(f"{DIARIZATION_BACKEND_URL}/v1/diarize", req.model_dump(), timeout=600)

    model = _get_diarization_model()
    # 如果前端/业务侧已知说话人数，可通过 min_speakers/max_speakers 传入；二者一致时使用最准确的 oracle_num。
    # 未知人数时不传 oracle_num，让 3D-Speaker 基于聚类自动估计。
    kwargs: dict[str, Any] = {}
    if req.min_speakers and req.max_speakers and req.min_speakers == req.max_speakers:
        kwargs["oracle_num"] = req.min_speakers
    elif req.max_speakers:
        kwargs["oracle_num"] = req.max_speakers

    source_path = Path(req.audio_path)
    model_audio_path = source_path
    normalized_path: Path | None = None
    try:
        # 实时浏览器录音通常已经是 WAV，但导入台账允许 MP3/M4A/MP4 等常见格式。旧实现
        # 无条件 ``wave.open``，MP3 会在模型调用前抛错；业务后端为了保住 ASR 文本而降级，
        # 最终页面只能显示“待匹配发言人”。先尝试无损读取 WAV，非 WAV 或非标准 PCM 统一
        # 交给 ffmpeg 转成 16kHz/单声道/16 位 PCM，保证实时与导入进入同一个模型输入契约。
        requires_ffmpeg = False
        try:
            with wave.open(str(source_path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                sample_rate = wav_file.getframerate()
            requires_ffmpeg = channels != 1 or sample_width != 2 or sample_rate != 16000
        except (wave.Error, EOFError):
            requires_ffmpeg = True

        if requires_ffmpeg:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise HTTPException(status_code=422, detail="说话人分离需要 ffmpeg 将导入音频转换为 16k 单声道 WAV")
            normalized_path = MODEL_SERVICE_DATA_DIR / f"diarization-{uuid.uuid4().hex}.wav"
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            command = [
                ffmpeg,
                "-y",
                "-i",
                str(source_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(normalized_path),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
            except (subprocess.SubprocessError, OSError) as exc:
                # 只报告转换错误，不把命令行中的本地完整目录或 ffmpeg stderr 暴露给前端。
                raise HTTPException(status_code=422, detail=f"导入音频格式转换失败：{type(exc).__name__}") from exc
            model_audio_path = normalized_path

        result = model(str(model_audio_path), **kwargs)
        segments = _normalize_diarization_result(result)
        return {"model": DIARIZATION_MODEL_ID, "segments": segments, "raw": json.loads(json.dumps(result, default=str))}
    finally:
        # 只删除本请求创建且路径明确的单个临时 WAV；原始会议录音始终保留。
        if normalized_path and normalized_path.is_file():
            normalized_path.unlink(missing_ok=True)


@app.post("/v1/align")
def align(req: AlignRequest) -> dict[str, Any]:
    """Qwen3-ForcedAligner-0.6B 强制对齐接口。"""

    _ensure_audio_exists(req.audio_path)
    if FORCED_ALIGNER_BACKEND_URL:
        return _post_json(f"{FORCED_ALIGNER_BACKEND_URL}/v1/align", req.model_dump(), timeout=300)
    if QWEN_FORCED_ALIGNER_MODEL_ID:
        return _align_with_local_qwen(req)
    if LOCAL_MODEL_MOCK_MODE:
        return {
            "model": "Qwen3-ForcedAligner-0.6B",
            "audioPath": req.audio_path,
            "language": req.language,
            "words": _mock_word_timestamps(req.transcript_text),
        }
    raise HTTPException(
        status_code=503,
        detail="未配置 Qwen3-ForcedAligner-0.6B 服务。请设置 FORCED_ALIGNER_BACKEND_URL 或让主后端直接指向 GPU 对齐服务。",
    )


@app.post("/v1/align/selection-window")
def selection_window(req: SelectionWindowRequest) -> dict[str, Any]:
    """选中文本反查音频片段接口。"""

    _ensure_audio_exists(req.audio_path)
    if FORCED_ALIGNER_BACKEND_URL:
        return _post_json(f"{FORCED_ALIGNER_BACKEND_URL}/v1/align/selection-window", req.model_dump(), timeout=300)

    if QWEN_FORCED_ALIGNER_MODEL_ID:
        # 真实对齐服务可直接返回字/词级时间戳，选中文本反查就不再依赖前端传来的粗略 words。
        aligned = _align_with_local_qwen(
            AlignRequest(
                audio_path=req.audio_path,
                transcript_text=req.transcript_text,
                language="zh",
            )
        )
        words = aligned.get("words", [])
    elif LOCAL_MODEL_MOCK_MODE:
        words = _mock_word_timestamps(req.transcript_text)
    else:
        # The proportional timestamp helper is useful only for explicit mock diagnostics. In real
        # mode it is not forced alignment and must never be returned as if a model had validated
        # the selected audio window; keep the capability truth consistent with `/v1/health`.
        raise HTTPException(
            status_code=503,
            detail="未配置可用的强制对齐服务。请设置 FORCED_ALIGNER_BACKEND_URL 或 QWEN_FORCED_ALIGNER_MODEL_ID。",
        )
    try:
        # ForcedAligner 既可能逐字返回，也可能把“智能”“会议”这类短词作为一个 token 返回。
        # 因此不能把拼接文本中的字符下标直接用于切片 words；公共 helper 会累计每个 token 的
        # 字符区间，并按区间交集找到真正覆盖选中文本的 token。先以 0 padding 取原始时间窗，
        # 后面再统一添加请求 padding，确保返回 words 不会因 padding 扩大而混入相邻 token。
        raw_window = find_audio_window_for_selection(
            req.transcript_text,
            req.selected_text,
            words,
            padding_ms=0,
        )
    except ValueError as exc:
        # helper 使用 ValueError 表达选区缺失或时间戳缺失；HTTP 服务边界保持原有 400 语义，
        # 同时透传具体原因，方便调用方区分文本不匹配与对齐结果不完整。
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raw_start_ms = raw_window["start_ms"]
    raw_end_ms = raw_window["end_ms"]
    selected = [
        item
        for item in words
        if int(item["start_ms"]) < raw_end_ms and raw_start_ms < int(item["end_ms"])
    ]
    return {
        "start_ms": max(0, raw_start_ms - req.padding_ms),
        "end_ms": raw_end_ms + req.padding_ms,
        "words": selected,
    }
