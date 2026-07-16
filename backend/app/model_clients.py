"""本地小模型服务 HTTP 客户端。

智能会议后端不直接 import FunASR、ModelScope、3D-Speaker 或
Qwen3-ForcedAligner 的 Python 包，而是通过本文件调用独立模型服务。
这样可以把 GPU 对齐服务、CPU VAD、CPU 声纹服务分别部署在不同进程或机器上，
业务 API、前端页面和数据库结构都不需要跟着变化。
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Any, Callable


Urlopen = Callable[..., Any]


class LocalModelServiceError(RuntimeError):
    """本地模型服务调用失败。

    前端任务流水线可以把该异常转成 failed/waiting_model_config 状态，明确告诉
    部署人员是哪一个小模型服务没有启动或返回异常。
    """


class _BaseLocalModelClient:
    """本地模型 HTTP 客户端基类。"""

    def __init__(self, base_url: str, urlopen: Urlopen = urllib.request.urlopen):
        self.base_url = (base_url or "").rstrip("/")
        self.urlopen = urlopen

    def health(self, timeout: int = 3) -> dict[str, Any]:
        """Return the model-service health payload with a deliberately short bounded probe.

        A configured base URL only says where a capability might live. Product readiness must be
        based on a real request, and this endpoint is refreshed by the user interface, so the
        timeout stays much shorter than inference requests.
        """

        if not self.base_url:
            raise LocalModelServiceError("local model service URL is not configured")
        request = urllib.request.Request(f"{self.base_url}/v1/health", method="GET")
        try:
            with self.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            payload = json.loads(body or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise LocalModelServiceError(f"model service health HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LocalModelServiceError(f"model service health is unreachable: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise LocalModelServiceError(f"model service health returned non-JSON content: {exc}") from exc
        if not isinstance(payload, dict):
            raise LocalModelServiceError("model service health returned a non-object payload")
        return payload

    def openapi_paths(self, timeout: int = 3) -> set[str]:
        """读取服务实际暴露的 OpenAPI 路径，用于识别未重启的旧模型进程。

        health 中“模型已加载”只证明权重可用，不能证明当前监听端口的进程已经包含新增路由。
        读取 OpenAPI 不触发推理，开销很小，却能准确发现 `/v1/speakers/embedding` 为 404 的
        旧 8100 进程，避免业务后端把说话人能力错误标记为 ready。
        """

        if not self.base_url:
            raise LocalModelServiceError("local model service URL is not configured")
        request = urllib.request.Request(f"{self.base_url}/openapi.json", method="GET")
        try:
            with self.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            payload = json.loads(body or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise LocalModelServiceError(f"model service OpenAPI HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LocalModelServiceError(f"model service OpenAPI is unreachable: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise LocalModelServiceError(f"model service OpenAPI returned non-JSON content: {exc}") from exc
        paths = payload.get("paths") if isinstance(payload, dict) else None
        if not isinstance(paths, dict):
            raise LocalModelServiceError("model service OpenAPI response has no paths object")
        return {str(path) for path in paths}

    def _post_json(self, path: str, payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        """向模型服务发送 JSON 请求。

        本系统后续会把 VAD、声纹、说话人分离、强制对齐分别部署到算力服务器上，
        业务后端只通过 HTTP 调它们。这里必须把网络错误、HTTP 4xx/5xx、JSON 解析错误
        都统一包装成 `LocalModelServiceError`，这样上层接口才能根据当前模式决定：
        - 开发/mock 模式：自动降级到本地 mock 或轻量算法，保证前端流程不断。
        - 生产/真实模型模式：把任务状态标记为 failed 或 waiting_model_config，明确提示部署人员。
        """

        if not self.base_url:
            raise LocalModelServiceError("未配置本地模型服务地址")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            return json.loads(body or "{}")
        except urllib.error.HTTPError as exc:
            # 例如 ForcedAligner 还没配置真实 GPU 服务时，本地模型服务会返回 503。
            # 这里读取响应体可让后端日志/前端任务提示更清楚，而不是只看到 HTTP 503。
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise LocalModelServiceError(f"模型服务 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LocalModelServiceError(f"模型服务不可达：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise LocalModelServiceError(f"模型服务返回非 JSON 内容：{exc}") from exc


class LocalVadClient(_BaseLocalModelClient):
    """FSMN-VAD 服务客户端。

    约定模型服务暴露 `POST /v1/vad/split`，输入音频文件路径或 URL，输出
    `segments: [{start_ms, end_ms}]`。实时会议中也可以把临时分片文件送入该接口。
    """

    def split(
        self,
        audio_path: str,
        min_speech_ms: int = 200,
        max_segment_ms: int = 30000,
    ) -> dict[str, Any]:
        return self._post_json(
            "/v1/vad/split",
            {
                "audio_path": audio_path,
                "min_speech_ms": min_speech_ms,
                "max_segment_ms": max_segment_ms,
            },
        )


class LocalVoiceprintClient(_BaseLocalModelClient):
    """CAM++ / 3D-Speaker 声纹服务客户端。"""

    def embedding(self, audio_path: str) -> dict[str, Any]:
        """提取会议片段的 CAM++ embedding，结果仅供后端会话内 tracker 使用。

        该方法故意只接受音频路径，不接受会议字典或前端 payload，缩小敏感向量被混入
        业务响应/持久化结构的机会。调用方应在完成会议内聚类后立即丢弃返回的向量。
        """

        payload = self._post_json(
            "/v1/speakers/embedding",
            {"audio_path": audio_path},
        )
        # `_post_json` 负责传输和 JSON 解析，但 JSON 顶层仍可能是数组，embedding 也可能
        # 缺失、为空或混入字符串。必须在模型服务边界拒绝这些形态，避免无效向量进入
        # meeting-scoped tracker 后被误当作稳定身份依据。
        if not isinstance(payload, dict):
            raise LocalModelServiceError("speaker embedding response must be a JSON object")
        embedding = payload.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise LocalModelServiceError("speaker embedding must be a non-empty numeric list")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in embedding
        ):
            raise LocalModelServiceError("speaker embedding must be a non-empty numeric list")
        return payload

    def register(
        self,
        speaker_id: str,
        speaker_name: str,
        audio_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """注册声纹样本，返回 embedding/model 状态。"""

        return self._post_json(
            "/v1/voiceprints/register",
            {
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "audio_path": audio_path,
                "metadata": metadata or {},
            },
        )

    def match(self, audio_path: str, top_k: int = 1) -> dict[str, Any]:
        """匹配最相似的已登记发言人。"""

        return self._post_json(
            "/v1/voiceprints/match",
            {"audio_path": audio_path, "top_k": top_k},
        )

    def diarize(self, audio_path: str, min_speakers: int | None = None, max_speakers: int | None = None) -> dict[str, Any]:
        """多人会议说话人分离。

        CAM++ 主要承担注册和匹配，若接入 3D-Speaker diarization，可在服务端根据
        该接口参数决定使用哪个模型或流水线。
        """

        return self._post_json(
            "/v1/diarize",
            {
                "audio_path": audio_path,
                "min_speakers": min_speakers,
                "max_speakers": max_speakers,
            },
            timeout=300,
        )


class LocalAlignmentClient(_BaseLocalModelClient):
    """Qwen3-ForcedAligner-0.6B 强制对齐服务客户端。"""

    def align(
        self,
        audio_path: str,
        transcript_text: str,
        language: str = "zh",
    ) -> dict[str, Any]:
        return self._post_json(
            "/v1/align",
            {
                "audio_path": audio_path,
                "transcript_text": transcript_text,
                "language": language,
            },
            timeout=300,
        )

    def selection_window(
        self,
        audio_path: str,
        transcript_text: str,
        selected_text: str,
        padding_ms: int = 500,
    ) -> dict[str, Any]:
        """由对齐服务直接返回选中文本对应的音频窗口。"""

        return self._post_json(
            "/v1/align/selection-window",
            {
                "audio_path": audio_path,
                "transcript_text": transcript_text,
                "selected_text": selected_text,
                "padding_ms": padding_ms,
            },
            timeout=300,
        )
