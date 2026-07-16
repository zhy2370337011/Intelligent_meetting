# 实时会议稳定性修复实施计划

**目标：** 修复上下文回声、重复实时会话、碎片化分段、说话人能力假健康、空态布局和 AI 内部字段泄漏。

**约束：** 实时与导入严格隔离；历史数据非破坏性处理；详细注释；测试先行；不使用递归或批量删除。

## Task 1：失败测试

- [ ] 后端：纯 corpus 回声不落库，正常句子保留。
- [ ] 后端：默认/配置 `silence_duration_ms` 为 1200ms 且合法钳制。
- [ ] 后端：同一 meeting 新 lease 使旧 session 无权落库，旧 release 不释放新 lease。
- [ ] 后端：AI 输入跳过历史纯上下文回声。
- [ ] 后端：voiceprint ready 需要 embedding capability。
- [ ] 前端：connecting 防重、socket 身份守卫、四行网格、渐进式控件和 AI 内部字段隐藏。

## Task 2：后端实现

- [ ] 收敛 realtime corpus，并增加精确 context-only echo 判定。
- [ ] 引入 meeting-scoped realtime lease，所有 final 落库前校验所有权。
- [ ] 将 endpointing 配置透传到 DashScope session，默认 1200ms。
- [ ] 清理 AI 输入中的历史纯回声，保留数据库原始记录。
- [ ] 强化模型健康契约，区分模型加载与 embedding API 能力。

## Task 3：前端实现

- [ ] 增加 `realtimeConnecting`，所有异步回调按 socket/token 做身份守卫。
- [ ] 修复编辑器四行网格和富文本工具栏顶部定位。
- [ ] 空态隐藏搜索/筛选，单发言人隐藏筛选，移除假“已保存”。
- [ ] 过滤历史纯上下文回声显示。
- [ ] 摘要、纪要、待办、规整、标记统一隐藏内部审计标识。

## Task 4：自动化回归

- [ ] `node --check frontend\app.js`
- [ ] `node frontend\prototype_spec_test.mjs`
- [ ] 运行新增后端定向测试。
- [ ] `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1`

## Task 5：真实运行态验收

- [ ] 重启 5173/8001/8100 到最新源码。
- [ ] 检查 8100 OpenAPI 包含 `/v1/speakers/embedding`。
- [ ] 使用真实 WAV 执行 embedding 探针。
- [ ] 浏览器验证空态、短停顿、AI 展示和无重复 session。
- [ ] 使用两人音频或确定性 fixture 验证发言人列表可出现多个稳定身份。
