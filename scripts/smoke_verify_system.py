"""智能会议系统本地冒烟验证脚本。

这个脚本给开发和部署人员使用：启动前端、后端、小模型服务以后，运行它即可快速确认
核心 API 是否可用。它只使用 Python 标准库，避免在服务器上为了验收再额外安装 requests。

默认验证范围是“安全验证”：
- 后端健康检查、会议列表、首页概览。
- 声纹库、关键词库、敏感词库、纪要模板等管理接口。
- 前端静态页面是否能访问。
- 小模型服务健康检查。

离线 ASR 会真实调用 Qwen3-ASR/DashScope 或远程 ASR 服务，可能产生耗时和费用，因此默认不跑。
如果需要完整验证上传 -> 转写链路，请显式增加 `--include-asr`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Any


CAPABILITY_STATUSES = {"ready", "degraded", "failed"}
CAPABILITY_REPORT_PATH = Path(__file__).resolve().parents[1] / "test-results" / "capability-report.json"


async def _exercise_realtime_stream(
    backend: str,
    meeting_id: str,
    audio_path: Path,
    *,
    language: str = "zh",
    connect_factory: Any | None = None,
) -> dict[str, Any]:
    """Stream one known PCM WAV through the public realtime WebSocket and collect provider events.

    This is deliberately different from uploading the same file to the import endpoint.  The WAV
    header is removed, 16-bit mono PCM is sent in browser-sized 100ms frames, and the smoke waits for
    the backend's provider-native ``streaming_started`` acknowledgement before sending audio.  A
    final ``closed`` event proves ``session.finish`` had a chance to flush the last utterance.

    ``connect_factory`` is injectable so unit tests can validate the protocol without opening a
    network socket or consuming paid ASR capacity.
    """

    with wave.open(str(audio_path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        pcm_bytes = wav_file.readframes(wav_file.getnframes())
    if channels != 1 or sample_width != 2:
        raise ValueError("realtime smoke requires mono 16-bit PCM WAV input")

    if connect_factory is None:
        from websockets.asyncio.client import connect as websocket_connect

        connect_factory = websocket_connect
    parsed = urllib.parse.urlparse(backend)
    websocket_scheme = "wss" if parsed.scheme == "https" else "ws"
    websocket_url = f"{websocket_scheme}://{parsed.netloc}/api/meetings/{urllib.parse.quote(meeting_id)}/realtime"
    events: list[dict[str, Any]] = []
    first_text_latency_ms: int | None = None
    session_token = f"smoke-realtime-{int(time.time() * 1000)}"

    async with connect_factory(websocket_url, max_size=4 * 1024 * 1024) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "realtime_config",
                    "streamingMode": "dashscope_realtime",
                    "audioFormat": "pcm16",
                    "sampleRate": sample_rate,
                    "language": language,
                    "sessionToken": session_token,
                },
                ensure_ascii=False,
            )
        )
        while True:
            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
            events.append(event)
            code = str(event.get("code") or "")
            if event.get("type") == "error" or code == "streaming_unavailable":
                raise RuntimeError(str(event.get("message") or event))
            if code == "streaming_started":
                break

        stream_started_at = time.monotonic()

        async def receive_until_closed() -> None:
            nonlocal first_text_latency_ms
            while True:
                event = json.loads(await socket.recv())
                events.append(event)
                text = str(event.get("text") or (event.get("segment") or {}).get("text") or "").strip()
                if text and first_text_latency_ms is None:
                    first_text_latency_ms = int((time.monotonic() - stream_started_at) * 1000)
                if event.get("type") == "error":
                    raise RuntimeError(str(event.get("message") or event))
                if event.get("type") == "closed":
                    return

        receiver = asyncio.create_task(receive_until_closed())
        frame_bytes = max(2, int(sample_rate * 2 * 0.1))
        for offset in range(0, len(pcm_bytes), frame_bytes):
            await socket.send(pcm_bytes[offset : offset + frame_bytes])
            # Two-times realtime pacing keeps the smoke reasonably fast while preserving the
            # incremental stream behavior required by server VAD and partial transcript events.
            await asyncio.sleep(0.05)
        await socket.send("stop")
        await asyncio.wait_for(receiver, timeout=30)

    transcript_text = "\n".join(
        str((event.get("segment") or {}).get("text") or "")
        for event in events
        if event.get("type") == "transcript"
    ).strip()
    return {
        "events": events,
        "transcriptText": transcript_text,
        "firstTextLatencyMs": first_text_latency_ms,
        "sampleRate": sample_rate,
        "audioDurationMs": int(len(pcm_bytes) / max(1, sample_rate * 2) * 1000),
    }


def new_capability_report() -> dict[str, dict[str, Any]]:
    """Return the empty, JSON-serializable capability-report payload.

    The report deliberately contains only subsystem records.  Keeping it flat makes it easy for
    CI, deployment tooling, and the frontend diagnostics screen to read a status by subsystem
    name without having to understand the human-oriented smoke-test console output.
    """

    return {}


def record_capability(
    report: dict[str, dict[str, Any]],
    subsystem: str,
    status: str,
    *,
    message: str = "",
    **details: Any,
) -> None:
    """Store one normalized capability state and reject accidental status spelling drift.

    ``ready`` means the production path has actually been exercised.  ``degraded`` means a
    deliberately skipped or unavailable optional dependency; ``failed`` means a requested product
    contract did not hold.  A closed vocabulary prevents CI from silently accepting ad-hoc labels.
    """

    if status not in CAPABILITY_STATUSES:
        raise ValueError(f"unsupported capability status: {status}")
    report[subsystem] = {"status": status, "message": message, **details}


def record_model_capabilities(report: dict[str, dict[str, Any]], capabilities: dict[str, Any]) -> None:
    """Translate backend model probes without promoting mocks or missing weights to ``ready``.

    Backend probes call a capability real only when the local model service reported both
    ``ready=True`` and ``mode=real``.  This second check is intentionally repeated in the smoke
    reporter: a reachable mock service is useful for unit tests, but is not evidence that CAM++ or
    Qwen3-ForcedAligner weights are installed and usable in a deployment.
    """

    for name in ("vad", "voiceprint", "alignment"):
        raw = capabilities.get(name)
        raw = raw if isinstance(raw, dict) else {}
        is_real_ready = raw.get("ready") is True and raw.get("mode") == "real"
        record_capability(
            report,
            name,
            "ready" if is_real_ready else "degraded",
            message=str(raw.get("message") or f"{name} capability was not reported ready"),
            mode=str(raw.get("mode") or "unavailable"),
            endpoint=str(raw.get("endpoint") or ""),
        )


def write_capability_report(report: dict[str, dict[str, Any]], output_path: Path = CAPABILITY_REPORT_PATH) -> None:
    """Persist the report as deterministic UTF-8 JSON, creating only its direct parent folders.

    ``mkdir(..., exist_ok=True)`` makes a repeated smoke run safe while avoiding cleanup commands.
    The smoke script never removes prior files or directories; it atomically replaces this one
    report path through the normal file write used for test artifacts.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_step(name: str) -> None:
    """打印当前验证步骤，便于定位失败发生在哪个模块。"""

    print(f"\n[check] {name}")


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    """发送 JSON 请求并返回对象。

    后端所有业务接口都约定返回 JSON。这里把 HTTP 错误体也读出来，方便部署时直接看到
    “模型未配置”“数据库不可用”等业务提示，而不是只看到 500/503。
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


def _request_bytes(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> bytes:
    """发送请求并返回原始字节。

    docx/mp3 导出接口不是 JSON 响应。冒烟脚本必须真实检查这类下载接口是否能返回非空文件，
    否则会出现“页面按钮能点，但下载时后端才报错”的验收盲区。payload 仍按 JSON 发送，
    这样和前端 `fetch(..., body: JSON.stringify(...))` 的调用方式保持一致。
    """

    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc


def _get_text(url: str, timeout: int = 10) -> str:
    """读取文本页面，用于确认前端静态服务已启动。"""

    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _multipart_upload(
    url: str,
    file_field: str,
    file_path: Path,
    timeout: int = 60,
    fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    """使用标准库构造 multipart/form-data 文件上传请求。

    该函数用于验证 `/api/meetings/{id}/files` 和声纹样本上传接口。真实系统中文件可能很大，
    但冒烟测试只上传一个极小 wav，重点验证接口、目录、数据库和任务记录能否正常工作。
    """

    boundary = f"----meeting-smoke-{int(time.time() * 1000)}"
    filename = file_path.name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    file_bytes = file_path.read_bytes()
    parts: list[bytes] = []
    # The import endpoint freezes its configuration from ordinary form fields.  Add them before
    # the file so both FastAPI's browser upload path and this standard-library smoke request use
    # the same multipart contract without pulling in a test-only HTTP dependency.
    for name, value in (fields or {}).items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    parts.extend(
        [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8"),
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib.request.Request(
        url,
        data=b"".join(parts),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"upload {url} -> HTTP {exc.code}: {detail}") from exc


def _create_tiny_wav() -> Path:
    """生成一个极小的 16k 单声道 wav 文件。

    这个文件只用于接口冒烟，不用于验证真实识别准确率。真实 ASR/声纹效果请用 15 秒以上清晰人声样本验证。
    """

    path = Path(tempfile.gettempdir()) / "meeting_smoke_test.wav"
    sample_rate = 16000
    duration_seconds = 1
    frames = b"\x00\x00" * sample_rate * duration_seconds
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return path


def run_legacy_smoke(args: argparse.Namespace) -> None:
    """执行端到端冒烟验证。"""

    backend = args.backend.rstrip("/")
    frontend = args.frontend.rstrip("/")
    model_service = args.model_service.rstrip("/")

    _print_step("后端健康检查")
    health = _request_json("GET", f"{backend}/api/health")
    assert health.get("status") == "ok", health
    print("backend ok:", health.get("asrGatewayMode"), "modelMockMode=", health.get("modelMockMode"))

    _print_step("会议 AI 本地编排状态")
    workflow_status = _request_json("GET", f"{backend}/api/workflows/status")
    assert "workflows" in workflow_status, workflow_status
    print(
        "ai mode:",
        workflow_status.get("mode"),
        "provider:",
        workflow_status.get("provider"),
    )

    _print_step("前端页面可访问")
    html = _get_text(f"{frontend}/")
    assert "智能会议系统" in html, "前端首页未包含系统标题"
    print("frontend ok")

    _print_step("本地/算力模型服务健康检查")
    model_health = _request_json("GET", f"{model_service}/v1/health")
    assert model_health.get("status") == "ok", model_health
    print("model service ok:", model_health.get("models", {}))

    _print_step("会议列表与首页概览")
    meetings = _request_json("GET", f"{backend}/api/meetings")
    overview = _request_json("GET", f"{backend}/api/dashboard/overview")
    assert isinstance(meetings.get("items"), list), meetings
    # 首页概览条为了方便前端渲染，返回 `items: [{key,label,value,hint}]`。
    # 这里转成 key 集合检查核心指标，避免把验证脚本写死成某一种后端内部结构。
    overview_keys = {item.get("key") for item in overview.get("items", [])}
    assert {"todayMeetings", "readyMinutes", "pendingTodos"}.issubset(overview_keys), overview
    print("meetings:", meetings.get("total"), "overview keys:", sorted(overview_keys))

    _print_step("创建会议并读取详情")
    created = _request_json(
        "POST",
        f"{backend}/api/meetings",
        {
            "meetingName": f"冒烟测试会议 {int(time.time())}",
            "meetingLocation": "本地测试",
            "language": "中文普通话",
            "translateDirection": "无",
            "audioSource": "上传文件",
            "enableDiarization": True,
            "keywordLibraryIds": ["kw-001"],
            "templateId": "tpl-001",
        },
    )
    meeting_id = created["id"]
    detail = _request_json("GET", f"{backend}/api/meetings/{urllib.parse.quote(meeting_id)}")
    assert detail["id"] == meeting_id, detail
    print("created meeting:", meeting_id)

    _print_step("配置库接口")
    keyword = _request_json(
        "POST",
        f"{backend}/api/dictionaries/keyword-libraries",
        {"name": "冒烟测试词库", "words": ["智能会议", "声纹注册"], "enabled": True, "scope": "测试"},
    )
    sensitive = _request_json(
        "POST",
        f"{backend}/api/dictionaries/sensitive-rules",
        {
            "word": "冒烟禁忌词",
            "displayMode": "stars",
            "replacement": "stars",
            "enabled": True,
            "caseSensitive": False,
            "language": "zh",
            "applyScope": "展示",
        },
    )
    templates = _request_json("GET", f"{backend}/api/minute-templates?source=system")
    assert keyword.get("id") and sensitive.get("id") and templates.get("items"), (keyword, sensitive, templates)
    print("keyword:", keyword["id"], "sensitive:", sensitive["id"], "system templates:", len(templates["items"]))

    _print_step("声纹资料与样本上传接口")
    voiceprint = _request_json(
        "POST",
        f"{backend}/api/voiceprints",
        {"name": "冒烟测试发言人", "department": "测试部门", "samples": 0, "enabled": True, "groupId": "vg-ungrouped"},
    )
    wav_path = _create_tiny_wav()
    sample_result = _multipart_upload(f"{backend}/api/voiceprints/{voiceprint['id']}/samples", "file", wav_path)
    # 声纹样本上传返回 `{status, voiceprint, job, sample}`，便于前端同时刷新人员卡片和任务状态。
    updated_voiceprint = sample_result.get("voiceprint", {})
    assert updated_voiceprint.get("id") == voiceprint["id"], sample_result
    print("voiceprint:", voiceprint["id"], "registerStatus:", updated_voiceprint.get("registerStatus"))

    _print_step("会议文件上传与任务记录")
    file_result = _multipart_upload(f"{backend}/api/meetings/{meeting_id}/files", "file", wav_path)
    assert file_result.get("id"), file_result
    jobs = _request_json("GET", f"{backend}/api/meetings/{meeting_id}/jobs")
    assert isinstance(jobs.get("items"), list), jobs
    print("file:", file_result["id"], "jobs:", len(jobs["items"]))

    if args.include_asr:
        _print_step("真实/远程 ASR 转写链路")
        # 该步骤会使用当前后端配置的 ASR_GATEWAY_MODE，可能访问 DashScope 或算力服务器。
        # 用于最终联调，不建议每次普通冒烟都跑。
        transcribed = _request_json(
            "POST",
            f"{backend}/api/files/{file_result['id']}/transcribe",
            {"enableDiarization": True, "language": "zh", "enableITN": True},
            timeout=360,
        )
        assert transcribed.get("status") in {"completed", "waiting_model_config"}, transcribed
        print("transcribe status:", transcribed.get("status"), "segments:", len(transcribed.get("segments", [])))

    _print_step("AI 五工作流与 docx 导出接口")
    # DeepSeek 真实生成摘要/纪要时可能超过普通管理接口的 20 秒默认超时。这里把 AI 工具统一放宽，
    # 验证真实模型链路时不把“模型生成较慢”误判成后端不可用；前端 fetch 本身也没有 20 秒限制。
    summary = _request_json("POST", f"{backend}/api/meetings/{meeting_id}/summaries/generate", timeout=180)
    minutes = _request_json(
        "POST",
        f"{backend}/api/meetings/{meeting_id}/minutes/generate",
        {"templateName": "默认模板"},
        timeout=180,
    )
    todos = _request_json("POST", f"{backend}/api/meetings/{meeting_id}/todos/extract", timeout=180)
    translate = _request_json(
        "POST",
        f"{backend}/api/meetings/{meeting_id}/translate",
        {"text": "请完成智能会议系统联调。", "targetLanguage": "en"},
        timeout=180,
    )
    discourse = _request_json("POST", f"{backend}/api/meetings/{meeting_id}/discourse/reorganize", timeout=180)
    docx_bytes = _request_bytes(
        "POST",
        f"{backend}/api/meetings/{meeting_id}/exports/docx",
        {"exportKind": "all"},
        timeout=60,
    )
    assert isinstance(summary.get("keywords"), list) and minutes.get("content") and todos.get("items") is not None, (
        summary,
        minutes,
        todos,
    )
    assert translate.get("text") and discourse.get("text") and len(docx_bytes) > 100, (
        translate,
        discourse,
        len(docx_bytes),
    )
    print("ai workflow mock/remote ok; docx bytes:", len(docx_bytes))

    _print_step("清理冒烟测试数据")
    _request_json("DELETE", f"{backend}/api/meetings/{meeting_id}")
    _request_json("DELETE", f"{backend}/api/dictionaries/keyword-libraries/{keyword['id']}")
    _request_json("DELETE", f"{backend}/api/dictionaries/sensitive-rules/{sensitive['id']}")
    _request_json("DELETE", f"{backend}/api/voiceprints/{voiceprint['id']}")
    print("cleanup ok")

    print("\nSMOKE OK")


def _request_json_status(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
) -> tuple[int, dict[str, Any]]:
    """Return an HTTP status and JSON error payload for a negative product-contract assertion.

    The normal helper intentionally raises on errors.  Realtime/import separation is different:
    the expected outcome is a ``409`` rejection, so the smoke test needs to inspect that response
    as evidence rather than treating it as a transport failure.
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
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def _assert_source_references(payload: dict[str, Any], expected_segment_ids: set[str], label: str) -> None:
    """Require derived output to name durable transcript IDs and timestamp ranges.

    A non-empty generated paragraph is not sufficient product evidence.  The UI source jump and
    stale-artifact logic both require IDs, while the audio jump also requires bounded ranges.
    """

    source_ids = {str(item).strip() for item in payload.get("sourceSegmentIds", []) if str(item).strip()}
    source_ranges = payload.get("sourceRanges", [])
    assert source_ids and source_ids.issubset(expected_segment_ids), (label, payload)
    assert isinstance(source_ranges, list) and source_ranges, (label, payload)
    assert all(str(item.get("segmentId") or "") in source_ids for item in source_ranges if isinstance(item, dict)), (
        label,
        payload,
    )


def _cleanup_smoke_resources(backend: str, resources: dict[str, list[str]], report: dict[str, dict[str, Any]]) -> None:
    """Best-effort removal of only IDs created by this run, without masking the primary failure.

    Each request is independently protected so a failed template cleanup does not leave a meeting,
    a sensitive rule, and a voiceprint behind.  This script never uses filesystem deletion or a
    bulk-delete endpoint; it issues one explicit API request per generated record.
    """

    endpoint_templates = {
        "meetings": "/api/meetings/{id}",
        "keywordLibraries": "/api/dictionaries/keyword-libraries/{id}",
        "sensitiveRules": "/api/dictionaries/sensitive-rules/{id}",
        "templates": "/api/minute-templates/{id}",
        "voiceprints": "/api/voiceprints/{id}",
    }
    failures: list[str] = []
    for resource_type, endpoint_template in endpoint_templates.items():
        for resource_id in resources.get(resource_type, []):
            try:
                _request_json("DELETE", f"{backend}{endpoint_template.format(id=urllib.parse.quote(resource_id))}")
                if resource_type == "meetings":
                    status, _ = _request_json_status(
                        "GET", f"{backend}{endpoint_template.format(id=urllib.parse.quote(resource_id))}"
                    )
                    if status != 404:
                        failures.append(f"meetings:{resource_id}: still readable after delete (HTTP {status})")
            except Exception as exc:  # noqa: BLE001 - cleanup must continue with the next known ID.
                failures.append(f"{resource_type}:{resource_id}: {exc}")
    # Meeting deletion owns its uploaded files. The smoke records every server path returned by its
    # own upload calls and verifies those exact files disappeared; it never performs filesystem
    # deletion itself, so a leaking backend cannot be hidden behind a green cleanup report.
    for raw_path in resources.get("uploadedFiles", []):
        if raw_path and Path(raw_path).exists():
            failures.append(f"uploaded file still exists after meeting cleanup: {raw_path}")
    record_capability(
        report,
        "cleanup",
        "ready" if not failures else "degraded",
        message="cleanup completed" if not failures else "; ".join(failures),
    )


def _strict_product_smoke(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    """Exercise the configuration, transcription, revision, minutes, export, and source chain.

    The default command intentionally stops before paid/remote ASR.  Passing ``--include-asr``
    enables the import endpoint with an explicitly supplied known audio file and continues through
    speaker correction, stale artifacts, alternate-template minutes, and export verification.
    """

    backend = args.backend.rstrip("/")
    frontend = args.frontend.rstrip("/")
    model_service = args.model_service.rstrip("/")
    report = new_capability_report()
    resources: dict[str, list[str]] = {
        "meetings": [],
        "keywordLibraries": [],
        "sensitiveRules": [],
        "templates": [],
        "voiceprints": [],
        "uploadedFiles": [],
    }

    # A path alone proves only that an ASR endpoint accepted bytes.  The expected fragment makes
    # ``--include-asr`` a real known-audio verification and also supplies a deterministic term for
    # the frozen sensitive-policy assertion below.
    expected_asr_text = str(getattr(args, "asr_expected_text", "") or "").strip()
    asr_language = str(getattr(args, "asr_language", "zh") or "zh").strip()

    try:
        if args.include_asr and not expected_asr_text:
            raise ValueError("--include-asr requires --asr-expected-text from the known audio")
        _print_step("backend health and model capability truth")
        health = _request_json("GET", f"{backend}/api/health")
        assert health.get("status") == "ok", health
        record_capability(report, "backend", "ready", message="FastAPI health endpoint returned ok")

        model_status = _request_json("GET", f"{backend}/api/model-services/status")
        record_model_capabilities(report, model_status)
        # The standalone service health remains a process diagnostic.  Individual capability
        # records above are the only model readiness truth used by this report.
        try:
            model_health = _request_json("GET", f"{model_service}/v1/health")
            record_capability(
                report,
                "modelService",
                "ready" if model_health.get("status") == "ok" else "degraded",
                message="model service process responded" if model_health.get("status") == "ok" else str(model_health),
            )
        except Exception as exc:  # noqa: BLE001 - backend status can still explain which model is absent.
            record_capability(report, "modelService", "degraded", message=str(exc))

        _print_step("frontend availability")
        html = _get_text(f"{frontend}/")
        assert "智能会议系统" in html, "frontend landing page is missing the product title"
        record_capability(report, "frontend", "ready", message="product landing page is reachable")

        workflow_status = _request_json("GET", f"{backend}/api/workflows/status")
        assert "workflows" in workflow_status, workflow_status
        record_capability(report, "workflows", "ready", message="AI workflow status contract is reachable")

        _print_step("frozen configuration and realtime/import separation")
        keyword = _request_json(
            "POST",
            f"{backend}/api/dictionaries/keyword-libraries",
            {"name": f"smoke-keywords-{int(time.time())}", "words": ["intelligent meeting", "release plan"], "enabled": True},
        )
        sensitive = _request_json(
            "POST",
            f"{backend}/api/dictionaries/sensitive-rules",
            {
                "word": expected_asr_text or "confidential",
                "displayMode": "stars",
                "replacement": "stars",
                "enabled": True,
                "caseSensitive": False,
                # The masking rule must use the same language family as the known audio. Hard-coding
                # English made a Chinese smoke fixture both misconfigure ASR and miss its display rule.
                "language": asr_language.split("-", 1)[0].lower(),
                "applyScope": "all",
            },
        )
        resources["keywordLibraries"].append(str(keyword["id"]))
        resources["sensitiveRules"].append(str(sensitive["id"]))
        templates = _request_json("GET", f"{backend}/api/minute-templates?source=system")
        template_items = templates.get("items", [])
        assert isinstance(template_items, list) and len(template_items) >= 2, templates
        primary_template_id = str(template_items[0]["id"])
        alternate_template_id = str(template_items[1]["id"])

        realtime = _request_json(
            "POST",
            f"{backend}/api/meetings",
            {
                "meetingName": f"smoke realtime {int(time.time())}",
                "meetingLocation": "local smoke verification",
                "language": asr_language,
                "audioSource": "desktop microphone",
                "enableDiarization": True,
                "participantNames": ["Avery", "Blake"],
                "keywordLibraryIds": [keyword["id"]],
                "templateId": primary_template_id,
                "optimizationProfile": {"manual": True, "smart": True},
            },
        )
        realtime_id = str(realtime["id"])
        resources["meetings"].append(realtime_id)
        realtime_config = realtime.get("processingConfig", {})
        assert realtime_config.get("transcriptionMode") == "realtime", realtime
        assert realtime_config.get("keywordLibraryIds") == [keyword["id"]], realtime
        assert realtime_config.get("templateId") == primary_template_id, realtime
        # Configuration and the 409 mode boundary prove ownership, not live microphone transcription.
        # Until this smoke opens the realtime WebSocket and observes a persisted final, keep the
        # machine-readable capability honest instead of borrowing evidence from import ASR.
        record_capability(
            report,
            "realtime",
            "degraded",
            message="configuration snapshot verified; realtime media stream not exercised",
            revision=realtime.get("transcriptRevision", 0),
        )

        realtime_view = _request_json("GET", f"{backend}/api/meetings/{urllib.parse.quote(realtime_id)}/transcript-view?target=display")
        assert realtime_view.get("target") == "display", realtime_view
        assert realtime_config.get("recognitionPolicy"), realtime
        record_capability(report, "recognitionPolicy", "ready", message="frozen realtime recognition policy is present")
        record_capability(report, "sensitivePolicy", "ready", message="display policy view is detached from source text")

        # Uploading to a realtime record is allowed for evidence/export, but offline import ASR
        # must reject it.  The 409 assertion proves the two product paths cannot silently merge.
        realtime_audio = _create_tiny_wav()
        realtime_file = _multipart_upload(f"{backend}/api/meetings/{urllib.parse.quote(realtime_id)}/files", "file", realtime_audio)
        if realtime_file.get("path"):
            resources["uploadedFiles"].append(str(realtime_file["path"]))
        separation_status, separation_payload = _request_json_status(
            "POST",
            f"{backend}/api/files/{urllib.parse.quote(str(realtime_file['id']))}/transcribe",
            {"enableDiarization": True},
        )
        assert separation_status == 409, (separation_status, separation_payload)

        if not args.include_asr:
            # The non-ASR command remains useful as an inexpensive smoke check.  Its report must
            # make the omitted product stages explicit instead of implying that mocks proved them.
            for subsystem in ("asr", "import", "minutes", "export", "sourceReferences"):
                record_capability(report, subsystem, "degraded", message="run again with --include-asr and known audio")
            print("ASR product chain skipped; pass --include-asr with a known audio file for full verification")
            return report

        audio_path = Path(getattr(args, "asr_audio", "") or "")
        if not audio_path.is_file():
            raise FileNotFoundError("--include-asr requires --asr-audio pointing to a known local audio file")

        _print_step("known-audio realtime WebSocket ASR")
        realtime_result = asyncio.run(
            _exercise_realtime_stream(
                backend,
                realtime_id,
                audio_path,
                language=asr_language,
            )
        )
        realtime_persisted = _request_json(
            "GET", f"{backend}/api/meetings/{urllib.parse.quote(realtime_id)}"
        )
        realtime_segments = realtime_persisted.get("segments", [])
        realtime_text = "\n".join(str(segment.get("text") or "") for segment in realtime_segments)
        assert realtime_segments, ("realtime stream returned no persisted final", realtime_result)
        assert expected_asr_text.casefold() in realtime_text.casefold(), (expected_asr_text, realtime_text)
        record_capability(
            report,
            "realtime",
            "ready",
            message="known PCM audio produced and persisted realtime WebSocket final text",
            segments=len(realtime_segments),
            firstTextLatencyMs=realtime_result.get("firstTextLatencyMs"),
            audioDurationMs=realtime_result.get("audioDurationMs"),
        )

        _print_step("known-audio import ASR")
        imported = _multipart_upload(
            f"{backend}/api/imports/transcribe",
            "file",
            audio_path,
            fields={
                "language": asr_language,
                "template_id": primary_template_id,
                "keyword_library_ids": str(keyword["id"]),
                "enable_diarization": "true",
            },
            timeout=360,
        )
        imported_meeting = imported.get("meeting", {})
        import_id = str(imported_meeting.get("id") or "")
        assert import_id, imported
        resources["meetings"].append(import_id)
        if imported.get("file", {}).get("path"):
            resources["uploadedFiles"].append(str(imported["file"]["path"]))
        import_config = imported_meeting.get("processingConfig", {})
        transcription = imported.get("transcription", {})
        segments = imported_meeting.get("segments", [])
        assert import_config.get("transcriptionMode") == "import", imported
        assert transcription.get("status") == "completed" and isinstance(segments, list) and segments, imported
        segment_ids = {str(segment.get("id") or "") for segment in segments if str(segment.get("id") or "")}
        assert segment_ids, imported
        recognized_text = "\n".join(str(segment.get("text") or "") for segment in segments)
        assert expected_asr_text.casefold() in recognized_text.casefold(), (expected_asr_text, recognized_text)
        imported_display = _request_json(
            "GET", f"{backend}/api/meetings/{urllib.parse.quote(import_id)}/transcript-view?target=display"
        )
        display_text = "\n".join(str(segment.get("displayText") or "") for segment in imported_display.get("segments", []))
        assert expected_asr_text.casefold() not in display_text.casefold(), (expected_asr_text, display_text)
        record_capability(report, "asr", "ready", message="known audio completed import transcription", segments=len(segment_ids))
        record_capability(report, "import", "ready", message="import record remained separate from realtime", meetingId=import_id)
        record_capability(report, "sensitivePolicy", "ready", message="frozen display policy masked the known ASR fragment")

        _print_step("speaker correction, stale artifact, minutes version, export, and provenance")
        first_minutes = _request_json(
            "POST", f"{backend}/api/meetings/{urllib.parse.quote(import_id)}/minutes/generate", {"templateId": primary_template_id}, timeout=180
        )
        _assert_source_references(first_minutes, segment_ids, "first minutes")
        speaker_name = str(segments[0].get("speakerName") or "发言人1")
        correction = _request_json(
            "POST",
            f"{backend}/api/meetings/{urllib.parse.quote(import_id)}/speaker-correction",
            {"oldName": speaker_name, "name": "Smoke Verified Speaker", "syncMode": "meeting_only"},
        )
        assert correction.get("segments"), correction
        corrected = _request_json("GET", f"{backend}/api/meetings/{urllib.parse.quote(import_id)}")
        assert corrected.get("minutesArtifact", {}).get("status") == "stale", corrected
        second_minutes = _request_json(
            "POST",
            f"{backend}/api/meetings/{urllib.parse.quote(import_id)}/minutes/generate",
            {"templateId": alternate_template_id},
            timeout=180,
        )
        assert second_minutes.get("templateId") == alternate_template_id, second_minutes
        _assert_source_references(second_minutes, segment_ids, "regenerated minutes")
        versions = _request_json("GET", f"{backend}/api/meetings/{urllib.parse.quote(import_id)}/minutes/versions")
        assert len(versions.get("items", [])) >= 2 and versions["items"][-1].get("status") == "current", versions
        record_capability(report, "minutes", "ready", message="speaker edit staled old minutes and alternate template regenerated a current version")

        summary = _request_json("POST", f"{backend}/api/meetings/{urllib.parse.quote(import_id)}/summaries/generate", timeout=180)
        todos = _request_json("POST", f"{backend}/api/meetings/{urllib.parse.quote(import_id)}/todos/extract", timeout=180)
        _assert_source_references(summary, segment_ids, "summary")
        _assert_source_references(todos, segment_ids, "todos")
        _assert_source_references(second_minutes, segment_ids, "minutes")
        record_capability(report, "sourceReferences", "ready", message="summary, todos, and minutes name imported source segments")

        docx_bytes = _request_bytes(
            "POST", f"{backend}/api/meetings/{urllib.parse.quote(import_id)}/exports/docx", {"exportKind": "all"}, timeout=60
        )
        text_bytes = _request_bytes(
            "POST", f"{backend}/api/meetings/{urllib.parse.quote(import_id)}/exports/text", {"exportKind": "transcript"}, timeout=60
        )
        assert len(docx_bytes) > 100 and text_bytes, (len(docx_bytes), len(text_bytes))
        record_capability(report, "export", "ready", message="DOCX and text exports returned non-empty documents")

        # CAM++ calls require a real capability and a human voice sample.  The condition is kept
        # here, before either registration or matching request, so a mock endpoint cannot receive
        # a request that later gets mislabeled as production enrollment evidence.
        voiceprint_audio = Path(getattr(args, "voiceprint_audio", "") or "")
        if report["voiceprint"]["status"] == "ready" and voiceprint_audio.is_file():
            voiceprint = _request_json("POST", f"{backend}/api/voiceprints", {"name": "Smoke Voiceprint", "enabled": True})
            resources["voiceprints"].append(str(voiceprint["id"]))
            registered = _multipart_upload(
                f"{backend}/api/voiceprints/{urllib.parse.quote(str(voiceprint['id']))}/samples",
                "file",
                voiceprint_audio,
                timeout=180,
            )
            assert registered.get("status") == "completed" and registered.get("voiceprint", {}).get("realModel") is True, registered
            matched = _request_json(
                "POST",
                f"{model_service}/v1/voiceprints/match",
                {"audio_path": str(voiceprint_audio.resolve()), "top_k": 1},
                timeout=180,
            )
            assert matched.get("realModel") is True and isinstance(matched.get("matches"), list), matched
            record_capability(report, "voiceprint", "ready", message="real CAM++ registration and match completed")
        elif report["voiceprint"]["status"] == "ready":
            record_capability(report, "voiceprint", "degraded", message="real CAM++ is ready; pass --voiceprint-audio for registration and match")

        return report
    except Exception as exc:
        record_capability(report, "smoke", "failed", message=str(exc))
        raise
    finally:
        _cleanup_smoke_resources(backend, resources, report)
        write_capability_report(report, Path(getattr(args, "capability_report", "") or CAPABILITY_REPORT_PATH))


def run_smoke(args: argparse.Namespace) -> dict[str, dict[str, Any]] | None:
    """Run the strict product smoke for CLI callers while retaining the legacy library contract.

    Older unit callers constructed a minimal ``argparse.Namespace`` before the strict product
    chain existed.  Missing ``strict_product_chain`` therefore selects their historical behavior;
    the command-line parser always sets it to true and is the supported operational entry point.
    """

    if getattr(args, "strict_product_chain", False):
        return _strict_product_smoke(args)
    run_legacy_smoke(args)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="智能会议系统本地冒烟验证")
    parser.add_argument("--backend", default="http://127.0.0.1:8001", help="智能会议后端地址")
    parser.add_argument("--frontend", default="http://127.0.0.1:5173", help="前端静态服务地址")
    parser.add_argument("--model-service", default="http://127.0.0.1:8100", help="小模型服务地址")
    parser.add_argument("--include-asr", action="store_true", help="额外执行真实/远程 ASR 转写验证")
    parser.add_argument("--asr-audio", default="", help="--include-asr 时必填：已知内容的本机音频文件")
    parser.add_argument("--asr-expected-text", default="", help="--include-asr 时必填：音频中可识别的文本片段")
    parser.add_argument("--asr-language", default="zh", help="已知音频的识别语言，例如 zh、en 或 auto")
    parser.add_argument("--voiceprint-audio", default="", help="真实 CAM++ 就绪时可选：用于注册和匹配的人声音频")
    parser.add_argument("--capability-report", default=str(CAPABILITY_REPORT_PATH), help="能力报告 JSON 输出路径")
    # Keep the comprehensive path as the public command default.  The hidden switch is only a
    # compatibility bridge for code importing ``run_smoke`` with the old minimal Namespace shape.
    parser.set_defaults(strict_product_chain=True)
    run_smoke(parser.parse_args())


if __name__ == "__main__":
    main()
