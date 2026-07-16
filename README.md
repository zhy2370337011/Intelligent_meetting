# 智能会议系统 V1

这是一个面向智能会议场景的网页版原型和真实 FastAPI 后端骨架。主 ASR 模型按计划固定为 `Qwen3-ASR-1.7B`，首版通过模型网关提供 mock/remote 双模式，保证没有 910B 模型服务时也能完成页面和接口联调。

## 运行与验收

### 两条转写链路必须独立

- **实时会议**：先调用 `POST /api/meetings` 创建会议，再通过 `WS /api/meetings/{meeting_id}/realtime` 写入实时最终片段。创建记录时 `processingConfig.transcriptionMode` 固定为 `realtime`；上传到该会议的附件不能再调用离线转写接口。
- **导入转写**：页面使用 `POST /api/imports/transcribe` 一次性创建 `import` 记录、保存文件并离线转写。`POST /api/files/{file_id}/transcribe` 只接受该模式的文件，向实时会议写入会返回 `409`。

两条链路共享词库、敏感策略、纪要和导出能力，但绝不相互降级或互相写入片段。冒烟脚本会专门验证这条 `409` 边界。

### 快照、修订与过期结果

会议创建时会冻结识别配置：语言、说话人分离、参会人、声纹组、关键词库及有效词表、敏感词规则、纪要模板快照都保存在 `processingConfig`。之后修改全局词库或模板，不会重写已创建会议的历史输入。

最终实时片段、导入完成片段、文本编辑和发言人编辑都会递增 `transcriptRevision`。摘要、纪要、待办、语篇规整和重点标记会保存来源片段 ID、时间范围和来源修订；修订变化后旧产物标为 `stale`，但保留模型生成内容和人工编辑层。重新生成才会得到当前修订的 `current` 产物。

纪要每次生成都会形成不可变版本。切换模板会追加新版本而不是覆盖原版本；历史版本仍可查看和保留人工修改，当前版本由 `minutesCurrentVersionId` 指向。

### 模型就绪状态

`GET /api/model-services/status` 分别报告 `vad`、`voiceprint` 和 `alignment`。只有服务自报 `ready=true` 且 `mode=real` 时才是 `ready`；未配置、无法访问、缺少权重和 mock 服务均为 `degraded`。特别是 `Qwen3-ForcedAligner-0.6B` 未安装时，即使全部 mock 单测通过，能力报告也必须保持 `degraded`。

声纹注册和匹配仅在真实 CAM++ 就绪时执行。否则会议中的发言人编辑仍会保存，声纹同步会返回明确警告，不会伪造 embedding 或把 mock 结果显示成已注册。

### 精确命令

首次安装：

```powershell
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

本地开发启动：

```powershell
scripts\start_model_services.ps1 -Port 8100 -MockMode true
scripts\start_backend.ps1
scripts\start_frontend.ps1
```

离线回归测试不会调用外部 ASR、CAM++、VAD 或 ForcedAligner：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1
```

服务级基础冒烟会写入 `test-results/capability-report.json`；未请求真实 ASR 的阶段会如实标记为 `degraded`：

```powershell
backend\.venv\Scripts\python.exe scripts\smoke_verify_system.py
```

完整产品链路需要一段内容已知的本地音频。`--asr-expected-text` 必须是音频中应被识别的连续片段；脚本会验证导入 ASR、敏感词显示遮罩、发言人编辑导致旧纪要过期、另一模板重新生成、导出和来源引用：

```powershell
backend\.venv\Scripts\python.exe scripts\smoke_verify_system.py --include-asr --asr-audio "E:\\audio\\known-meeting.wav" --asr-expected-text "发布计划" --asr-language zh
```

仅当 `voiceprint` 状态已为真实 `ready` 时，才可额外传入清晰人声音频验证 CAM++ 注册和匹配：

```powershell
backend\.venv\Scripts\python.exe scripts\smoke_verify_system.py --include-asr --asr-audio "E:\\audio\\known-meeting.wav" --asr-expected-text "发布计划" --asr-language zh --voiceprint-audio "E:\\audio\\speaker-voice.wav"
```

能力报告中的每个子系统都有 `ready`、`degraded` 或 `failed` 状态及诊断消息。自动化部署应读取 JSON，不应依据 mock 测试结果宣称模型已就绪。

## 功能范围

- 实时会议转写接口：`WS /api/meetings/{meeting_id}/realtime`
- 离线音视频导入与转写：`POST /api/meetings/{meeting_id}/files`、`POST /api/files/{file_id}/transcribe`
- 声纹区分与选区注册：`POST /api/voiceprints/register-from-selection`
- 字音对照与音频区间反查：`POST /api/transcripts/{transcript_id}/align`
- 关键词优化与敏感词屏蔽：`POST /api/dictionaries/hotwords`、`POST /api/dictionaries/sensitive-words`
- AI 摘要、纪要、待办、翻译、语篇规整
- 对接普通会议系统待办推送 payload：`POST /api/meetings/{meeting_id}/todos/push`
- docx 文稿导出、mp3 音频导出接口

## 模型栈

- 主 ASR：`Qwen3-ASR-1.7B`
- 字音对照：`Qwen3-ForcedAligner-0.6B`
- 声纹：`CAM++`
- 翻译、摘要、纪要、待办、语篇规整：复用智能体平台大模型工作流

首版代码里，模型入口集中在 `backend/app/asr_gateway.py`。真实部署时设置：

```powershell
$env:ASR_GATEWAY_MODE="remote"
$env:ASR_GATEWAY_BASE_URL="http://你的910B模型服务地址"
```

远程服务需要实现：

```text
POST /v1/asr/transcribe
```

请求体包含 `model`、`meeting_id`、`file_id`、`hotwords`、`start_ms`、`end_ms`、`enable_diarization` 等字段。

## 本地运行

后端依赖已经建议安装到项目自己的 `backend\.venv` 中。首次安装：

```powershell
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

之后可以分别启动：

```powershell
scripts\start_backend.ps1
scripts\start_frontend.ps1
```

后端默认运行在 `http://127.0.0.1:8001`。如果需要改端口，可以先设置：

```powershell
$env:MEETING_BACKEND_PORT="8002"
scripts\start_backend.ps1
```

也可以一键启动前后端：

```powershell
scripts\start_all.ps1
```

然后打开：

```text
http://127.0.0.1:5173
```

如果后端没有启动，前端会自动进入本地演示模式；后端启动后会优先调用真实 API。

## 测试

```powershell
scripts\run_tests.ps1
node frontend\prototype_spec_test.mjs
```

测试覆盖：

- 敏感词替换
- 待办字段映射到普通会议系统接口
- 字音对齐选区反查音频区间
- 声纹选区注册记录
- Qwen3-ASR mock 网关输出
- 摘要和纪要结构

启动前后端和小模型服务后，可以再跑服务级冒烟：

```powershell
backend\.venv\Scripts\python.exe scripts\smoke_verify_system.py
```

该脚本会检查后端健康状态、智能体工作流配置状态、前端首页、小模型服务、会议创建、
配置库、声纹样本上传、摘要、纪要、待办、翻译、语篇规整和 docx 导出。默认不调用真实
ASR，避免产生百炼费用；最终联调真实转写时再加 `--include-asr`。

## 对接说明

普通会议系统待办保存接口来自用户提供的接口文档：

```text
POST /task/management/meeting/taskSave
```

当前 `todos/push` 接口先返回符合文档字段的 payload，不直接访问外部系统。配置好 `MEETING_SYSTEM_BASE_URL` 和 token 后，可在 `backend/app/main.py` 的 `push_todos` 中加入 HTTP 转发。

## 目录结构

```text
backend/
  app/
    asr_gateway.py          # Qwen3-ASR-1.7B 模型网关
    alignment_service.py    # 字音对照和文本选区音频反查
    voiceprint_service.py   # 声纹注册与匹配
    llm_workflow.py         # 摘要/纪要/翻译/待办工作流适配
    integration_service.py  # 普通会议系统字段映射
    export_service.py       # docx/mp3 导出
    main.py                 # FastAPI 接口
  tests/
frontend/
  index.html
  styles.css
  app.js
```

## Qwen3-ASR API、本地小模型和智能体工作流接入

本地测试阶段建议先使用 DashScope/百炼 Qwen3-ASR API，VAD、声纹、强制对齐用本地 HTTP 小模型服务承载。

### 1. 后端 ASR 配置

```powershell
$env:ASR_GATEWAY_MODE="dashscope"
$env:DASHSCOPE_API_KEY="你的百炼 API Key"
$env:DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com"
$env:DASHSCOPE_SYNC_MODEL="qwen3-asr-flash"
$env:DASHSCOPE_FILETRANS_MODEL="qwen3-asr-flash-filetrans"
scripts\start_backend.ps1
```

说明：
- `qwen3-asr-flash` 用于实时分片和本地小文件同步识别。
- `qwen3-asr-flash-filetrans` 用于可访问 URL 的长音频异步转写。
- `qwen3-asr-flash` 的同步专用任务只接收一个音频项，不支持热词增强；系统仍冻结有效词表用于审计、替换规则和支持 corpus 的 filetrans 路径，不会把文本提示塞进同步 ASR 导致 400。
- 如果后续改成本地 910B / 昇腾服务，切换为 `ASR_GATEWAY_MODE=remote` 并设置 `ASR_GATEWAY_BASE_URL` 即可。

### 2. 本地小模型服务

先以 mock 模式启动，验证智能会议完整流程：

```powershell
scripts\start_model_services.ps1 -Port 8100 -MockMode true
```

智能会议后端配置：

```powershell
$env:VAD_GATEWAY_BASE_URL="http://127.0.0.1:8100"
$env:VOICEPRINT_GATEWAY_BASE_URL="http://127.0.0.1:8100"
$env:ALIGNMENT_GATEWAY_BASE_URL="http://127.0.0.1:8100"
```

真实模型部署时，安装依赖：

```powershell
backend\.venv\Scripts\python.exe -m pip install -r backend\model_services\requirements-models.txt
```

依赖安装后可先预下载 ModelScope 权重，再检查模型服务是否能被后端调用：

```powershell
backend\.venv\Scripts\python.exe scripts\download_model_weights.py
backend\.venv\Scripts\python.exe scripts\check_model_services.py --base-url http://127.0.0.1:8100
backend\.venv\Scripts\python.exe scripts\check_model_services.py --base-url http://127.0.0.1:8100 --deep
```

默认检查只访问 `/v1/health`；`--deep` 会用临时 wav 调用 VAD、声纹注册/匹配和选区对齐接口。
如果要验证真实声纹效果，请通过 `--audio-path` 传入清晰人声样本。

模型职责：
- `FSMN-VAD`：CPU，负责语音活动检测和音频切分。
- `CAM++`：CPU，优先负责声纹注册、声纹匹配、发言人身份识别。
- `3D-Speaker`：CPU，可作为多人说话人分离增强方案。
- `Qwen3-ForcedAligner-0.6B`：GPU，负责字/词级时间戳、字音同步、文本选区反查音频。

### 3. 智能体平台工作流

后端已有 `AgentWorkflowClient`，调用方式与 `shenhe-agent - 0621` 一致：第一次只传 `workflow_id`，平台返回 input 节点后再传 `{node_id: {字段: 值}}`。

需要配置：

```powershell
$env:WORKFLOW_INVOKE_URL="你的智能体平台 /workflow/invoke 地址"
$env:WORKFLOW_BEARER_TOKEN="你的平台 token，或后续改为登录换 token"
$env:WORKFLOW_MEETING_SUMMARY_ID="meeting_summary_workflow 的 ID"
$env:WORKFLOW_MINUTES_ID="meeting_minutes_workflow 的 ID"
$env:WORKFLOW_TODO_ID="todo_extract_workflow 的 ID"
$env:WORKFLOW_TRANSLATE_ID="translate_workflow 的 ID"
$env:WORKFLOW_DISCOURSE_ID="discourse_rewrite_workflow 的 ID"
```

建议工作流输出统一为 JSON：
- 摘要工作流输出 `keywords/topic/overview/keyPoints/decisionItems/riskFlags/todos/speakerSummaries`。
- 纪要工作流输出 `title/templateId/filledFields/content/docxBlocks`。
- 待办工作流输出 `title/content/ownerDept/cooperateDept/dueDate/milestones/taskType/childNodes/completeDate`。
- 翻译工作流输出 `segments: [{segmentId, speakerName, startMs, endMs, sourceText, translatedText}]`。
- 语篇规整工作流输出 `paragraphs/sections/normalizedText`。
