"""检查智能会议小模型 HTTP 服务是否可被后端调用。

默认模式只检查 `/v1/health`，适合启动脚本和日常巡检；`--deep` 模式会额外调用
VAD、声纹注册/匹配和文本选区反查接口，适合模型依赖安装后做一次完整联调。
该脚本只使用 Python 标准库，便于在 Windows 本机或内网服务器上直接运行。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any


MODEL_SERVICE_IDENTITY = "intelligent-meeting-local-model-service"


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    """发送 JSON 请求并解析响应。

    小模型服务所有诊断接口都返回 JSON。这里把 HTTP 错误体读出来拼进异常，部署人员能直接看到
    “模型未配置”“音频文件不存在”“依赖未安装”等真实原因，而不是只看到 500/503。
    """

    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc


def _create_tiny_wav() -> Path:
    """生成一个 1 秒静音 wav，用于 deep 检查。

    真实声纹质量需要清晰人声样本；这里的静音文件只用于验证 HTTP 协议、路径访问和服务进程是否通畅。
    如果要验证真实模型效果，请通过 `--audio-path` 传入 15 秒以上的人声 wav。
    """

    path = Path(tempfile.gettempdir()) / "meeting_model_service_check.wav"
    sample_rate = 16000
    frames = b"\x00\x00" * sample_rate
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return path


def _require_real_health(health: dict[str, Any], deep: bool) -> None:
    """Reject mock, look-alike, and unready health states before certifying real inference."""

    if health.get("status") != "ok":
        raise RuntimeError(f"模型服务健康检查失败：{health}")
    if health.get("service") != MODEL_SERVICE_IDENTITY:
        raise RuntimeError(f"模型服务身份不匹配：{health}")
    if health.get("mockMode") is True:
        raise RuntimeError("model service is running in mock mode and cannot certify real deployment")
    capabilities = health.get("capabilities")
    if not isinstance(capabilities, dict):
        raise RuntimeError("real model-service check requires capability health")
    for name in ("vad", "voiceprint", "alignment"):
        capability = capabilities.get(name)
        if not isinstance(capability, dict) or capability.get("ready") is not True or capability.get("mode") != "real":
            raise RuntimeError(f"real model-service check requires {name} capability ready: {capability}")

    # 声纹健康不能只证明模型权重可加载。实时多人区分实际调用 embedding 路由，旧进程
    # 缺少该路由时仍会把所有片段降级到同一个发言人，因此浅检查也必须拒绝这种假健康。
    voiceprint = capabilities.get("voiceprint", {})
    if voiceprint.get("embeddingReady") is not True:
        raise RuntimeError(f"real model-service check requires voiceprint embedding API ready: {voiceprint}")


def _require_real_voiceprint_response(payload: dict[str, Any], operation: str) -> None:
    """Reject a mock or fallback payload even when it uses a superficially successful status."""

    if payload.get("realModel") is not True or payload.get("fallbackReason") or payload.get("mockMode") is True:
        raise RuntimeError(f"voiceprint {operation} did not confirm real CAM++ output: {payload}")


def run_checks(args: argparse.Namespace) -> None:
    """执行小模型服务健康检查。"""

    base_url = args.base_url.rstrip("/")

    print("[check] model service health")
    health = _request_json("GET", f"{base_url}/v1/health")
    _require_real_health(health, deep=args.deep)
    print("health ok:", health.get("models", {}))

    if not args.deep:
        print("MODEL SERVICE CHECK OK")
        return

    audio_path = Path(args.audio_path) if args.audio_path else _create_tiny_wav()
    if not audio_path.exists():
        raise FileNotFoundError(f"deep 检查音频不存在：{audio_path}")
    audio = str(audio_path)
    speaker_id = f"check-{int(time.time())}"

    print("[check] vad split")
    vad = _request_json(
        "POST",
        f"{base_url}/v1/vad/split",
        {"audio_path": audio, "min_speech_ms": 200, "max_segment_ms": 30000},
        timeout=120,
    )
    if "segments" not in vad:
        raise RuntimeError(f"VAD 响应缺少 segments：{vad}")
    print("vad segments:", len(vad.get("segments", [])))

    print("[check] voiceprint register")
    registered = _request_json(
        "POST",
        f"{base_url}/v1/voiceprints/register",
        {
            "speaker_id": speaker_id,
            "speaker_name": "模型服务检查发言人",
            "audio_path": audio,
            "metadata": {"source": "check_model_services"},
        },
        timeout=180,
    )
    if registered.get("status") != "registered" or not registered.get("embeddingId"):
        raise RuntimeError(f"声纹注册响应异常：{registered}")
    _require_real_voiceprint_response(registered, "register")
    print("voiceprint registered:", registered.get("embeddingId", registered.get("status")))

    print("[check] voiceprint match")
    matched = _request_json(
        "POST",
        f"{base_url}/v1/voiceprints/match",
        {"audio_path": audio, "top_k": 1},
        timeout=180,
    )
    if "matches" not in matched:
        raise RuntimeError(f"声纹匹配响应缺少 matches：{matched}")
    _require_real_voiceprint_response(matched, "match")
    print("voiceprint matches:", len(matched.get("matches", [])))

    print("[check] speaker embedding")
    embedding = _request_json(
        "POST",
        f"{base_url}/v1/speakers/embedding",
        {"audio_path": audio},
        timeout=180,
    )
    # deep 检查必须拿到非空真实向量；只验证 200 响应仍可能把 mock/fallback 当作正常能力。
    if not isinstance(embedding.get("embedding"), list) or not embedding.get("embedding"):
        raise RuntimeError(f"说话人 embedding 响应异常：{embedding}")
    _require_real_voiceprint_response(embedding, "embedding")
    print("speaker embedding dimensions:", len(embedding["embedding"]))

    print("[check] alignment selection-window")
    aligned = _request_json(
        "POST",
        f"{base_url}/v1/align/selection-window",
        {
            "audio_path": audio,
            "transcript_text": "模型服务检查发言人正在验证智能会议系统。",
            "selected_text": "智能会议",
            "padding_ms": 200,
        },
        timeout=180,
    )
    if "start_ms" not in aligned or "end_ms" not in aligned:
        raise RuntimeError(f"选区对齐响应异常：{aligned}")
    print("alignment window:", aligned.get("start_ms"), aligned.get("end_ms"))

    print("MODEL SERVICE CHECK OK")


def main() -> None:
    parser = argparse.ArgumentParser(description="检查智能会议小模型 HTTP 服务")
    parser.add_argument("--base-url", default="http://127.0.0.1:8100", help="小模型服务地址")
    parser.add_argument("--deep", action="store_true", help="额外调用 VAD、声纹和对齐接口")
    parser.add_argument("--audio-path", default="", help="deep 检查使用的本机 wav 文件路径")
    run_checks(parser.parse_args())


if __name__ == "__main__":
    main()
