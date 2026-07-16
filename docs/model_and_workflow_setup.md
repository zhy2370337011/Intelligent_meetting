# 本地模型与智能体工作流配置说明

## 一、模型组合是否够用

当前组合可以覆盖智能会议系统 V1 的核心能力：

- Qwen3-ASR API：中文普通话、英文、中英混合实时/离线转写。
- FSMN-VAD：实时音频流端点检测、离线音频切分、长音频分片。
- CAM++：声纹注册、发言人身份匹配、已登记人员识别。
- 3D-Speaker：多人会议说话人分离增强，建议在 CAM++ 匹配效果不足时启用。
- Qwen3-ForcedAligner-0.6B：字音同步、选中文本反查音频片段、精确回听。
- 智能体平台大模型：翻译、摘要、纪要、待办、语篇规整、导图、发言人总结。

额外必须准备的是 `ffmpeg`，用于音视频格式统一转 wav/pcm/mp3。

## 二、后端环境变量

```powershell
# 主 ASR：本地测试先走 DashScope API。
$env:ASR_GATEWAY_MODE="dashscope"
$env:DASHSCOPE_API_KEY="由你输入"
$env:DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com"
$env:DASHSCOPE_SYNC_MODEL="qwen3-asr-flash"
$env:DASHSCOPE_FILETRANS_MODEL="qwen3-asr-flash-filetrans"

# 本地小模型服务。可以先全部指向 scripts/start_model_services.ps1 启动的 8100 端口。
$env:VAD_GATEWAY_BASE_URL="http://127.0.0.1:8100"
$env:VOICEPRINT_GATEWAY_BASE_URL="http://127.0.0.1:8100"
$env:ALIGNMENT_GATEWAY_BASE_URL="http://127.0.0.1:8100"

# 智能体平台工作流。
$env:WORKFLOW_INVOKE_URL="你的智能体平台 /workflow/invoke 地址"
$env:WORKFLOW_BEARER_TOKEN="由你输入或后续改成登录换 token"
$env:WORKFLOW_MEETING_SUMMARY_ID="meeting_summary_workflow 的 ID"
$env:WORKFLOW_MINUTES_ID="meeting_minutes_workflow 的 ID"
$env:WORKFLOW_TODO_ID="todo_extract_workflow 的 ID"
$env:WORKFLOW_TRANSLATE_ID="translate_workflow 的 ID"
$env:WORKFLOW_DISCOURSE_ID="discourse_rewrite_workflow 的 ID"
```

## 三、本地小模型服务接口

启动：

```powershell
scripts\start_model_services.ps1 -Port 8100 -MockMode true
```

真实模型依赖：

```powershell
backend\.venv\Scripts\python.exe -m pip install -r backend\model_services\requirements-models.txt
```

接口约定：

- `POST /v1/vad/split`
  - 输入：`audio_path/min_speech_ms/max_segment_ms`
  - 输出：`segments: [{start_ms, end_ms, speech}]`
- `POST /v1/voiceprints/register`
  - 输入：`speaker_id/speaker_name/audio_path/metadata`
  - 输出：`embeddingId/status/model`
- `POST /v1/voiceprints/match`
  - 输入：`audio_path/top_k`
  - 输出：`speakerId/speakerName/confidence/matches`
- `POST /v1/diarize`
  - 输入：`audio_path/min_speakers/max_speakers`
  - 输出：`segments: [{speaker,start_ms,end_ms}]`
- `POST /v1/align`
  - 输入：`audio_path/transcript_text/language`
  - 输出：`words: [{text,start_ms,end_ms}]`
- `POST /v1/align/selection-window`
  - 输入：`audio_path/transcript_text/selected_text/padding_ms`
  - 输出：`start_ms/end_ms/words`

## 四、智能体工作流编排

### 1. meeting_summary_workflow

输入字段：

```json
{
  "meeting_meta": {"meetingName": "", "createdAt": "", "location": ""},
  "transcript_segments": [{"speakerName": "", "startMs": 0, "endMs": 0, "text": ""}],
  "keyword_libraries": [{"name": "", "words": []}],
  "sensitive_hits": [{"word": "", "segmentId": ""}]
}
```

输出字段：

```json
{
  "keywords": [],
  "topic": "",
  "overview": "",
  "keyPoints": [],
  "decisionItems": [],
  "riskFlags": [],
  "todos": [],
  "speakerSummaries": []
}
```

### 2. meeting_minutes_workflow

输入字段：

```json
{
  "meeting_meta": {},
  "transcript_segments": [],
  "summary": {},
  "template": {"id": "", "name": "", "content": "", "tagBindings": []}
}
```

输出字段：

```json
{
  "title": "",
  "templateId": "",
  "filledFields": {},
  "content": "",
  "docxBlocks": []
}
```

### 3. todo_extract_workflow

输入字段：

```json
{
  "meeting_meta": {},
  "transcript_segments": [],
  "summary": {}
}
```

输出字段：

```json
{
  "todos": [
    {
      "title": "",
      "content": "",
      "ownerDept": "",
      "cooperateDept": "",
      "dueDate": "",
      "taskType": "TaskManagement",
      "childNodes": [{"majorTime": "", "nodeContent": ""}],
      "completeDate": ""
    }
  ]
}
```

### 4. translate_workflow

输入字段：

```json
{
  "direction": "zh-en",
  "segments": [{"segmentId": "", "speakerName": "", "startMs": 0, "endMs": 0, "text": ""}]
}
```

输出字段：

```json
{
  "segments": [
    {
      "segmentId": "",
      "speakerName": "",
      "startMs": 0,
      "endMs": 0,
      "sourceText": "",
      "translatedText": ""
    }
  ]
}
```

### 5. discourse_rewrite_workflow

输入字段：

```json
{
  "transcript_text": "",
  "meeting_meta": {},
  "style": "政企会议纪要"
}
```

输出字段：

```json
{
  "normalizedText": "",
  "sections": [{"title": "", "content": ""}],
  "paragraphs": []
}
```

## 五、后续需要你提供的信息

- DashScope/百炼 `DASHSCOPE_API_KEY`。
- 智能体平台 `WORKFLOW_INVOKE_URL`。
- 智能体平台 token 或登录换 token 所需账号配置。
- 五个工作流 ID。
- 如果要推送普通会议系统：`MEETING_SYSTEM_BASE_URL` 和 token。
