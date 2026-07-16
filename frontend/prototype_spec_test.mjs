import { readFileSync } from "node:fs";
import { strict as assert } from "node:assert";

const html = readFileSync(new URL("./index.html", import.meta.url), "utf8");
const js = readFileSync(new URL("./app.js", import.meta.url), "utf8");
const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
let realtimeWorklet = "";
try {
  realtimeWorklet = readFileSync(new URL("./realtime-audio-worklet.js", import.meta.url), "utf8");
} catch {
  realtimeWorklet = "";
}

// 这些结构标记用于防止页面退回上一版“顶部导航 + 简单卡片”的展示原型。
// 新版必须是讯飞风格的产品工作台：左侧功能栏、大白色工作区、详情页右侧工具栏。
const requiredHtmlMarkers = [
  "app-shell",
  "side-nav",
  "workspace-card",
  "top-actions",
  "right-tool-dock",
  "page-records",
  "page-import",
  "page-voiceprints",
  "page-hotwords",
  "page-sensitive",
  "page-templates",
  "page-integration",
  "speakerPanel",
  "transcriptEditor",
  "bottomAudioPlayer",
  "meetingListView",
  "quickMeetingBtn",
  "importLedgerView",
  "importDetailView",
  "backToImportLedgerBtn",
  "endMeetingBtn",
  "realtimePlayBtn",
  "voiceprintGroupList",
  "voiceprintTableBody",
  "batchDeleteVoiceprintBtn",
  "batchDownloadVoiceprintBtn",
  "template-tabs",
  "data-template-source=\"my\"",
  "data-template-source=\"system\"",
  "importTemplateCard",
  "templateImportDialog",
  "templateFileInput",
  "templateTagEditor",
  "templatePreviewDialog",
  "templatePreviewModalBody",
  "optimization-tabs",
  "manualKeywordsPanel",
  "documentKeywordsPanel",
  "smartKeywordsPanel",
  "replacementRulesPanel",
  "sensitiveDisplayModes",
  "displayModeHide",
  "displayModeSpace",
  "displayModeStars",
];

// 这些 JS 标记约束前端必须继续走 API，不得回退到本地假数据。
// 同时要求新增的批量声纹、模板复制、识别优化、会议详情工具栏动作都有显式处理函数。
const requiredJsMarkers = [
  "apiRequest",
  "loadInitialData",
  "voiceprintGroups",
  "manualKeywords",
  "replacementRules",
  "templateSource",
  "optimizationTab",
  "renderVoiceprintManager",
  "renderOptimizationCenter",
  "renderTemplateCenter",
  "renderMeetingDetailWorkspace",
  "isImportedMeeting",
  "meetingRecords",
  "importRecords",
  "openMeetingDetail",
  "openTemplateImportDialog",
  "parseTemplateFile",
  "saveImportedTemplate",
  "setImportProcessingState",
  "importResults",
  "renderDetailToolResult",
  "/api/imports/transcribe",
  "/api/meeting-rooms",
  "asrFallback",
  "startRealtimeTranscription",
  "startMicrophoneCapture",
  "encodeWavFromSamples",
  "flushRealtimeAudioChunk",
  "stopRealtimeTranscription",
  "realtimeMediaStream",
  "REALTIME_FLUSH_MS",
  "chunkDurationMs",
  "handleRealtimeMessage",
  "openTemplatePreview",
  "copyTemplate",
  "importTemplate",
  "batchDeleteVoiceprints",
  "batchDownloadVoiceprints",
  "saveManualKeywords",
  "uploadOptimizationDocument",
  "generateSmartKeywords",
  "saveReplacementRule",
  "saveForbiddenWords",
  "patchMeetingSegment",
  "runDetailTool",
  "fetch(",
];

// CSS 侧重点是布局骨架和主要交互控件。只测类名，不测像素，避免把视觉迭代锁死。
const requiredCssMarkers = [
  ".app-shell",
  ".side-nav",
  ".workspace-card",
  ".top-actions",
  ".right-tool-dock",
  ".detail-workbench",
  ".speaker-panel",
  ".transcript-editor",
  ".bottom-audio-player",
  ".file-list-row",
  ".template-tabs",
  ".template-card",
  ".template-import-dialog",
  ".template-tag-palette",
  ".template-preview-layout",
  ".voiceprint-manager",
  ".voiceprint-table",
  ".optimization-tabs",
  ".optimization-panel",
  ".forbidden-layout",
  ".display-mode-row",
  ".iflytek-import-table",
  ".detail-ai-workbench",
  ".tool-tab-bar",
];

// 本轮 ElevenLabs 风格重构需要额外保护这些设计钩子。
// 它们不验证具体像素，只防止后续改动把“编辑感工作台”的 HTML/CSS 骨架删掉。
const requiredElevenLabsHtmlMarkers = [
  "records-toolbar",
  "surface-panel",
  "editor-surface",
];

// 这些 CSS token 和类名是本次视觉系统的入口。
// 测试它们能确保页面继续使用米白画布、近黑主色和柔和氛围光，而不是退回蓝色后台皮肤。
const requiredElevenLabsCssMarkers = [
  "--canvas:",
  "--ink:",
  ".records-toolbar",
  ".surface-panel",
  ".editor-surface",
];

// Image review feedback guards:
// 1. The left rail must no longer render the brand block or "政企系统" subtitle.
// 2. The reviewed pages need dedicated cleanup hooks for hero spacing, tabs, mode controls, and footer actions.
const forbiddenImageFeedbackHtmlMarkers = [
  "政企系统",
  "optimization-side",
];

const requiredImageFeedbackCssMarkers = [
  ".side-brand-wordmark",
  ".side-nav-clean",
  ".review-clean-tabs",
  ".sensitive-mode-panel",
  ".footer-action-pills",
  ".optimization-main-full",
];

for (const marker of requiredHtmlMarkers) {
  assert.ok(html.includes(marker), `index.html 缺少讯飞风工作台入口：${marker}`);
}

for (const marker of requiredElevenLabsHtmlMarkers) {
  assert.ok(html.includes(marker), `index.html 缺少 ElevenLabs 工作台设计钩子：${marker}`);
}

for (const marker of requiredJsMarkers) {
  assert.ok(js.includes(marker), `app.js 缺少新版业务逻辑：${marker}`);
}

for (const marker of requiredElevenLabsCssMarkers) {
  assert.ok(css.includes(marker), `styles.css 缺少 ElevenLabs 工作台视觉系统：${marker}`);
}

for (const marker of forbiddenImageFeedbackHtmlMarkers) {
  assert.ok(!html.includes(marker), `index.html 仍包含图片要求删除的标记：${marker}`);
}

for (const marker of requiredImageFeedbackCssMarkers) {
  assert.ok(css.includes(marker), `styles.css 缺少图片反馈排版修正钩子：${marker}`);
}

assert.ok(html.includes("智能会议"), "左侧顶部需要恢复“智能会议”四个字");

assert.ok(
  !js.includes('import: ["导入转写", "导入台账、转写编辑、声纹注册、规整、AI 摘要、纪要、待办与标记。"]'),
  "导入转写副标题不应再保留包含“智能会议”品牌感的长说明文案"
);

for (const marker of requiredCssMarkers) {
  assert.ok(css.includes(marker), `styles.css 缺少新版布局样式：${marker}`);
}

assert.ok(!js.includes("prompt("), "前端不能使用浏览器原生 prompt，必须使用系统内弹窗表单");
assert.ok(!js.includes("alert("), "前端不能使用浏览器原生 alert，必须使用 toast 或状态提示");
assert.ok(!js.includes("localStorage.setItem("), "前端业务数据不能写入 localStorage，必须写后端持久化接口");
assert.ok(!html.includes("科大讯飞"), "页面可以参考功能和信息架构，但不能复制竞品品牌文案");
assert.ok(!js.includes("会议已创建，开始上传文件"), "导入转写不能把内部记录创建暴露成“创建会议”提示");
assert.ok(!js.includes("文件处理完成，已进入转写详情"), "导入转写完成后必须留在导入页展示结果，不能自动跳到实时会议详情");
assert.ok(!html.includes("importResultPanel"), "导入转写台账页不应再直接渲染底部转写结果区域");
assert.ok(!js.includes("renderImportResults"), "导入转写台账页不应再渲染转写片段卡片，详情统一从“查看”进入");
assert.ok(!js.includes("文件处理完成，结果已在下方展示"), "导入完成提示不应再暗示台账下方会展示转写正文");
assert.ok(js.includes("文件处理完成，已加入导入台账"), "导入完成后应提示记录已进入台账，并引导用户查看详情");
assert.ok(js.includes("会议已创建，但麦克风不可用，实时转写未启动"), "快速会议创建成功后，麦克风失败只能提示实时转写未启动，不能吞掉会议创建结果");
assert.ok(js.includes("声纹资料已保存，但样本上传失败"), "声纹资料保存和样本上传必须分段处理，避免样本失败导致资料丢失感");
assert.ok(!js.includes("已生成演示转写片段"), "实时会议不能再生成演示转写片段，必须采集真实麦克风音频或提示不可用");
assert.ok(!js.includes("JSON.stringify(result, null, 2)"), "详情页右侧工具不能直接把后端 JSON 原样展示给用户");

assert.ok(html.includes("会议列表"), "我的记录模块必须改名为会议列表");
assert.ok(!html.includes("我的记录"), "页面文案不应继续暴露我的记录");
assert.ok(html.includes("quickMeetingBtn"), "会议列表必须提供快速会议入口");
assert.ok(html.includes("endMeetingBtn"), "实时会议详情必须提供结束会议按钮");
assert.ok(!html.includes("startRealtimeBtn"), "实时转写入口应收敛到会议列表的快速会议流程");
assert.ok(js.includes("meetingRecords()"), "会议列表必须通过 meetingRecords 过滤掉导入转写记录");
assert.ok(js.includes("importRecords()"), "导入转写列表必须通过 importRecords 只展示上传导入记录");
assert.ok(js.includes("!isImportedMeeting(meeting)") && js.includes("isImportedMeeting(meeting)"), "前端必须显式区分会议记录和导入记录");
assert.ok(js.includes("startRealtimeTranscription"), "快速会议入口必须继续调用真实实时转写流程");
assert.ok(js.includes("bottomAudioPlayer") && js.includes("hidden = imported"), "导入转写详情必须隐藏实时转写播放器，避免和快速会议在线转写混在一起");
assert.ok(js.includes("正在执行") && js.includes("请选择一条会议记录后再使用右侧 AI 工具"), "右侧 AI 工具必须有执行反馈和空会议保护，避免用户误以为按钮失效");

assert.ok(js.includes("analyzeRealtimeAudioQuality"), "实时转写必须先分析音频质量，不能把静音或底噪分片直接送进 ASR");
assert.ok(js.includes("REALTIME_MIN_ACTIVE_RATIO"), "实时转写必须检查有效语音占比，降低静音底噪导致的乱识别");
assert.ok(js.includes("当前音频分片音量过低，已跳过实时转写"), "实时转写跳过低质量分片时必须给出明确状态提示");
assert.ok(js.includes("setRealtimeStatus") && js.includes("realtimeStatus"), "实时转写必须有独立状态机，不能只靠 realtimeRunning 判断识别中、低音量、暂停和错误");
assert.ok(js.includes("renderRealtimeEmptyState") && js.includes("正在采集麦克风，等待可识别语音"), "实时识别中但还没有文本时，编辑区必须显示采集中的真实空态");
assert.ok(js.includes("当前麦克风音量偏低，请靠近麦克风或检查输入设备"), "低音量时必须在页面内联提示用户检查麦克风，而不是只刷 toast");
assert.ok(js.includes("realtimeStopIntent") && js.includes('state.realtimeStopIntent === "pause"'), "WebSocket 关闭时必须区分用户主动暂停和异常/低音量状态，避免误报“已暂停”");
assert.ok(js.includes('event.code === "low_volume"') && js.includes('event.code === "asr_empty"'), "前端必须处理后端结构化实时 status code，低音量和 ASR 空结果不能混成暂停");
assert.ok(js.indexOf("analyzeRealtimeAudioQuality") < js.indexOf("socket.send(encodeWavFromSamples"), "实时音频分片必须先完成质量门控，再发送给后端 ASR");
assert.ok(js.includes("const REALTIME_FLUSH_MS = 15000"), "实时转写分片窗口不能过短；15 秒能给 ASR 更完整的上下文，减少每段只识别几个字");

const speakerIndex = html.indexOf('id="speakerPanel"');
const editorIndex = html.indexOf('id="transcriptEditor"');
const toolDockIndex = html.indexOf("right-tool-dock");
assert.ok(speakerIndex > -1 && editorIndex > -1 && toolDockIndex > -1, "详情页必须包含说话人、编辑器和右侧工具栏");
assert.ok(speakerIndex < editorIndex && editorIndex < toolDockIndex, "说话人列表必须移动到转写编辑区左侧");

// 用户截图要求在线会议和导入转写详情共用的三栏工作台继续微调：
// 左侧说话人栏要更窄、右侧 AI 工具栏要更宽，并且左右两栏都能在编辑时收起。
// 这里不测具体像素值，但用稳定类名和状态函数保护结构，避免后续只改其中一个入口。
assert.ok(html.includes('data-detail-collapse="speaker"'), "详情页左侧说话人栏必须提供收起/展开按钮");
assert.ok(html.includes('data-detail-collapse="tools"'), "详情页右侧 AI 工具栏必须提供收起/展开按钮");
assert.ok(js.includes("detailPanelCollapsed"), "左右栏收起状态必须保存在共享详情工作台状态中");
assert.ok(js.includes("syncDetailWorkbenchState"), "详情工作台渲染后必须同步收起类名和按钮可访问状态");
assert.ok(js.includes("toggleDetailPanel"), "左右栏收起/展开必须由统一函数处理，确保在线会议和导入详情一致");
assert.ok(css.includes(".detail-workbench.speaker-collapsed"), "详情工作台必须支持左侧说话人栏收起布局");
assert.ok(css.includes(".detail-workbench.tools-collapsed"), "详情工作台必须支持右侧 AI 工具栏收起布局");
assert.ok(css.includes("minmax(196px, 220px)") && css.includes("minmax(500px, 600px)"), "发言人栏必须保持紧凑，右侧 AI 工具栏必须获得足够的编辑和结果展示空间");
assert.ok(css.includes(".panel-edge-toggle"), "左右栏收起按钮必须做成边缘浮动控件，避免藏在面板内容里看不见");
assert.ok(!css.includes("right: -17px") && !css.includes("left: -17px"), "左右栏收起按钮不能用负偏移导致按钮被相邻面板或 overflow 裁切");
assert.ok(css.includes(".speaker-panel.is-collapsed .panel-mode-tabs") && css.includes(".speaker-panel.is-collapsed .navigation-mode-hint"), "左栏收起时必须隐藏发言人/章节切换和说明，不能在窄轨道里残留截断文字");
assert.match(html, /<aside class="right-tool-dock">\s*<!-- 收起按钮必须是面板直属元素/, "右栏展开按钮必须位于工具标签栏之外，收起后仍可操作");
assert.ok(css.includes(".right-tool-dock.is-collapsed > .tool-edge-toggle"), "右栏收起后必须显式保留直属展开按钮");
assert.ok(css.includes("flex-wrap: nowrap") && css.includes(".rich-toolbar"), "转写编辑工具栏必须保持单行排布，不能把菜单按钮挤到第二行");

// 媒体刚加载会在 00:00 触发浏览器事件，但不能伪装成用户正在播放第一段。
assert.ok(js.includes("playbackInteractionStarted"), "逐字稿高亮必须区分浏览器初始化与用户真实播放/跳转");
assert.ok(js.includes("state.playbackInteractionStarted = true") && js.includes("state.playbackInteractionStarted = false"), "播放交互状态必须在操作后开启、切换录音后重置");
assert.ok(css.includes(".speech-block-paragraph.is-active") && !css.includes(".speech-segment.is-active {"), "块级活动样式不能泄漏到合并正文的行内片段");

// 右侧五个 AI 工具按钮需要在请求期间出现明确运行态，避免用户误判为点击无效。
assert.ok(js.includes("runningDetailTool"), "AI 工具按钮必须记录当前运行中的工具");
assert.ok(js.includes("setDetailToolRunning"), "AI 工具按钮必须通过统一函数同步旋转动画状态");
assert.ok(css.includes(".tool-running-spinner"), "AI 工具执行中面板必须包含旋转 loading 图标样式");
assert.ok(css.includes("@keyframes detailToolSpin"), "AI 工具按钮运行态必须有旋转动画关键帧");
assert.ok(css.includes('button[data-detail-tool].is-running::before'), "五个 AI 工具按钮点击后必须能在按钮内显示旋转动画");
assert.ok(js.includes("renderDetailToolGenerating"), "右侧 AI 工具点击后必须渲染参考图式生成中面板，而不是只显示静态等待文案");
assert.ok(js.includes("streamDetailToolResult"), "AI 工具结果必须有逐字流式生成效果");
assert.ok(js.includes("detailToolProgressStages"), "AI 工具生成中必须显示分阶段进度，避免长链路接口看起来没反应");
assert.ok(js.includes("copyDetailToolResult"), "AI 工具完成后必须提供复制结果能力");
assert.ok(js.includes("applyDetailToolResultToMinutes"), "AI 工具结果必须能添加到纪要，匹配参考图操作区");
assert.ok(js.includes("saveDetailToolResult"), "AI 工具完成后必须能保存当前编辑结果，切换到其他工具再回来不应重新生成");
assert.ok(js.includes("showSavedDetailToolResult") && js.includes("openDetailTool"), "AI 工具普通点击必须优先展示已保存/已生成结果，只有重新生成按钮才重新调用后端");
assert.ok(js.includes("detailToolDrafts"), "前端必须按会议和工具缓存右侧 AI 工具结果，避免切换 Tab 后丢失");
// Minutes are durable versioned records rather than a generic AI-tool draft. Keep the selected
// version in the cache identity and make reopening fetch history before any generic-draft shortcut,
// otherwise a preserved edit can be rendered as a different version after reopening the panel.
assert.ok(js.includes("minutesVersionId"), "Minutes cache identity must include the selected minutesVersionId.");
assert.ok(js.indexOf('if (tool === "minutes")') < js.indexOf("showSavedDetailToolResult(tool)"), "Minutes reopening must prefer durable history over the generic draft cache.");
assert.ok(js.includes("loadMinutesVersions().then"), "Minutes reopening must load durable ordered version history.");
assert.ok(js.includes("detailToolRunToken"), "AI 工具生成请求必须有运行 token，避免待办/纪要等工具切换时旧请求覆盖当前面板");
assert.ok(js.includes("isCurrentDetailToolRun"), "AI 工具流式输出必须先校验当前工具和运行 token，防止生成状态串扰");
assert.ok(js.includes("detailMode") && js.includes("detailWorkspaceKey") && js.includes("resetDetailToolPanelForContext"), "实时会议和导入转写详情共用组件时必须按入口和会议隔离右侧 AI 结果");
assert.ok(js.includes("segmentFingerprint") && js.includes("detailContextKey"), "右侧 AI 结果缓存必须包含转写内容指纹，实时会议不能读取导入转写的旧草稿");
assert.ok(js.includes("detailToolDraftKey(tool, meetingId") && js.includes("contextKey"), "AI 工具草稿 key 必须显式包含 meetingId 与详情上下文，避免跨入口串数据");
assert.ok(js.includes("meetingHasTranscriptText") && js.includes("请先开始会议识别或导入音视频文件"), "无转写内容时右侧 AI 工具不能生成假摘要，必须提示先开始识别");
assert.ok(js.includes("updateDetailSelectedText") && js.includes("selectedTextForDetailMark"), "标记工具必须使用转写编辑区选中文本，而不是点击按钮后丢失选择再写入默认文案");
assert.ok(js.includes('data-detail-tool-save') && js.includes('data-detail-tool-regenerate') && js.includes('data-detail-tool-copy') && js.includes('data-detail-tool-apply-minutes'), "AI 工具完成态必须包含保存、重新生成、复制、添加至纪要四个操作");
assert.ok(css.includes(".detail-tool-editor") && css.includes(".detail-stream-cursor") && css.includes(".detail-tool-progress"), "AI 工具结果区必须支持在线编辑、流式光标和进度展示样式");
assert.ok(html.includes("speakerRenameDialog"), "发言人列表必须提供重命名弹窗，不能只能看不能改");
assert.ok(js.includes("openSpeakerRenameDialog") && js.includes("renameSpeakerAcrossSegments"), "发言人改名必须批量同步同名转写片段");
assert.ok(js.includes("speaker-correction") && js.includes('value="meeting_only"') && js.includes('value="sync_voiceprint"'), "发言人改名必须由服务端提供仅当前会议/同步声纹库两种明确范围");
assert.ok(js.includes("voiceprintRuntime") && js.includes("声纹运行时不可用") && js.includes("仍可仅修改本次会议"), "声纹运行时不可用时必须保留会议内修改并说明同步能力不可用");
assert.ok(js.includes("result.warning") && js.includes("发言人已修改"), "声纹库同步失败只能警告，不能回滚已经保存的会议发言人修改");
assert.ok(html.includes(">保存修改</button>"), "修改发言人弹窗主按钮应使用中性文案，由保存范围决定是否同步声纹库");
assert.ok(html.includes("<th>姓名</th><th>部门</th>"), "声纹库表头第一列业务字段必须是姓名、第二列必须是部门");
assert.ok(!html.includes("<th>模型名称</th><th>注册人</th>"), "声纹库不能继续把发言人资料显示成模型名称/注册人");
assert.ok(js.includes("entity-dialog-grid") && js.includes("声纹样本音频"), "导入声纹弹窗需要使用重构后的表单栅格和清晰样本文案");
assert.ok(html.includes("createOptimizationMode") && html.includes("识别优化模式"), "快速会议弹窗必须把领域改成识别优化模式");
assert.ok(html.includes("createOptimizationPicker"), "快速会议弹窗必须提供识别优化项选择区，而不是复用热词库视觉");
assert.ok(!html.includes("id=\"createDomain\""), "快速会议弹窗不应继续出现领域下拉框");
assert.ok(!html.includes("id=\"createHotwordPicker\""), "快速会议弹窗不应继续出现旧热词库选择区");
assert.ok(js.includes("function renderHotwordPicker") && js.includes('renderHotwordPicker("importHotwordPicker")'), "导入转写页仍需要自己的热词库渲染函数，快速会议改造不能删掉它");
assert.ok(html.includes("startRealtimeMeetingBtn") && html.includes("开始会议"), "实时会议详情顶部必须提供开始会议按钮，点击后开始实时转写");
assert.ok(
  html.includes('<option value="zh">中文普通话</option>')
    && html.includes('<option value="en">英文</option>')
    && html.includes('<option value="auto">中英混合</option>'),
  "实时会议和导入转写的语言下拉框必须提交 ASR 标准代码，不能把中文展示文案传给 DashScope",
);
assert.match(html, /\.\/app\.js\?v=[^"']+/, "index.html must cache-bust app.js so browsers load the current product logic.");
assert.match(html, /\.\/styles\.css\?v=[^"']+/, "index.html must cache-bust styles.css so layout fixes are visible without a stale browser cache.");
assert.ok(js.includes("syncRealtimeControls") && js.includes("compactRealtimeIcon"), "实时会议的顶部按钮和底部播放器按钮必须分开同步，底部保持小图标按钮");
assert.ok(js.includes("shouldFlushRealtimeSegment") && js.includes("silenceEndMs") && js.includes("maxSegmentMs"), "实时转写必须用端点检测和最大时长兜底，不能只靠固定间隔切分");
assert.ok(js.includes("const REALTIME_MIN_CHUNK_MS = 800") && js.includes("const REALTIME_SILENCE_END_MS = 1200") && js.includes("const REALTIME_SENTENCE_END_SILENCE_MS = 700") && js.includes("const REALTIME_MAX_SEGMENT_MS = 15000"), "实时会议必须保留足够的自然停顿和句子上下文，避免 1～2 秒碎片同时降低 ASR 与声纹稳定性。");
assert.ok(js.includes("REALTIME_MIN_FINAL_SPEECH_MS") && js.includes("REALTIME_MIN_FINAL_SEGMENT_MS"), "实时转写不能把 1 秒左右的短碎片直接写入正文，必须有稳定语音和上下文窗口门槛");
assert.ok(js.includes("realtimeSessionToken") && js.includes("newRealtimeSessionToken"), "实时转写必须为每次开始会议生成独立会话 token，避免旧 WebSocket/轮询结果写回当前详情");
assert.ok(js.includes("isCurrentRealtimeEvent") && js.includes("event.sessionToken"), "前端处理实时 WebSocket 消息前必须校验 sessionToken，过期事件不能渲染或落入当前会议");
assert.ok(js.includes("sessionToken: state.realtimeSessionToken"), "前端发送 realtime_config/realtime_chunk 元数据时必须携带当前会话 token，后端才能回传并完成隔离");
assert.ok(js.includes("overlapMs") && js.includes("realtimePendingChunkMeta"), "实时转写 flush 时必须携带真实时间戳和 overlap 元数据，减少边界吞字");
assert.ok(js.includes("currentRealtimeTimelineBaseMs") && js.includes("state.realtimeCapturedMs = timelineBaseMs"), "暂停后重新开始实时识别必须从已有片段最大时间继续，不能把新片段时间轴重置到 00:00。");
assert.ok(js.includes("trimRealtimeSilenceBuffer") && js.includes("REALTIME_IDLE_BUFFER_MS"), "实时转写静音期间必须裁掉旧缓冲，避免下一段语音混入上一段尾音或长静音");
assert.ok(js.includes("audioContext.resume()"), "实时转写创建 AudioContext 后必须主动 resume，避免浏览器把采集上下文挂起导致没有音频帧");
assert.ok(js.includes("realtimeInFlightChunks") && js.includes("syncRealtimeMeetingFromServer"), "Realtime transcription must track in-flight ASR chunks and poll server records so saved backend segments cannot be hidden by a later low-volume status.");
assert.ok(js.includes("REALTIME_CONTEXT_TAIL_CHARS") && js.includes("realtimeTranscriptContextText"), "实时转写必须把上一段正文作为上下文带给后端 ASR，不能让每个音频块孤立识别。");
assert.ok(js.includes("contextText: realtimeTranscriptContextText()"), "flush 实时音频块时必须把上下文文本写入 realtime_chunk 元数据。");
assert.ok(js.includes("REALTIME_STREAMING_MODE") && js.includes("encodePcm16FromSamples") && js.includes("resampleRealtimeFrame"), "实时会议主路径必须持续发送 16k PCM 流式帧，不能只攒 WAV 分片后同步识别。");
assert.ok(js.includes('new AudioContextClass({ sampleRate: REALTIME_STREAM_SAMPLE_RATE, latencyHint: "interactive" })'), "实时采集应优先让浏览器音频引擎原生输出 16kHz，避免粗糙线性降采样损伤辅音和专有名词识别。");
assert.ok(js.includes("createScriptProcessor(1024, 1, 1)"), "实时 PCM 帧应控制在约 20-70ms，不能用过大的处理缓冲增加首字延迟。");
assert.ok(js.includes("audioWorklet.addModule") && js.includes("new AudioWorkletNode"), "支持 AudioWorklet 的浏览器必须在音频线程采集 PCM，避免主线程渲染导致麦克风丢帧。");
assert.ok(js.includes('state.realtimeCaptureMode = "audio_worklet"') && js.includes("dataset.captureMode"), "实时采集必须暴露实际使用的音频线程模式，便于浏览器验收确认没有静默回退。");
assert.ok(realtimeWorklet.includes("registerProcessor") && realtimeWorklet.includes("FRAME_SAMPLES = 1024"), "实时音频 worklet 必须按稳定 1024 样本帧发送，兼顾延迟和 WebSocket 开销。");
assert.ok(js.includes('event.type === "partial_transcript"') && js.includes("realtimeDraftText"), "前端必须渲染流式 partial 文本，达到边说边出的实时体验。");
assert.ok(js.includes("updateRealtimeDraftInline") && js.includes("realtimeDraftTextArea"), "partial 文本必须只更新草稿节点，不能高频重绘整个详情工作区造成出字卡顿和编辑内容闪动。");
assert.ok(js.includes("waitForRealtimeServerClose") && js.includes('event.type === "closed"'), "暂停或结束实时会议时必须等待后端完成最后一句转写，不能发送 stop 后立刻关闭浏览器 WebSocket。");
assert.ok(js.includes("shouldShowRealtimeLowVolumeEmpty") && js.includes("state.realtimeInFlightChunks <= 0"), "Realtime low-volume copy must not replace the editor while ASR chunks are still in flight.");
assert.ok(!js.includes('return state.realtimeRunning ? "暂停识别" : "开始识别"'), "底部播放器按钮不能再用中文开始/暂停文案");
assert.ok(js.includes('return state.realtimeRunning ? "⏸" : "▶"'), "底部播放器按钮必须使用传统播放/暂停符号");
assert.ok(css.includes(".detail-realtime-button") && css.includes(".bottom-audio-player .play-button"), "实时开始按钮和底部小播放按钮都必须有稳定样式");

// 导入转写和转写详情必须合并为一个模块：左侧导航只有“导入转写”，台账内点击查看进入同模块详情。
assert.ok(!html.includes('data-route="detail"'), "转写详情不应再作为独立左侧导航模块");
assert.ok(!html.includes('id="page-detail"'), "转写详情不应再作为独立页面，必须合并进导入转写模块");
assert.ok(html.includes('id="importLedgerView"'), "导入转写模块必须包含台账视图");
assert.ok(html.includes('id="importDetailView"'), "导入转写模块必须包含详情视图");
assert.ok(js.includes("openImportDetail"), "点击台账查看必须通过 openImportDetail 在导入模块内打开详情");
assert.ok(js.includes("backToImportLedger"), "导入详情必须能返回台账");
assert.ok(!js.includes('routeTo("detail")'), "查看转写详情不能再跳到独立 detail 路由");

// 用户指出截图里的暂停、结束、分享、继续转写按钮不要；实时转写需要恢复成明确入口。
for (const removedDetailButton of ["pauseBtn", "finishBtn", "shareDetailBtn", "continueTranscribeBtn"]) {
  assert.ok(!html.includes(removedDetailButton), `转写详情不应再保留问题按钮：${removedDetailButton}`);
}

// 本轮收敛产品范围：看板、日程、写作、AI 工具、知识库都要从前端入口和页面结构中删除。
// 这些断言保护用户要求的“前后端都删除”，避免只隐藏导航但残留页面和初始化调用。
for (const removedRoute of ["board", "schedule", "writing", "aitools", "knowledge"]) {
  assert.ok(!html.includes(`data-route="${removedRoute}"`), `左侧导航不应再暴露 ${removedRoute} 模块`);
  assert.ok(!html.includes(`page-${removedRoute}`), `index.html 不应再保留 page-${removedRoute} 页面`);
}
for (const removedApi of ["/api/board", "/api/schedules", "/api/knowledge/items", "/api/ai-tools", "/api/writing/generate"]) {
  assert.ok(!js.includes(removedApi), `app.js 不应再调用已删除后端接口：${removedApi}`);
}
for (const removedRenderer of ["renderBoardPage", "renderSchedulePage", "renderWritingPage", "renderAiToolsPage", "renderKnowledgePage"]) {
  assert.ok(!js.includes(removedRenderer), `app.js 不应再保留已删除模块渲染函数：${removedRenderer}`);
}

assert.ok(!html.includes('data-route="translate"'), "翻译优化模块本轮应先从左侧导航和优化中心移除");
assert.ok(!html.includes("page-translate"), "翻译优化独立页面本轮应先移除，会议详情里的翻译工具保留");
assert.ok(!html.includes('data-detail-tool="translate"'), "导入转写详情页不再提供翻译工具");
assert.ok(!html.includes('data-detail-tool="mindmap"'), "导入转写详情页不再提供导图工具");
assert.ok(!js.includes("/translate"), "app.js 不应再调用会议翻译接口");
assert.ok(!js.includes("generateMindmap"), "app.js 不应再保留导图生成入口");

// Task 7 protects the workbench contract at the static boundary as well as in the browser audit.
// These assertions intentionally name durable IDs and functions rather than visual measurements so
// later stylesheet work cannot silently remove the product behavior that operators depend on.
assert.ok(!html.includes('class="records-hero"'), "会议列表不能保留重复的装饰性 records hero");
// 用户已明确指出详情标题下方的冻结配置横条占据主工作区且信息冗余，因此静态契约反向保护：
// 数据仍由会议快照保存，但不再在每次查看逐字稿时强制占用一整行。
assert.ok(!html.includes('id="meetingConfigSummary"') && !js.includes("renderMeetingConfigSummary"), "详情工作台必须移除冗余的本次实际配置横条");
assert.ok(html.includes('id="transcriptWorkbenchBar"') && html.includes('id="transcriptSearch"') && html.includes('id="transcriptSpeakerFilter"'), "逐字稿必须保留可按内容条件显示的搜索和发言人筛选控件");
assert.ok(!html.includes('id="transcriptAutosaveState"'), "只读逐字稿不能展示没有真实保存状态流支撑的“已保存”提示");
assert.ok(html.includes('placeholder="搜索转写内容"') && html.includes('aria-label="搜索转写内容"'), "逐字稿搜索控件必须使用中文业务文案");
assert.ok(html.includes('<option value="">全部发言人</option>'), "发言人筛选必须使用中文默认选项");
assert.ok(!html.includes('role="status">已保存</span>'), "详情页必须移除容易误导用户的静态“已保存”文案");
assert.ok(!html.includes('data-detail-tool="summary">AI摘要</button>'), "AI 工具栏不应保留冗余的 AI 摘要长标签");
assert.ok(html.includes('data-detail-tool="summary">摘要</button>'), "AI 工具栏摘要按钮必须使用紧凑标签");
assert.ok(html.includes("management-layout"), "管理页必须共享紧凑的双列管理布局");
assert.ok(js.includes("function renderArtifactStaleBanner") && js.includes("会议转写内容已更新"), "衍生结果过期时只应展示人类可读的中文提示");
assert.ok(!css.includes("meeting-config-summary"), "已移除的配置摘要不能残留空白样式或占位高度");
assert.ok(!js.includes('transcriptAutosaveState: "已保存"'), "前端状态不能继续维护没有真实保存动作的静态保存文案");
assert.ok(js.includes("function isCurrentSpeakerUpdate") && js.includes("isCurrentSpeakerUpdate(event)"), "异步声纹更新必须单独严格校验会议和会话 token");
assert.ok(js.includes('event.type === "speaker_update"') && js.includes("event.segmentId") && js.includes("affectedSegmentIds"), "实时声纹结果必须按稳定片段 ID 增量更新");
const speakerFieldList = js.slice(js.indexOf("const REALTIME_SPEAKER_UPDATE_FIELDS"), js.indexOf("function isCurrentSpeakerUpdate"));
assert.ok(speakerFieldList.includes("REALTIME_SPEAKER_UPDATE_FIELDS") && !/["']text["']/.test(speakerFieldList), "声纹更新身份字段白名单的任意位置都不能包含 text");
const speakerGuardBody = js.slice(js.indexOf("function isCurrentSpeakerUpdate"), js.indexOf("async function ensureRealtimeMeeting"));
for (const requiredGuard of ["event.meetingId", "event.sessionToken", "state.currentMeetingId", "state.realtimeSessionToken", "String(event.meetingId) === String(state.currentMeetingId)", "event.sessionToken === state.realtimeSessionToken"]) {
  assert.ok(speakerGuardBody.includes(requiredGuard), `speaker_update 严格会话守卫缺少：${requiredGuard}`);
}
const speakerUpdateBody = js.slice(js.indexOf('if (event.type === "speaker_update")'), js.indexOf('if (event.type === "error")'));
assert.ok(speakerUpdateBody.includes("isCurrentSpeakerUpdate(event)") && speakerUpdateBody.includes("meeting.segments = (meeting.segments || []).map"), "speaker_update 必须先校验会话，再通过 map 更新既有片段");
assert.ok(!speakerUpdateBody.includes("event.text") && !speakerUpdateBody.includes("segments.push") && !/event\.segment(?!Id)/.test(speakerUpdateBody), "speaker_update 不能覆盖正文或追加新片段");
const stableToolTabRule = css.indexOf("grid-template-columns: repeat(5, minmax(64px, 1fr))");
assert.ok(stableToolTabRule >= 0, "右侧五个 AI 工具必须使用稳定的单行五列布局");
const stableToolTabBlock = css.slice(css.lastIndexOf(".right-tool-dock .tool-tab-bar {", stableToolTabRule), css.indexOf("}", stableToolTabRule));
assert.ok(stableToolTabBlock.includes("grid-template-columns: repeat(5, minmax(64px, 1fr))"), "五列声明必须属于右侧 AI 工具栏选择器");
assert.ok(stableToolTabBlock.includes("overflow-x: auto"), "窄屏下五列 AI 工具栏必须允许容器内横向滚动");
const detailToolButtons = [...html.matchAll(/<button data-detail-tool="([^"]+)">([^<]+)<\/button>/g)];
assert.deepEqual(detailToolButtons.map((match) => match[1]), ["reorganize", "summary", "minutes", "todos", "mark"], "详情右栏必须恰好保留五个约定 AI 工具按钮");
const laterToolTabRules = [...css.slice(stableToolTabRule).matchAll(/\.right-tool-dock \.tool-tab-bar\s*\{([^}]*)\}/g)];
assert.ok(!laterToolTabRules.some((match) => /display:\s*flex|flex-wrap:\s*wrap/.test(match[1])), "五列工具栏后续级联不能重新退化成可换行 flex");
assert.ok(js.includes('$("transcriptSearch")?.addEventListener("input"') && js.includes('$("transcriptSpeakerFilter")?.addEventListener("change"'), "中文搜索与发言人筛选必须保留交互事件绑定");
assert.ok(css.includes(".management-layout") && css.includes(".artifact-stale-banner") && css.includes(".speech-block-paragraph.is-active") && css.includes(".merged-segment-fragment.is-active"), "产品样式必须覆盖管理布局、过期提示和两种逐字稿播放高亮");

assert.ok(js.includes("loadModelServicesInBackground"), "模型服务健康探测必须后台加载，不能阻塞会议列表和其他核心业务数据的首屏渲染");

// Focused reviewer fixes for Task 7A:
// 1) source jumps must use a real media element/currentTime when playable audio exists;
// 2) voiceprint batch actions stay disabled until there is a visible selection;
// 3) template / voiceprint / meeting deletes must go through an in-app confirmation dialog.
assert.ok(html.includes('id="detailMediaElement"'), "detail workbench must expose a real media element for source seeks");
assert.ok(
  html.includes('id="batchDeleteVoiceprintBtn" disabled') && html.includes('id="batchDownloadVoiceprintBtn" disabled'),
  "voiceprint batch actions must stay disabled until selection exists",
);
assert.ok(
  html.includes('id="confirmActionDialog"') && html.includes('id="confirmActionConfirmBtn"'),
  "destructive actions must use an in-app confirmation dialog",
);
assert.ok(
  js.includes("seekDetailMediaElement(seekMs)") && js.includes("mediaElement.currentTime = Math.min"),
  "scrollToSourceSegment must perform a real media seek through currentTime when audio is playable",
);
assert.ok(
  js.includes("openActionConfirmDialog") && js.includes("confirmActionDialog"),
  "DELETE paths must route through the confirmation dialog instead of firing directly",
);
assert.ok(
  js.includes("deleteMeetingRecord") && js.includes("data-delete-record") && js.includes('method: "DELETE"'),
  "meeting record deletion must have an explicit frontend confirmation flow",
);
assert.ok(
  js.includes("syncVoiceprintSelectionState") && js.includes("selectCurrentVoiceprints"),
  "voiceprint batch button enablement must track the visible selection state",
);

assert.ok(
  js.includes('${renderArtifactStaleBanner(result)}') && js.includes('data-detail-tool-editor="${tool}"'),
  "流式 AI 工具完成卡片仍需保留必要的中文过期提示",
);
assert.ok(
  js.includes('tool === "minutes" ? renderMinutesVersionControls(result) : ""'),
  "纪要流式生成完成后必须立即显示模板与版本控件，不能只有重新打开历史记录时才显示",
);
assert.ok(
  js.includes("seekDetailMediaElement") && js.includes('addEventListener("loadedmetadata"') && js.includes("pendingSeekMs"),
  "来源跳转必须在媒体元数据尚未就绪时保存待定位时间，并在 loadedmetadata 后完成真实 seek",
);
assert.ok(
  js.includes("toggleDetailMediaPlayback") && js.includes("mediaElement.play()") && js.includes("mediaElement.pause()"),
  "详情底部传输按钮在存在真实录音源时必须控制媒体播放与暂停",
);
assert.ok(
  js.includes('addEventListener("timeupdate"') && js.includes("syncPlaybackActiveSegment"),
  "录音回放必须用 timeupdate 同步当前时间和高亮转写片段",
);
assert.ok(
  js.includes("loadDetailMediaSource") && js.includes("/exports/audio") && js.includes("URL.createObjectURL"),
  "详情页必须把后端 POST 音频导出转换为可播放 Blob URL，不能等待不存在的 meeting.audioUrl",
);
assert.ok(
  js.includes("hasMeetingRecordedMedia") && js.includes("录音正在加载"),
  "导入详情的底部按钮只能回放已上传录音，加载中不得回退为实时识别",
);

// 实时连接必须覆盖 CONNECTING 窗口，并以 socket + token 双重身份判断回调归属。
// 这组静态守卫专门防止用户连续点击后创建多个 WebSocket，以及旧连接稍后 close 时误停新会话。
assert.ok(js.includes("realtimeConnecting: false"), "实时状态必须显式记录 WebSocket 正在连接，不能只依赖 realtimeRunning");
assert.ok(js.includes("state.realtimeRunning || state.realtimeConnecting"), "连接建立前再次点击开始按钮必须被拦截");
assert.ok(js.includes("function isActiveRealtimeConnection(socket, sessionToken)"), "WebSocket 回调必须通过 socket 与 sessionToken 双重身份守卫");
assert.ok(js.includes("startMicrophoneCapture(socket, sessionToken)"), "麦克风采集必须捕获当前连接 token，不能在异步帧回调中读取可能已变化的全局 token");
const realtimeStartBody = js.slice(js.indexOf("async function startRealtimeTranscription"), js.indexOf("async function stopRealtimeTranscription"));
assert.ok(realtimeStartBody.includes("if (!isActiveRealtimeConnection(socket, sessionToken)) return;"), "旧 WebSocket 的 open/message/error/close 回调必须在修改全局状态前校验身份");

// 编辑区实际包含筛选栏、格式栏、正文和播放器四个直接子区，网格也必须有四行。
// 空会议时正文占据第三行，格式栏因此始终贴在顶部，不再被 1fr 垂直居中到页面中部。
const editorPanelRule = css.slice(css.indexOf(".editor-panel {"), css.indexOf("}", css.indexOf(".editor-panel {")));
assert.ok(editorPanelRule.includes("grid-template-rows: auto auto minmax(0, 1fr) auto"), "详情编辑区必须使用四行网格固定筛选栏、格式栏、正文和播放器");
assert.ok(
  css.includes("#transcriptWorkbenchBar {\n  grid-row: 1;")
    && css.includes(".editor-panel > .rich-toolbar {\n  grid-row: 2;")
    && css.includes(".editor-panel > .transcript-editor {\n  grid-row: 3;")
    && css.includes(".editor-panel > .bottom-audio-player {\n  grid-row: 4;"),
  "隐藏筛选栏后四个编辑器区域仍必须固定在各自网格行，播放器不能占据正文 1fr",
);
const richToolbarRules = [...css.matchAll(/\.rich-toolbar\s*\{([^}]*)\}/g)].map((match) => match[1]);
assert.ok(richToolbarRules.some((rule) => /align-content:\s*flex-start/.test(rule)), "空会议格式栏必须固定在顶部，不能垂直居中");
assert.ok(!richToolbarRules.some((rule) => /align-content:\s*center/.test(rule)), "后续 CSS 级联不能再次把格式栏推到编辑区中间");

// 筛选控件只在有可见逐字稿时出现；单发言人无需提供没有选择价值的筛选器。
assert.ok(js.includes("workbenchBar.hidden = !hasVisibleTranscript"), "没有转写内容时必须隐藏搜索与筛选工具栏");
assert.ok(js.includes("speakerFilter.hidden = speakerNames.length <= 1"), "发言人筛选只应在识别到多人时显示");
assert.ok(
  css.includes(".transcript-workbench-bar[hidden]") && css.includes("display: none"),
  "作者级 display:grid 规则不能覆盖 hidden 属性，空会议必须在视觉上真正隐藏搜索栏",
);

// 历史脏数据只在实时会议显示层过滤。精确的分号关键词串应隐藏，包含这些词的正常句子必须保留。
assert.ok(js.includes("function isPureRealtimeContextEcho(text)"), "前端必须提供严格的历史纯上下文回声判断函数");
assert.ok(js.includes('"智能转写;声纹注册;强制对齐"'), "纯上下文回声判断必须覆盖截图中的历史错误文本");
assert.ok(js.includes("!imported && isPureRealtimeContextEcho(segment.text)"), "纯回声过滤只能作用于实时详情，不能污染导入转写数据");
assert.ok(js.includes("const displayableSegments"), "详情渲染必须基于经过显示层清洗的片段集合，且不修改后端原始数据");

// AI 工具可以保留内部溯源字段用于后端计算，但前端卡片绝不能暴露 revision、segmentId、rt-rec 或 source ranges。
const artifactBannerBody = js.slice(js.indexOf("function renderArtifactStaleBanner"), js.indexOf("function scrollToSourceSegment"));
for (const forbidden of ["<span>Revision", "sourceRanges", "segmentId", "data-source-segment", "Regenerate", "Transcript changed"]) {
  assert.ok(!artifactBannerBody.includes(forbidden), `AI 通用结果卡不能展示内部溯源标记：${forbidden}`);
}
const minutesControlsBody = js.slice(js.indexOf("function renderMinutesVersionControls"), js.indexOf("function renderDetailToolResult"));
for (const forbidden of ["sourceRanges", "segmentId", "data-minutes-source-ranges", "source revision", "revision ${", "Transcript changed"]) {
  assert.ok(!minutesControlsBody.includes(forbidden), `纪要版本区不能展示内部溯源标记：${forbidden}`);
}
assert.ok(!js.includes(">Revision "), "任何 AI 工具界面都不能直接输出 Revision");

// 逐字稿工作台必须只做展示层合并，并通过一个带 revision 的批量请求保存所有底层片段。
assert.ok(js.includes("function groupTranscriptSegments"), "逐字稿必须提供连续同人展示分组函数");
assert.ok(js.includes("gapMs <= 3000") && js.includes("previousGroup.identityKey === identityKey"), "仅相邻同身份且间隔不超过3秒的片段可以合并展示");
assert.ok(js.includes("transcript-merged-text") && js.includes("merged-segment-fragment"), "只读态必须把连续同人渲染成一段正文，同时为每个底层片段保留稳定来源标识");
assert.ok(!js.includes("已合并 ${block.segments.length} 个连续片段"), "合并后的正文不能继续显示内部片段数量等工程提示");
assert.ok(js.includes("expectedTranscriptRevision") && js.includes("/segments/batch"), "逐字稿保存必须使用带乐观锁的原子批量接口");
assert.ok(html.includes('id="saveTranscriptBtn"') && html.includes('id="cancelTranscriptEditBtn"'), "纯文本编辑态必须提供全局保存和取消入口");

// 快速会议弹窗必须使用语义分区和紧凑开关，避免原生复选框、必填星号和操作按钮错行。
assert.ok(html.includes("quick-meeting-scroll-area") && html.includes("quick-meeting-footer"), "快速会议必须采用固定标题、可滚动内容和固定操作栏结构");
assert.ok((html.match(/class="quick-meeting-section"/g) || []).length === 3, "快速会议配置必须按基本信息、识别设置、纪要与附件分为三个区块");
assert.ok((html.match(/class="switch-card"/g) || []).length === 2, "会议转写和声纹区分必须使用一致的紧凑开关卡片");
assert.ok(css.includes("grid-template-rows: auto minmax(0, 1fr) auto") && css.includes(".switch-card input[type=\"checkbox\"]"), "快速会议弹窗必须固定头尾并自定义开关外观");
assert.ok(html.includes('id="transcriptFontFamily"') && html.includes('id="transcriptFontSize"'), "用户要求恢复字体和字号工具");
assert.ok(
  css.includes(".transcript-edit-toolbar #transcriptFontFamily")
    && css.includes("min-width: 94px")
    && css.includes("min-width: 68px"),
  "字体和字号下拉框必须保留独立最小宽度，不能在中栏被挤压重叠",
);
assert.ok(html.includes('data-transcript-style="bold"') && html.includes('data-transcript-style="align"'), "常用显示样式工具必须完整恢复");
assert.ok(js.includes("transcriptViewStyle") && js.includes("applyTranscriptViewStyle"), "恢复的样式按钮必须具备真实显示行为，不能成为假按钮");
assert.ok(!html.includes('contenteditable="true"'), "逐字稿底层仍使用按 segment 保存的纯文本编辑器");

// 识别优化和禁忌词的候选必须显式确认，导入/导出/启停不能再是提示按钮。
assert.ok(js.includes("confirmDocumentKeywords") && js.includes("renderDocumentKeywordPicker"), "文档关键词必须存在人工确认流程");
assert.ok(js.includes("confirmSmartKeywords") && js.includes("confirmedTerms"), "智能关键词必须确认后才进入识别配置");
assert.ok(js.includes("parseWordListFile") && js.includes("downloadWordList"), "关键词与禁忌词导入、下载按钮必须接入真实文件接口");
assert.ok(js.includes("toggleReplacementRule") && js.includes("toggleSensitiveRule"), "替换和禁忌词规则必须支持启停");
assert.ok(html.includes('id="sensitiveScopeAi"') && html.includes('id="sensitiveScopeExport"'), "禁忌词必须显式选择展示、AI、导出目标范围");

// 图四中的原生巨型控件和详情假按钮已经由紧凑控件与真实媒体行为替代。
assert.ok(html.includes('id="rewindAudioBtn"') && html.includes('id="forwardAudioBtn"') && html.includes('id="playbackRateBtn"'), "播放器前后5秒和倍速必须有可绑定的真实按钮");
assert.ok(js.includes("downloadMeetingArchive") && js.includes("/api/meetings/exports/archive"), "批量导出必须调用服务端 ZIP 接口");
assert.ok(js.includes("openAssignSpeakerDialog") && js.includes("selectedTranscriptSegmentIds"), "添加发言人必须基于用户选中的底层片段原子分配");

console.log("prototype spec ok");
