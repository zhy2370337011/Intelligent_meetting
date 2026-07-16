# HANDOFF - 智慧会议系统联调交接文档

> **最高优先级声明（请新会话首先阅读）**
>
> 本文件合并了三个不同时期的对话交接内容。**当前这次对话是时间上最新、验证最完整的一次；文档中任何旧结论与下方“0. 最新会话权威修订”冲突时，一律以下方最新修订为准。**旧内容仅用于理解历史背景和已经尝试过的方案，不能直接当成当前实现规格。
>
> 优先级顺序：**“0. 最新会话权威修订” > 本文件其他历史章节 > 更早的截图或口头方案**。如果文档和实际代码仍不一致，应先运行测试、健康检查并复现，再以当前代码和最新验证证据为准，禁止凭旧文档盲改。

更新时间：2026-07-15（本次会话最终交付后更新）
工作区：`E:\work\my-todo\intelligent meetting`

这份文档写给完全没有上下文的新会话。请先读完再继续改代码。

## 0. 最新会话权威修订（冲突时以本节为准）

### 0.00 本次会话最终交付状态（最高优先级）

本小节是整份文档中时间最新的结论。若 `0.1` 以后或历史章节存在不同端口、固定时长切片、旧健康契约、旧测试数量等说法，全部以本小节和 `0.0` 为准。

本轮针对用户 2026-07-15 提出的六项实时会议问题，已经完成以下修复：

1. 实时 ASR 上下文不再把“智能转写、声纹注册、强制对齐”等泛功能词作为有效识别结果落库。前端展示和 AI 输入还会过滤历史上下文回显，但不会擅自删除数据库里的原始审计记录。
2. 修复同一会议重复启动、旧 WebSocket 回调继续落库导致的重复片段、覆盖和状态错乱。前端使用 connecting/socket/token 身份守卫，后端使用 meeting 级单活 lease；当前 lease 是进程内实现，多 worker 部署前必须迁移到 Redis 或数据库。
3. 发言人能力探测不再只看模型服务进程是否存活，而会验证 `/v1/speakers/embedding`。真实 CAM++ 探针已返回 192 维向量，`realModel=true`、无 fallback。多人最终能否稳定显示多个发言人仍受实际音色、录音设备和分离结果影响，需要真实双人交替讲话验收，不能用单人自动化测试宣称完全通过。
4. 空实时会议布局已修正：工具栏固定在正文顶部；没有转写时隐藏搜索、发言人筛选和虚假的“已保存”；正文占据剩余高度；播放器固定在底部。截图验收文件为 `test-results/realtime-stability-audit/empty-realtime-detail-1600x1000.png`。
5. DashScope native realtime 默认静音端点为 `1200ms`，并读取前端配置后限制在合法范围。禁止恢复为 400ms，也不要重新引入 3 秒或 8 秒固定 flush 作为主切分方式；最大时长只能作为兜底。
6. 摘要、纪要、待办、规整和标记均不再显示 `Revision`、`rt-rec-*`、segment id、source range 等内部字段。后端仍保留审计数据，前端只展示面向用户的内容。

本次最终验证结果：

- `node --check frontend\app.js`：通过。
- `node frontend\prototype_spec_test.mjs`：`prototype spec ok`。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_tests.ps1`：系统 smoke 通过，后端 `Ran 205 tests`、`OK`。
- `node scripts\browser_realtime_stability_audit.mjs`：`REALTIME BROWSER AUDIT OK`；测试创建的临时会议已通过公开 DELETE API 清理。
- 真实模型服务探针：embedding 192 维，耗时约 1.7 秒；8001 报告 voiceprint ready。

本次最终运行地址：

```text
前端：http://127.0.0.1:5173/
后端：http://127.0.0.1:8001/
模型服务：http://127.0.0.1:8100/
联调地址：http://127.0.0.1:5173/?api=http://127.0.0.1:8001
健康检查：http://127.0.0.1:8001/api/health
```

最终运行态曾验证为：前端 PID 31848、后端 PID 31616、模型服务 PID 8840。PID 只代表当时进程，接手时必须重新检查端口和 health，不能把这些 PID 当成长期固定值。

当前没有代码阻塞。下一位接手者的首要任务是使用真实两人、同一麦克风环境进行至少 2 分钟交替讲话验收，观察发言人切换、短停顿切段、重复片段和端到端延迟；若失败，先保存浏览器与后端诊断数据再修改门限，禁止凭感觉继续调参。

### 0.0 2026-07-15 最新实时会议稳定性修订

本小节晚于下方 0.1-0.6 以及历史章节，发生冲突时优先使用本小节。

- 当前联调固定使用 `http://127.0.0.1:5173/?api=http://127.0.0.1:8001`，业务后端为 8001，模型服务为 8100。历史 8011 进程不代表当前源码。
- 实时 corpus 已排除“智能转写、声纹注册、强制对齐”等泛功能词；只由上下文词组成的 final 不落库。历史污染片段只在展示和 AI 输入副本中过滤，数据库原始审计记录不自动删除。
- 同一 meeting 使用前端 connecting/socket/token 守卫和后端进程内单活 lease。后来连接接管后，旧连接的 partial、final、音频和 speaker update 都不能落库；旧 finally 不能释放新 lease。多 worker 部署需要把 lease 迁移到 Redis/数据库。
- DashScope native realtime 的 `silence_duration_ms` 默认改为 1200ms，并读取前端配置后钳制到供应商合法范围，不能再恢复硬编码 400ms。
- 8100 健康契约新增 `capabilities.voiceprint.embeddingReady`。启动脚本禁止复用缺少该字段的旧进程；深度巡检会直接调用 `/v1/speakers/embedding`。
- 2026-07-15 真实运行态验证：8100 OpenAPI 已包含 embedding 路由，现有会议 WAV 返回 192 维 CAM++ 向量，`realModel=true`，无 fallback，耗时约 1.7 秒；8001 报告 voiceprint ready。
- 空实时会议采用显式四行网格：筛选栏、格式栏、正文、播放器。没有转写时隐藏搜索栏，少于两名发言人时隐藏筛选，删除没有真实状态来源的“已保存”。四个子区必须绑定固定 grid row，避免 hidden 后播放器占据正文 `1fr`。
- 摘要、纪要、待办、规整、标记不再显示 `Revision`、`rt-rec-*`、segment id 或 source range；审计字段仍保留在后端。
- 前端资源缓存版本为 `realtime-stability-20260715a`。如果浏览器仍显示旧布局，先确认 HTML 已加载该版本参数，不能继续在旧缓存页面上判断修复无效。
- 最终自动化结果：前端语法/契约通过，系统 smoke 通过，后端 `Ran 205 tests`、`OK`；专用 Edge CDP 验收通过，截图在 `test-results/realtime-stability-audit/empty-realtime-detail-1600x1000.png`。

最新实现与测试说明见：

- `docs/superpowers/specs/2026-07-15-realtime-meeting-stability-design.md`
- `docs/superpowers/plans/2026-07-15-realtime-meeting-stability.md`

### 0.1 业务边界

- **实时会议转写和导入音视频转写是两个独立功能。**两者可以复用编辑器、发言人列表和 AI 工具，但会议记录、转写片段、详情上下文、AI 草稿及运行状态必须严格隔离。
- 导入转写台账页只保留文件列表以及“查看/下载/删除”。**台账页底部不能直接展示转写片段卡片**；用户通过“查看”进入详情阅读和编辑转写。
- 新建实时会议没有转写内容时，右侧摘要、纪要、待办等工具只能显示空态，不能复用上一次导入转写的结果。

因此，后文如果出现“导入页面直接展示结果片段”“实时会议可以回退到第一条导入记录”等描述，均为旧方案，不再适用。

### 0.2 当前切分策略

- 后文所述“实时固定约 8 秒 flush”是历史方案，**当前目标实现是 VAD/静音端点优先、最大时长兜底**。
- 实时转写参考参数：连续静音约 `1200ms` 落段、疑似句末静音约 `800ms`、单段最大约 `25000ms`、边界 overlap 约 `300ms`。
- 导入长音频优先使用现有 `LocalVadClient.split()` / `/v1/vad/split`；相邻短间隔语音合并、首尾增加 padding、超长语音再拆分。VAD 不可用时才回退 ffmpeg 固定切片。
- 新的最终识别结果必须追加为独立 segment，不能覆盖上一条结果；upsert 必须使用稳定且唯一的 segment id。

### 0.3 当前服务地址

当前权威联调地址如下，与 `0.00` 保持一致：

```text
前端：http://127.0.0.1:5173/
后端：http://127.0.0.1:8001/
模型服务：http://127.0.0.1:8100/
联调地址：http://127.0.0.1:5173/?api=http://127.0.0.1:8001
健康检查：http://127.0.0.1:8001/api/health
```

`8011` 只属于历史联调记录，当前不得使用。接手时仍需检查端口占用和浏览器实际 API 参数；页面能打开不代表连接的是最新后端。

### 0.4 最新修复：DashScope 语言参数

最新用户截图中的导入失败为：

```text
HTTP 400 InvalidParameter: Language code '中文普通话' is not recognized
```

根因是前端把显示文案“中文普通话”直接提交给 DashScope，而供应商只接受 `zh`、`en` 等机器代码。

已完成：

- 新增 `backend/app/asr_language.py`，在供应商边界统一归一化语言。
- `中文普通话/普通话/中文 -> zh`，`英文/英语 -> en`，`中英混合/自动检测 -> auto`。
- `zh-CN -> zh`、`en-US -> en`；未知的人类显示标签回退 `auto`。
- `backend/app/asr_gateway.py` 和 `backend/app/realtime_stream.py` 都执行归一化，兼容历史数据库和冻结的 processing config。
- `frontend/index.html` 中导入与创建会议的语言选项使用 `value="zh"`、`value="en"`、`value="auto"`。
- `frontend/app.js` 导入默认值使用 `zh`。
- 前端缓存版本更新为 `app.js?v=asr-language-20260714a`。
- 参数类 `HTTP 400` 只请求一次，不再错误显示“已重试 3 次”；只有网络瞬断等瞬态错误才重试，并报告真实尝试次数。

不要把后端所有领域默认语言直接改成 `zh`。之前这样做会意外激活默认 zh 识别优化词并改变业务筛选语义。正确方式是保留领域层兼容值，在供应商适配层归一化。

### 0.5 DashScope 语言参数修复的历史验证证据

- 使用真实 DashScope 和精确旧值“中文普通话”调用公开导入接口，返回 HTTP 200，并生成 4 个转写 segment。
- 验证创建的临时会议已经通过公开 DELETE API 清理，没有改动用户历史记录。
- 当时完整回归为：`prototype spec ok`、`SMOKE OK`、后端 `Ran 167 tests`、`OK`。这是语言参数修复阶段的历史数量；当前最终基线是 `0.00` 中的 205 个测试。
- 当时曾使用 `8011` 做临时语言参数联调；该端口现已废弃，当前统一使用 `8001`。

截图中的旧失败导入记录是历史任务结果，修复后不会自动重跑。应让用户重新导入原音频验证，不能擅自删除或改写用户历史数据。

### 0.6 当前下一步

1. 用真实两人、同一麦克风做不少于 2 分钟的实时会议测试，覆盖交替讲话、连续讲话、短停顿、长停顿、暂停后继续和结束会议。
2. 检查发言人列表是否能稳定出现多个人；若仍只有一人，采集 diarization speaker key、embedding 距离、阈值和音频窗口，不要直接伪造第二个发言人。
3. 如果实时仍慢，分别记录浏览器端点触发、WebSocket 发送、后端接收、DashScope 首包/终包、segment 落库和前端渲染时间，先定位延迟阶段再修改。
4. 重新导入用户原 MP3，确认请求 language 为 `zh`、台账完成且“查看”进入导入详情，避免语言参数修复回归。
5. 每次修改先补失败测试，完成后运行 `scripts/run_tests.ps1` 和 `scripts/browser_realtime_stability_audit.mjs`，再做真实浏览器 smoke。

## 1. 我们在做什么任务

用户要把“智慧会议系统”做到前后端可直接使用，重点是：

1. 导入转写：上传音频后能真正识别，识别完成后结果留在“导入转写”页面展示，不要自动跳到“实时会议”。
2. 实时会议：开始会议/继续转写要可用，转写结果要尽量是完整句子，不能大量碎片化。
3. 声纹识别：用户希望提前录入声纹；会议中如果命中声纹，自动显示真实姓名和部门/职位；没命中时显示稳定的“发言人1/发言人2”。
4. 功能参考讯飞听见办公页面，补齐看板、日程、写作、AI工具、知识库、预约会议、预定会议室等可操作入口。
5. 后端继续接 DashScope/Qwen ASR、本地模型服务、智能体工作流 fallback，前端所有按钮尽量都有真实后端契约支撑。

## 2. 当前启动方式和端口

从项目根目录启动全部服务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
```

当前约定端口：

- 前端：`http://127.0.0.1:5173`
- 后端：`http://127.0.0.1:8001`
- 本地模型服务：`http://127.0.0.1:8100`

常用健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/health
Invoke-RestMethod http://127.0.0.1:8001/api/workflows/status
Invoke-RestMethod http://127.0.0.1:8100/v1/health
```

注意：不要再按旧文档里的 `8011` 找后端，本轮验证使用的是 `8001`。

## 3. 已经完成了什么

### 3.1 导入转写

已修改 `backend/app/asr_gateway.py`：

- 本地音频文件不再直接整文件丢给同步 ASR。
- 新增 ffprobe 探测音频时长。
- 新增长音频切片逻辑：用 ffmpeg 切成 16k 单声道 WAV 小段，再逐段调用 `qwen3-asr-flash` 同步识别。
- 每段结果会带时间轴，前端能看到多段真实转写结果。
- 如果是远程 URL，仍可走 filetrans 链路。

已修改前端导入流程：

- 导入转写完成后仍停留在导入页面。
- 页面能显示导入任务状态和转写结果摘要/片段，不再强制跳到实时会议。
- 已避免“处理完成却看不到结果”的旧问题。

### 3.2 实时会议

已修改 `frontend/app.js` 和 `backend/app/main.py`：

- 实时采集从短 3 秒碎片调整为约 8 秒 flush，降低断句过碎问题。
- WebSocket config 支持 `chunkDurationMs`，后端按实际 chunk 时长生成时间轴。
- 后端实时 ASR 会尝试结合 diarization/声纹映射，再返回 speaker 信息。

### 3.3 声纹和多人发言人区分

已修改 `backend/app/main.py`：

- 不再用“整段音频 top1 声纹结果”覆盖所有多说话人片段。
- 优先使用 diarization speaker key，把同一个 diarization speaker 稳定映射到同一个人。
- 若某个 diarization speaker 命中声纹，显示声纹姓名，并补充 `speakerTitle`，例如部门/职位。
- 若未命中声纹，显示稳定编号：`发言人1`、`发言人2`。
- 声纹匹配会从对应说话人窗口截取代表音频，避免把整场会议误判成一个人。

前端 `frontend/app.js` 已支持显示：

- `姓名（部门/职位）`
- 或 fallback 的 `发言人1/发言人2`

### 3.4 讯飞听见风格功能补齐

参考了讯飞听见办公页面，已补齐一批前后端可点可用的模块。

新增前端页面/导航，主要在 `frontend/index.html`、`frontend/app.js`、`frontend/styles.css`：

- 看板：`board`
- 日程：`schedule`
- 写作：`writing`
- AI工具：`aitools`
- 知识库：`knowledge`

新增后端接口，主要在 `backend/app/main.py` 和 `backend/app/store.py`：

- `GET /api/board`
- `GET /api/schedules`
- `POST /api/schedules`
- `GET /api/meeting-rooms`
- `POST /api/meeting-rooms/{room_id}/reserve`
- `GET /api/knowledge/items`
- `POST /api/knowledge/items`
- `GET /api/ai-tools`
- `POST /api/writing/generate`

这些接口目前是轻量可用契约，适合先支撑前端验收；后续可继续扩展完整 CRUD、附件上传、权限、模板管理等。

### 3.5 测试覆盖

新增/更新测试：

- `backend/tests/test_core_services.py`
  - 覆盖长音频本地同步 ASR 切片后的时间戳结果。
- `backend/tests/test_api_contract.py`
  - 覆盖无声纹命中时 diarization speaker 编号。
- `backend/tests/test_iflytek_style_contract.py`
  - 覆盖看板、日程、会议室、知识库、AI工具、写作等后端契约。
- `frontend/prototype_spec_test.mjs`
  - 覆盖新增页面 DOM、API path 和 CSS marker。

## 4. 最后一次已验证结果

下面这些是本轮完成前跑过的验证，不是猜测：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1
```

结果：后端 `45 tests OK`，并且脚本内 smoke 通过。

```powershell
node frontend\prototype_spec_test.mjs
```

结果：`prototype spec ok`。

```powershell
backend\.venv\Scripts\python.exe scripts\smoke_verify_system.py
```

结果：`SMOKE OK`。

健康检查结果：

- 后端 `/api/health`：`status ok`，`asrGatewayMode dashscope`，`modelMockMode false`。
- 模型服务 `/v1/health`：`status ok`，`mockMode false`。

浏览器级验证结果：

- 导入页面上传真实 MP3 后停留在导入页。
- 生成约 36 个 segment。
- 没有出现本地 fallback 文案。
- 有编号发言人，也有命中声纹后的姓名/职位显示。
- 实时 WebSocket 用 8 秒 chunk 验证过，返回了 `speakerName`、`speakerTitle`、`voiceprintConfidence`、`durationMs: 8000` 和非空文本。
- 新增的看板、日程、写作、AI工具、知识库页面都能打开，按钮能调用后端，没有浏览器 console/page error。

## 5. 当前卡在哪

当前没有明确代码阻塞点。用户下一步很可能会亲自打开前端做验收。

需要警惕的是：

1. 用户的真实音频、麦克风环境、浏览器权限可能和我们验证用的文件不同。
2. DashScope ASR 有网络和计费链路，不稳定时可能失败；要看后端日志，不要只看前端状态。
3. 声纹识别依赖预录入样本质量。如果样本太短、噪声大、与会议说话声差异很大，命中率会下降。
4. 新增讯飞风格模块目前是“可用契约/轻量功能”，不是完整企业级系统。

## 6. 下一步计划

建议新会话按这个顺序继续：

1. 先启动服务并确认端口：`5173 / 8001 / 8100`。
2. 打开前端，按用户真实流程做手工验收：导入转写、实时会议、声纹库、摘要、纪要、待办、翻译、语篇规整、导出。
3. 如果导入失败，第一时间看 `backend/app/asr_gateway.py` 的 DashScope 调用日志和 ffmpeg/ffprobe 是否可用。
4. 如果实时识别碎片化，检查前端 `REALTIME_FLUSH_MS` 和 WebSocket config 里的 `chunkDurationMs` 是否仍为 8000 左右。
5. 如果多人识别不准，先确认声纹库是否有对应人的样本，再看 diarization 返回的 speaker key 和 `_build_diarization_voiceprint_map` 的匹配结果。
6. 如果用户继续要求讯飞听见完整对齐，可以继续扩展：日程 CRUD、会议室编辑、知识库文件上传、写作模板、任务列表、会议模板与日程联动。

## 7. 绝对不要再踩的坑

1. 不要把长本地 MP3 整个直接走同步 `qwen3-asr-flash`。长文件必须切片，或者使用可访问 URL 走 filetrans。
2. 不要在导入转写完成后自动跳到实时会议。导入转写和实时会议是两个独立功能。
3. 不要用整段音频的 top1 声纹结果覆盖所有 diarization 片段，这会把多人会议误显示成同一个人。
4. 不要回到 3 秒实时 flush。太短会导致句子不完整，用户已经明确指出过这个问题。
5. 不要把后端端口写回旧的 `8011`。当前验证和启动脚本使用 `8001`。
6. 不要只跑静态测试就说好了。这个项目的问题经常出现在真实浏览器上传、WebSocket、DashScope、模型服务联动里。
7. 不要在文档或源码里打印/保存用户的讯飞账号密码、百炼 API Key、智能体 token 等敏感信息。`.env` 可本地使用，但不要扩散到文档。
8. 不要使用递归或批量删除命令。当前 AGENTS 明确禁止 `del /s`、`rd /s`、`Remove-Item -Recurse`；只允许单个完整路径文件删除。
9. 不要看到页面能打开就认为前后端联通。必须确认 health、API、WebSocket、浏览器 console。
10. 不要把测试数据长期留在数据库。做浏览器契约测试后，如新增了 Codex 测试日程/知识/写作记录，要定点清理对应 SQLite 行。

## 8. 重要文件索引

后端核心：

- `backend/app/asr_gateway.py`：DashScope ASR、本地长音频切片、实时 chunk 识别。
- `backend/app/main.py`：API 路由、WebSocket、声纹匹配、diarization 映射、新增讯飞风格接口。
- `backend/app/store.py`：JSON/SQLite 风格存储封装，新增 schedules、meeting_rooms、knowledge_items、writing_documents。
- `backend/app/workflow_service.py`：摘要、纪要、待办、翻译、语篇规整等工作流/fallback。

前端核心：

- `frontend/index.html`：所有页面 DOM 和导航。
- `frontend/app.js`：页面状态、API 调用、导入/实时/声纹/新增模块交互。
- `frontend/styles.css`：整体样式和新增模块布局。
- `frontend/prototype_spec_test.mjs`：前端结构规格测试。

测试：

- `backend/tests/test_core_services.py`
- `backend/tests/test_api_contract.py`
- `backend/tests/test_iflytek_style_contract.py`
- `scripts/smoke_verify_system.py`
- `scripts/run_tests.ps1`

## 9. 修改代码时的项目要求

用户追加的 AGENTS 要求必须遵守：

1. 写代码和改代码时要加详细中文注释，尤其是复杂流程、接口兜底、声纹/ASR/工作流编排部分。
2. 禁止批量、递归删除文件或文件夹。
3. 全程禁止：
   - CMD：`del /s`、`rd /s`
   - PowerShell：`Remove-Item -Recurse`
4. 仅允许单次删除单个独立文件，并且必须使用完整文件路径。
5. 如果需求涉及批量删除文件或删除文件夹，立即停止并提示用户手动完成。

## 10. 给新会话的建议

先不要大改架构。这个项目现在已经能跑通主链路，最容易出问题的是“真实浏览器 + 真实音频 + DashScope + 本地模型服务”的联动细节。

继续处理用户反馈时，优先复现用户截图里的具体动作，然后看后端日志和浏览器 console，再动代码。每次修完至少跑：后端测试、前端规格测试、smoke、以及一遍浏览器上传或实时 WebSocket 验证。
