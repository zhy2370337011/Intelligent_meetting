# 智能会议系统智能体平台工作流编排说明

本文档说明智能会议系统中 5 个大模型工作流如何在智能体平台编排，并说明后端如何调用它们。
后端调用协议已经参考 `E:\work\my-todo\shenhe-agent - 0621` 完成适配：

1. 后端先调用 `LOGIN_URL`，用 RSA 公钥加密 `PASSWORD` 获取 `access_token`。
2. 后端调用 `WORKFLOW_INVOKE_URL`，请求头使用 `Authorization: Bearer <access_token>`。
3. 第一次 invoke 只传 `workflow_id`，平台返回 input 节点后，后端再按 `{node_id: {字段: 值}}` 回填业务数据。
4. 工作流最终必须输出 JSON，后端会解析 `output_msg` 中的 JSON 入库并返回给前端。

> 当前本地 `.env` 已配置平台登录地址、invoke 地址和 DashScope Key。5 个业务 workflow id 仍需要在平台创建后填入。

## 后端环境变量

```env
LLM_WORKFLOW_MODE=remote
WORKFLOW_INVOKE_URL=http://172.27.16.75:13333/bsapp/api/v2/workflow/invoke
LOGIN_URL=http://172.27.16.75:13333/bsapp/api/v1/thirdParty/app/login
COMPANY_ID=111
COMPANY_INFO=华电
EMPLOYEE_INFO=员工
USER_NAME=admin
PASSWORD=你的平台密码
RSA_PUBLIC_KEY_PATH=E:\work\my-todo\shenhe-agent - 0621\backend\app\rsa_public.pem

WORKFLOW_MEETING_SUMMARY_ID=摘要工作流ID
WORKFLOW_MEETING_MINUTES_ID=纪要工作流ID
WORKFLOW_TODO_EXTRACT_ID=待办工作流ID
WORKFLOW_TRANSLATE_ID=翻译工作流ID
WORKFLOW_DISCOURSE_REWRITE_ID=语篇规整工作流ID
```

后端也兼容旧变量名：

```env
WORKFLOW_MINUTES_ID=纪要工作流ID
WORKFLOW_TODO_ID=待办工作流ID
WORKFLOW_DISCOURSE_ID=语篇规整工作流ID
```

配置完成后访问：

```text
GET http://127.0.0.1:8001/api/workflows/status
```

当返回 `remoteReady=true` 时，5 个工作流才会真正走智能体平台。当前没有 5 个 workflow id 时，
接口会返回 `remoteReady=false` 和 `fallbackReason`，业务接口仍走 mock/fallback，保证前端完整可用。

## 1. meeting_summary_workflow

用途：生成会议关键词、主题概要、会议要点、决策事项、风险提醒、发言人总结。

后端输入：

```json
{
  "meeting_meta": {
    "meetingId": "rec-001",
    "meetingName": "项目例会",
    "createdAt": "2026-06-26 09:30:00",
    "location": "第一会议室",
    "creator": "管理员"
  },
  "transcript_segments": [
    {
      "id": "seg-1",
      "speakerName": "王忠",
      "startMs": 1000,
      "endMs": 8200,
      "text": "请信息中心完成模型网关联调。"
    }
  ],
  "keyword_libraries": ["政务会议词库", "智能会议技术词库"],
  "sensitive_hits": []
}
```

推荐节点：

1. 输入节点：接收 `meeting_meta/transcript_segments/keyword_libraries/sensitive_hits`。
2. 代码节点：按时间排序，合并转写全文，按发言人聚合发言内容，过滤空片段。
3. LLM 节点：提取结构化摘要，要求只输出 JSON。
4. JSON 校验节点：校验必填字段，不合格时让 LLM 修复一次。
5. 输出节点：返回下面的 JSON。

LLM 输出 JSON：

```json
{
  "keywords": ["模型网关", "声纹注册"],
  "topic": "智能会议系统建设与联调",
  "overview": "本次会议围绕 ASR、声纹、纪要和待办推送展开。",
  "keyPoints": ["确认 ASR 先走 DashScope API。"],
  "decisionItems": ["优先部署 FSMN-VAD、CAM++、3D-Speaker。"],
  "riskFlags": ["涉密会议不能调用公网 ASR。"],
  "todos": [
    {
      "title": "完成模型网关联调",
      "content": "完成 ASR、声纹、强制对齐接口联调。",
      "ownerDept": "信息中心",
      "cooperateDept": "办公室",
      "dueDate": "2026-08-31",
      "milestones": [{"time": "2026-08-31", "content": "完成接口联调"}]
    }
  ],
  "speakerSummaries": [
    {"speakerName": "王忠", "summary": "提出模型联调安排。"}
  ]
}
```

## 2. meeting_minutes_workflow

用途：结合会议内容和纪要模板自动填充“会议主题、时间、地点、参会人、纪要正文、待办”等模板标签。

后端输入：

```json
{
  "meeting_meta": {
    "meetingId": "rec-001",
    "meetingName": "项目例会",
    "createdAt": "2026-06-26 09:30:00",
    "location": "第一会议室"
  },
  "transcript_segments": [],
  "summary": {},
  "template": {
    "name": "企业会议纪要模板",
    "content": "会议主题：{{会议主题}}\n会议纪要：{{会议纪要}}",
    "tagBindings": {
      "会议主题": "meeting_topic",
      "会议纪要": "minutes_body"
    }
  }
}
```

推荐节点：

1. 输入节点：接收会议元数据、全文、摘要、模板内容、模板标签绑定。
2. 代码节点：把 `transcript_segments` 合并为 `transcript_text`，把模板标签展开成清单。
3. LLM 节点：按模板风格生成纪要，严禁编造不存在的参会人和时间。
4. 代码节点：把输出回填到 `tagValues`，生成可编辑正文。
5. 输出节点。

输出 JSON：

```json
{
  "title": "项目例会会议纪要",
  "templateName": "企业会议纪要模板",
  "content": "会议主题：智能会议系统建设\n会议纪要：……",
  "tagValues": {
    "会议主题": "智能会议系统建设",
    "会议时间": "2026-06-26 09:30:00",
    "会议地点": "第一会议室",
    "参会人": "王忠、薛总",
    "会议纪要": "……",
    "会议待办": "……"
  },
  "docxBlocks": [
    {"type": "paragraph", "text": "项目例会会议纪要"}
  ]
}
```

## 3. todo_extract_workflow

用途：抽取待办，并生成普通会议系统 `/task/management/meeting/taskSave` 可映射字段。

后端输入：

```json
{
  "meeting_meta": {"meetingId": "rec-001", "meetingName": "项目例会"},
  "transcript_segments": [],
  "summary": {}
}
```

推荐节点：

1. 输入节点：接收会议全文、摘要和决策事项。
2. LLM 节点：抽取明确行动项，识别负责人、协同部门、截止时间、优先级。
3. 代码节点：补齐普通会议系统字段，如 `taskType/childNodes/completeDate`。
4. 输出节点。

输出 JSON：

```json
{
  "todos": [
    {
      "title": "完成模型网关联调",
      "content": "完成 ASR、声纹、强制对齐接口联调。",
      "ownerDept": "信息中心",
      "ownerName": "张三",
      "cooperateDept": "办公室",
      "priority": "high",
      "dueDate": "2026-08-31",
      "taskType": "meeting",
      "completeDate": "2026-08-31",
      "childNodes": [
        {"time": "2026-07-15", "content": "完成接口自测"},
        {"time": "2026-08-31", "content": "完成联调验收"}
      ]
    }
  ]
}
```

## 4. translate_workflow

用途：中英互译，保留原发言人和时间戳。

后端输入：

```json
{
  "direction": "zh-en",
  "segments": [
    {"segmentId": "seg-1", "speakerName": "王忠", "startMs": 1000, "text": "请完成模型联调。"}
  ]
}
```

推荐节点：

1. 输入节点：接收 `direction/segments`。
2. LLM 节点：逐句翻译，保留专有名词，如 Qwen3-ASR、CAM++、KingbaseES。
3. 代码节点：把译文按原 segmentId 回填。
4. 输出节点。

输出 JSON：

```json
{
  "segments": [
    {
      "segmentId": "seg-1",
      "speakerName": "王忠",
      "startMs": 1000,
      "sourceText": "请完成模型联调。",
      "translatedText": "Please complete the model integration testing."
    }
  ],
  "text": "Please complete the model integration testing."
}
```

## 5. discourse_rewrite_workflow

用途：对口语化转写做语篇规整，包括去口头禅、重排段落、生成标题和章节。

后端输入：

```json
{
  "transcript_text": "嗯今天我们主要说一下模型部署然后这个声纹注册也要做。",
  "meeting_meta": {},
  "style": "政企会议纪要"
}
```

推荐节点：

1. 输入节点：接收原始口语化文本。
2. 代码节点：基础清洗，保留数字、专有名词和人名。
3. LLM 节点：按政企会议文风重写，保持原意，不新增事实。
4. JSON 校验节点：检查 `normalizedText/sections`。
5. 输出节点。

输出 JSON：

```json
{
  "title": "模型部署与声纹注册工作安排",
  "normalizedText": "本次会议主要讨论模型部署和声纹注册工作。会议要求完成相关服务部署，并推进声纹注册能力联调。",
  "sections": [
    {
      "heading": "一、模型部署",
      "content": "完成 ASR、VAD、声纹和强制对齐服务部署。"
    }
  ]
}
```

## 验证方式

1. 本地 mock 验证：

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\smoke_verify_system.py
```

该脚本覆盖摘要、纪要、待办、翻译、语篇规整和 docx 导出。默认不调用真实 ASR；
需要最终验证百炼转写链路时再添加 `--include-asr`。

2. 查看工作流配置：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/workflows/status
```

3. 切换真实智能体平台：

```env
LLM_WORKFLOW_MODE=remote
```

然后重启后端，再调用摘要、纪要、待办、翻译、语篇规整接口。

4. 检查小模型服务：

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\check_model_services.py --base-url http://127.0.0.1:8100
.\backend\.venv\Scripts\python.exe .\scripts\check_model_services.py --base-url http://127.0.0.1:8100 --deep
```
