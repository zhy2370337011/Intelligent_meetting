# 智能会议系统模型部署与工作流编排

本文说明本地测试和后续算力服务器部署时要安装哪些模型、启动哪些 HTTP 服务、后端怎么填写 URL，以及智能体平台 5 个工作流应如何编排。

## 1. 当前模型组合是否够用

这套组合可以覆盖当前智能会议系统的核心能力：

- `qwen3-asr-flash / qwen3-asr-flash-filetrans`：先通过百炼 DashScope API 做实时分片识别和离线文件转写。
- `FSMN-VAD`：部署在 CPU，用于实时音频端点检测、静音过滤、长音频切分。
- `CAM++`：部署在 CPU，用于声纹注册、已登记人员身份匹配。
- `3D-Speaker`：部署在 CPU 或单独服务，用于多人会议说话人分离；CAM++ 匹配效果不足时启用。
- `Qwen3-ForcedAligner-0.6B`：部署在 GPU，用于字音对照、选中文本反查音频片段、离线结果精准回听。
- 智能体平台大模型：用于摘要、纪要、待办、翻译、语篇规整、导图、发言人总结。

还必须准备 `ffmpeg`。它不是模型，但音视频导入、统一转 wav/pcm、音频片段裁剪、mp3 导出都需要它。

## 2. 推荐服务器拓扑

开发机本地测试可以把小模型都开在 `127.0.0.1:8100`。正式部署建议拆成 3 个模型服务，方便资源隔离：

| 服务 | 建议地址 | 设备 | 模型 |
| --- | --- | --- | --- |
| VAD 服务 | `http://vad-server:8101` | CPU | FSMN-VAD |
| 声纹服务 | `http://voiceprint-server:8102` | CPU | CAM++ / 3D-Speaker |
| 强制对齐服务 | `http://align-server:8103` | GPU | Qwen3-ForcedAligner-0.6B |
| ASR 服务 | DashScope 或 `http://asr-910b:8104` | API / 910B | Qwen3-ASR |
| 智能体平台 | `http://172.27.16.75:13333/...` | 平台 | 大模型工作流 |

后端只依赖 HTTP URL，所以后续把模型迁到算力服务器时只改 `backend/.env`：

```env
ASR_GATEWAY_MODE=dashscope
VAD_GATEWAY_BASE_URL=http://vad-server:8101
VOICEPRINT_GATEWAY_BASE_URL=http://voiceprint-server:8102
ALIGNMENT_GATEWAY_BASE_URL=http://align-server:8103
# 如果 3D-Speaker 单独部署为多人分离服务，可让小模型服务或主后端直接转发到该地址。
DIARIZATION_BACKEND_URL=http://voiceprint-server:8102
```

如果后续 ASR 也从 DashScope 切到 910B 自部署服务：

```env
ASR_GATEWAY_MODE=remote
ASR_GATEWAY_BASE_URL=http://asr-910b:8104
```

该服务需要实现 `POST /v1/asr/transcribe`，返回智能会议系统统一的 `segments` 结构。

## 3. 本地小模型服务接口

当前仓库提供了统一 HTTP 服务壳：

```powershell
.\scripts\start_model_services.ps1 -Port 8100 -MockMode true
```

安装真实模型依赖后改成：

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -r .\backend\model_services\requirements-models.txt
.\scripts\start_model_services.ps1 -Port 8100 -MockMode false
```

依赖和权重准备完成后，使用下面两个脚本做部署前检查：

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\download_model_weights.py
.\backend\.venv\Scripts\python.exe .\scripts\check_model_services.py --base-url http://127.0.0.1:8100 --deep
```

`check_model_services.py` 默认只检查 `/v1/health`；加 `--deep` 会调用 VAD、声纹注册/匹配和
选区对齐接口，确认主后端配置 `VAD_GATEWAY_BASE_URL/VOICEPRINT_GATEWAY_BASE_URL/ALIGNMENT_GATEWAY_BASE_URL`
后能够真正访问这些本地或算力服务器模型服务。

正式接真实模型时，保持以下接口不变：

- `POST /v1/vad/split`：输入 `audio_path`，输出 `segments: [{start_ms,end_ms,speech}]`。
- `POST /v1/voiceprints/register`：输入 `speaker_id/speaker_name/audio_path`，输出 `embeddingId/status/model`。
- `POST /v1/voiceprints/match`：输入 `audio_path/top_k`，输出最相似的登记发言人。
- `POST /v1/diarize`：输入 `audio_path`，输出多人说话人时间段。
- `POST /v1/align`：输入 `audio_path/transcript_text`，输出字/词级时间戳。
- `POST /v1/align/selection-window`：输入全文和选中文本，输出音频起止时间。

3D-Speaker 本地加载时使用 ModelScope `speaker-diarization` pipeline；正式服务器上也可以把
`DIARIZATION_BACKEND_URL` 指向独立 3D-Speaker 服务。该服务已经按统一协议返回
`segments: [{speaker,start_ms,end_ms}]`，所以前端和业务 API 不需要关心底层是本地模型还是远程模型。

## 4. 五个智能体工作流编排

后端已读取 `E:\work\my-todo\shenhe-agent - 0621` 同类配置：

- `WORKFLOW_INVOKE_URL=http://172.27.16.75:13333/bsapp/api/v2/workflow/invoke`
- `LOGIN_URL=http://172.27.16.75:13333/bsapp/api/v1/thirdParty/app/login`
- RSA 登录换 token 方式与该项目保持一致。

工作流启用条件：

```env
LLM_WORKFLOW_MODE=remote
WORKFLOW_MEETING_SUMMARY_ID=你的摘要工作流ID
WORKFLOW_MINUTES_ID=你的纪要工作流ID
WORKFLOW_TODO_ID=你的待办工作流ID
WORKFLOW_TRANSLATE_ID=你的翻译工作流ID
WORKFLOW_DISCOURSE_ID=你的语篇规整工作流ID
```

### 4.1 meeting_summary_workflow

节点编排：

1. 输入节点：接收 `meeting_meta/transcript_segments/keyword_libraries/sensitive_hits`。
2. 代码节点：按时间排序，合并连续同发言人片段，生成全文和发言人分组文本。
3. LLM 节点：抽取关键词、主题概要、会议要点、决策事项、风险提醒、待办草案、发言人总结。
4. JSON 校验节点：强制输出下面结构。

输出：

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

### 4.2 meeting_minutes_workflow

节点编排：

1. 输入节点：接收 `meeting_meta/transcript_segments/summary/template`。
2. 模板解析节点：读取 `template.content` 和 `template.tagBindings`。
3. LLM 节点：根据模板标签填充会议主题、时间、地点、参会人、纪要正文和待办。
4. 渲染节点：输出可编辑正文和 `filledFields`，后续用于 docx 导出。

输出：

```json
{
  "title": "",
  "templateId": "",
  "filledFields": {},
  "content": "",
  "docxBlocks": []
}
```

### 4.3 todo_extract_workflow

节点编排：

1. 输入节点：接收会议元数据、转写片段和摘要。
2. LLM 节点：抽取“谁负责、做什么、何时完成、协同部门、里程碑”。
3. 规则节点：把日期标准化为 `YYYY-MM-DD`，缺失负责人标记为待确认。
4. 映射节点：生成普通会议系统 `/task/management/meeting/taskSave` 可用字段。

输出：

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

### 4.4 translate_workflow

节点编排：

1. 输入节点：接收 `direction` 和 `segments`。
2. 分批节点：长会议按片段批处理，保留 `segmentId/speakerName/startMs/endMs`。
3. LLM 节点：逐句翻译，不改写事实和数字。
4. 合并节点：按原始时间戳回填翻译结果。

输出：

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

### 4.5 discourse_rewrite_workflow

节点编排：

1. 输入节点：接收 `transcript_text/meeting_meta/style`。
2. 清洗节点：删除口头语、重复词、明显语气词。
3. 结构节点：按议题、结论、风险、待办重排段落。
4. LLM 节点：输出适合政企会议阅读的规整稿。

输出：

```json
{
  "normalizedText": "",
  "sections": [{"title": "", "content": ""}],
  "paragraphs": []
}
```

## 5. 你需要在服务器安装的东西

基础依赖：

- Python 3.10+。
- `ffmpeg`。
- `torch`、`torchaudio`。
- `funasr`、`modelscope`。
- `fastapi`、`uvicorn`、`python-multipart`。
- `numpy`、`soundfile`。
- `addict`、`simplejson`、`sortedcontainers`、`datasets`、`pillow`、`hdbscan`，用于 ModelScope
  `speaker-diarization` pipeline 和 CAM++ 聚类式多人分离。

模型：

- 百炼/DashScope：先不装 Qwen3-ASR 权重，只配置 `DASHSCOPE_API_KEY`。
- `FSMN-VAD`：CPU。
- `CAM++`：CPU。
- `3D-Speaker`：CPU，做多人 diarization 增强。
- `Qwen3-ForcedAligner-0.6B`：GPU。

3D-Speaker 还会间接加载以下组件，服务器离线部署时也要提前缓存：

- `damo/speech_campplus_sv_zh-cn_16k-common`。
- `damo/speech_fsmn_vad_zh-cn-16k-common-pytorch`。
- `damo/speech_campplus-transformer_scl_zh-cn_16k-common`。

后续完全内网部署时再补：

- `Qwen3-ASR-1.7B` 自部署服务，优先放 910B-4。
- 文件访问服务或对象存储，例如 Nginx/MinIO，用于长音频 filetrans 或模型服务跨机读取音频。
- 任务队列，例如 Celery/RQ/Arq，用于长任务并发和失败重试。

## 6. 当前还需要你提供

- 五个智能体平台 workflow id。
- 如果要关闭 mock 模式：确认小模型服务已经部署并返回真实推理结果。
- 普通会议系统地址和 token，用于真正推送 `/task/management/meeting/taskSave`。

## 7. 当前本地实测状态

截至本轮联调，本地开发机已经完成以下验证：

- `GET /v1/health`：小模型服务正常，`LOCAL_MODEL_MOCK_MODE=false`。
- `POST /v1/vad/split`：FSMN-VAD 使用 ModelScope 示例音频返回 8 个语音片段。
- `POST /v1/voiceprints/register`：CAM++ 可以生成 embedding 并返回 `registered`。
- `POST /v1/voiceprints/match`：CAM++ 可以对同一说话人相近样本返回相似度。
- `POST /v1/diarize`：3D-Speaker 示例音频返回 5 个说话人分离片段。
- `POST /v1/align`：当前只保留强制对齐代理入口；真正的 `Qwen3-ForcedAligner-0.6B` GPU 服务地址尚未填写。

智能体平台当前状态：

- `WORKFLOW_INVOKE_URL` 和 `LOGIN_URL` 已按 `shenhe-agent - 0621` 方式接入。
- 后端已支持 RSA 登录换 token，也支持直接填写 `WORKFLOW_BEARER_TOKEN`。
- 5 个业务工作流 ID 尚未填写，所以当前 `LLM_WORKFLOW_MODE=mock`。
- 配置完成后访问 `/api/workflows/status`，确认 `remoteReady=true`；如果仍为 false，查看
  `fallbackReason` 可定位是模式、invoke 地址、认证材料还是 5 个 workflow id 未配置。

建议服务器部署时把环境变量写成更语义化的长名：

```env
WORKFLOW_MEETING_SUMMARY_ID=摘要工作流ID
WORKFLOW_MEETING_MINUTES_ID=纪要工作流ID
WORKFLOW_TODO_EXTRACT_ID=待办工作流ID
WORKFLOW_TRANSLATE_ID=翻译工作流ID
WORKFLOW_DISCOURSE_REWRITE_ID=语篇规整工作流ID
```

后端同时兼容旧短名 `WORKFLOW_MINUTES_ID / WORKFLOW_TODO_ID / WORKFLOW_DISCOURSE_ID`。
