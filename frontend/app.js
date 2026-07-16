// 智能会议系统前端主脚本。
// 本轮已经从“展示型原型”升级为“API 驱动的讯飞风政企工作台”：
// 1. 页面主数据全部来自 FastAPI 后端；
// 2. 不使用 prompt/alert/localStorage 保存业务数据；
// 3. 每个可见功能按钮都尽量映射到稳定后端 API，后续接真实模型时不改页面结构。
const API_BASE = window.MEETING_API_BASE || new URLSearchParams(window.location.search).get("api") || "http://127.0.0.1:8001";
// 实时 ASR 需要在“低延迟”和“句子完整度”之间取平衡；3 秒很容易把一句话切断。
// 这里改成 15 秒窗口，并通过 WebSocket 配置告诉后端，保证时间轴和后端分段一致。
// 真实 qwen3-asr-flash 对过短分片容易只返回几个字；15 秒能保留更多上下文，仍能接受会议现场的低延迟。
const REALTIME_FLUSH_MS = 15000;
// 浏览器麦克风在无声、会议室空调声或系统虚拟输入下仍会产生 PCM 数据。
// 如果把这些低能量分片直接送进 Qwen3-ASR，同步 ASR 可能会“幻听”出一串不相关文字。
// 下面几个阈值只拦截明显静音/底噪。它们是“基础门限”，真正判断时还会结合浏览器采集到的
// 噪声底动态抬高门槛：安静房间里的小声说话不被误杀，空调声/纯底噪也不会直接送进 ASR。
const REALTIME_MIN_CHUNK_MS = 800;
const REALTIME_MIN_RMS = 0.0025;
const REALTIME_MIN_PEAK = 0.012;
const REALTIME_ACTIVE_SAMPLE_LEVEL = 0.006;
const REALTIME_MIN_ACTIVE_RATIO = 0.002;
const REALTIME_SKIP_NOTICE_MS = 15000;
// 原来的 600ms 静音端点会把“因为……我们是不是……”这类带自然停顿的句子拆成
// 1～2 秒短片段：ASR 缺少上下文时更容易把“图标”猜成“股票”，短片段也会放大
// CAM++ 声纹波动。会议模式统一给自然停顿保留 1200ms，并把兜底窗口放宽到 15 秒；
// 代价是最终句最多晚约 0.6 秒出现，但中间结果仍实时展示，准确率和说话人稳定性更重要。
const REALTIME_SILENCE_END_MS = 1200;
const REALTIME_SENTENCE_END_SILENCE_MS = 700;
const REALTIME_MAX_SEGMENT_MS = 15000;
// 产品级实时转写不能把“嗯、对、那个”这类 1 秒内的短碎片直接写进正文。
// 这里同时看“累计像人声的时长”和“整段上下文窗口”：前者挡掉环境声/口头停顿，
// 后者给同步 ASR 留出足够上下文，减少模型只凭半句话猜词。
// 这里不是越长越准：同步 ASR 的确需要上下文，但窗口过长会让用户感觉“我说完很久才出现文字”。
// 参考主流实时转写的做法，前端先挡掉 1 秒以内的碎片，再把约 3 秒的稳定语音块送去识别；
// 最大 15 秒兜底负责处理连续讲话场景，避免一直等静音端点导致页面没有响应。
const REALTIME_MIN_FINAL_SPEECH_MS = 1200;
const REALTIME_MIN_FINAL_SEGMENT_MS = 3000;
const REALTIME_OVERLAP_MS = 300;
const REALTIME_IDLE_BUFFER_MS = 300;
const REALTIME_SERVER_SYNC_MS = 1000;
// The current backend realtime path is still "short WAV chunk -> ASR request", not a vendor-native
// streaming recognizer with continuous interim hypotheses. Passing a short transcript tail gives the
// model enough linguistic context to keep names, pronouns, and unfinished phrases stable across chunks
// without resending the whole meeting transcript on every WebSocket frame.
const REALTIME_CONTEXT_TAIL_CHARS = 240;
const REALTIME_STREAMING_MODE = "dashscope_realtime";
const REALTIME_STREAM_SAMPLE_RATE = 16000;

const $ = (id) => document.getElementById(id);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const DETAIL_TOOL_TITLES = {
  reorganize: "语篇规整结果",
  summary: "AI 摘要",
  minutes: "会议纪要",
  todos: "会议待办",
  mark: "标记结果",
};
const DETAIL_TOOL_PROGRESS = {
  reorganize: ["读取转写片段", "重组语篇层次", "整理规整正文", "完成生成"],
  summary: ["分析会议上下文", "提炼关键要点", "生成摘要正文", "完成生成"],
  minutes: ["匹配纪要模板", "填充会议段落", "整理纪要正文", "完成生成"],
  todos: ["识别任务表达", "抽取负责人和期限", "整理待办列表", "完成生成"],
  mark: ["读取选中文本", "保存重点标记", "整理标记说明", "完成生成"],
};

function isImportedMeeting(meeting) {
  // 后端的导入转写接口会把上传文件创建成一条会议型记录，并统一写入 audioSource="上传文件"。
  // 会议列表只展示实时/快速会议，所以这里集中识别上传导入记录，避免两个台账互相串数据。
  return meeting?.audioSource === "上传文件";
}

function meetingRecords() {
  // 这里保留函数而不是在渲染里直接 filter，是为了让“会议列表”和“导入转写”的边界足够清晰。
  return state.meetings.filter((meeting) => !isImportedMeeting(meeting));
}

function importRecords() {
  // 导入台账只显示上传音视频产生的记录；用户在会议列表里创建的快速会议不会混进这里。
  return state.meetings.filter((meeting) => isImportedMeeting(meeting));
}

const state = {
  route: "records",
  currentMeetingId: "",
  meetings: [],
  overview: [],
  voiceprints: [],
  voiceprintGroups: [],
  // Capability state comes from an independent backend probe. It is intentionally separate from
  // the personnel list so a cached or pending profile cannot make sample enrollment look ready.
  modelServices: {},
  speakerCorrectionSaving: false,
  speakerRenameMode: "rename",
  keywordLibraries: [],
  manualKeywords: [],
  // 文档关键词只有在“抽取后人工确认”且被本次会议显式勾选时才进入 ASR。
  // 这里分别保存服务端文档列表、当前待确认候选以及两类创建入口的勾选状态，避免普通重绘丢失用户选择。
  optimizationDocuments: [],
  pendingDocumentKeywordId: "",
  pendingDocumentKeywords: [],
  selectedCreateDocumentIds: new Set(),
  selectedImportDocumentIds: new Set(),
  smartKeywordCandidates: [],
  smartKeywordMeetingId: "",
  sensitiveRules: [],
  // Display-safe transcript data is intentionally separate from ``meetings[].segments``.  The
  // latter is the stored source contract, while this cache may contain masks that must never be
  // submitted through the existing segment PATCH route.
  displayTranscriptViews: {},
  displayTranscriptViewLoading: {},
  replacementRules: [],
  replacementEditingId: "",
  sensitiveEditingId: "",
  templates: [],
  // Minutes selections are scoped by meeting ID. Keeping them outside the meeting payload avoids
  // accidentally treating a viewer's chosen historical version as a server-side current pointer.
  minutesVersions: {},
  minutesVersionIds: {},
  minutesTemplateIds: {},
  rooms: [],
  jobs: [],
  selectedFiles: [],
  createMeetingAttachmentFile: null,
  // 导入转写的运行态只用于当前页面展示，不落库；真正的会议、文件、任务状态仍以后端为准。
  // 之前点击“开始处理”后前端会一直等待后端转写完成，用户看不到任何即时反馈，容易误以为按钮失效。
  // 这里用文件名映射每个文件的阶段，让上传、转写、完成、失败都能立刻显示在页面上。
  importProcessing: false,
  importFileStatuses: {},
  // 导入台账和转写详情现在属于同一个“导入转写”模块。importView 只控制模块内部视图，
  // 不再通过独立 detail 路由切页，避免用户从左侧导航进入一个没有上下文的详情空页。
  importView: "ledger",
  importResults: [],
  // 详情编辑器被实时会议和导入转写复用，但这两个入口是独立功能。
  // detailMode 标记当前详情属于哪条业务线，detailWorkspaceKey 用来在切换入口或会议时重置右侧 AI 结果。
  detailMode: "import",
  detailWorkspaceKey: "",
  // 详情工作台被“在线会议详情”和“导入转写详情”共用，所以左右栏折叠不能写成某个路由的临时 DOM 状态。
  // 放在统一 state 中可以保证用户在两种入口之间切换时，三栏比例、收起按钮和可访问状态始终同步。
  detailPanelCollapsed: { speaker: false, tools: false },
  detailNavigationMode: "speakers",
  // 右侧五个 AI 工具调用的是后端长链路接口。单独记录当前运行中的工具，按钮本身就能出现旋转反馈，
  // 即使右侧结果面板正在刷新，也不会让用户误以为点击没有生效。
  runningDetailTool: "",
  activeDetailTool: "",
  // Transcript workspace controls are presentation state only.  Keeping them out of the meeting
  // payload prevents a user's local search/filter choice from becoming a persisted transcript edit.
  transcriptQuery: "",
  transcriptSpeakerFilter: "",
  playbackActiveSegmentId: "",
  // 音频刚载入时浏览器会在 00:00 主动触发 loadedmetadata/timeupdate。只有用户点击来源、
  // 前后跳转或真正开始播放后才允许高亮逐字稿，避免“尚未播放却整块标绿”的假定位状态。
  playbackInteractionStarted: false,
  // 逐字稿编辑态始终绑定进入编辑时的 revision，并按底层 segment id 保存草稿。
  // 连续同一发言人虽然显示为一个框，但这里绝不把 segment 真正合并，保证音频和 AI 来源稳定。
  transcriptEditMode: false,
  transcriptEditRevision: 0,
  transcriptEditOriginals: {},
  transcriptEditDrafts: {},
  transcriptUndoStack: [],
  transcriptRedoStack: [],
  transcriptHistoryLock: false,
  transcriptSaveStatus: "readonly",
  selectedTranscriptSegmentIds: new Set(),
  // 恢复旧工作台中的字体工具，但它们只属于当前浏览器的阅读/编辑偏好。
  // 逐字稿 API 仍只提交纯文本，避免把样式标记混入 segment 文本、AI 来源和导出内容。
  transcriptViewStyle: {
    fontFamily: "source-han",
    fontSize: 18,
    bold: false,
    italic: false,
    underline: false,
    strike: false,
    colorIndex: 0,
    alignIndex: 0,
  },
  // Uploaded recordings are returned by a POST export endpoint, which cannot be assigned directly
  // to <audio src>. Keep one scoped Blob URL and revoke it whenever the selected meeting changes.
  detailMediaSourceKey: "",
  detailMediaObjectUrl: "",
  detailMediaLoadingKey: "",
  // 每次启动右侧 AI 工具都会递增 token。流式进度和接口返回必须带着启动时的 token 回来校验，
  // 否则用户从“待办”切到“纪要”时，旧请求仍可能把当前面板覆盖成上一项的“正在生成”。
  detailToolRunToken: 0,
  // AI 工具草稿按 meetingId:tool 缓存在前端，并在用户点击“保存”后写入后端 aiToolDrafts。
  // 这样用户从“AI 摘要”切到“纪要/待办/标记”再回来时，面板优先回显已生成/已保存内容，不会被普通 Tab 点击重新触发生成。
  detailToolDrafts: {},
  // 浏览器点击右侧工具按钮时可能会清空文本选择，所以把转写编辑区最近一次选中文本缓存下来，
  // “标记”按钮优先使用这份缓存，保证左侧选中哪段文字，右侧标记结果就落哪段文字。
  detailSelectedText: "",
  detailSelectedSegmentId: "",
  selectedVoiceprintGroupId: "vg-all",
  voiceprintGroupEditingId: "",
  selectedVoiceprintIds: new Set(),
  selectedMeetingIds: new Set(),
  // A delete operation remains inert until this staged callback is explicitly confirmed in-app.
  pendingActionConfirmation: null,
  templateSource: "my",
  // 导入模板弹窗的临时状态只存在于当前页面会话中，不作为业务数据保存；
  // 真正的模板数据仍通过后端 API 写入 SQLite/后续 KingbaseES。
  importingTemplateFile: null,
  importingTemplateParsed: null,
  importingTemplateTags: ["会议主题", "会议时间", "会议地点", "主持人", "记录人", "参会人", "会议纪要"],
  // 实时会议运行态。后端已经提供 `/api/meetings/{id}/realtime` WebSocket，
  // 这里保存 socket 和定时器，确保播放按钮、继续转写、暂停、结束都有真实动作。
  realtimeSocket: null,
  realtimeTimer: null,
  realtimeRecorder: null,
  realtimeMediaStream: null,
  realtimeAudioContext: null,
  realtimeAudioSource: null,
  realtimeAudioProcessor: null,
  realtimeCaptureMode: "",
  realtimeAudioBuffers: [],
  realtimeAudioSampleRate: 16000,
  realtimeSegmentStartMs: 0,
  realtimeCapturedMs: 0,
  realtimeSpeechStarted: false,
  realtimeSpeechAccumulatedMs: 0,
  realtimeLastVoiceMs: 0,
  realtimePendingChunkMeta: null,
  realtimeInFlightChunks: 0,
  realtimeLastTranscriptAt: 0,
  realtimeSyncTimer: null,
  realtimeSyncing: false,
  realtimeLastServerSyncAt: 0,
  realtimeRunning: false,
  // WebSocket 从构造到 open 之间仍处于 CONNECTING。这个窗口内 realtimeRunning 还是 false，
  // 如果不单独上锁，快速连点“开始会议”会创建多个连接，并让旧连接的 close 回调误停新采集。
  realtimeConnecting: false,
  // realtimeStatus 专门描述实时识别链路当前处于哪个阶段：
  // realtimeRunning 只能说明连接/采集是否启动，不能区分“正在采集但音量低”“ASR 空结果”“用户主动暂停”。
  realtimeStatus: "idle",
  realtimeStatusDetail: {},
  realtimeDraftText: "",
  realtimeUseStreaming: false,
  realtimeStopIntent: "",
  realtimeStopResolver: null,
  realtimeSessionToken: "",
  realtimeActiveMeetingId: "",
  realtimeNoiseFloorRms: 0,
  realtimeNoiseFloorSamples: 0,
  realtimeChunkIndex: 0,
  // 低质量分片可能会连续出现，提示需要节流，避免每 8 秒弹一次 toast 干扰会议记录。
  lastRealtimeSkipNoticeAt: 0,
  optimizationTab: "manual",
  optimizationLanguage: "zh",
  entityDialog: { type: "", id: "" },
  serviceError: "",
};

async function apiRequest(path, options = {}) {
  const isFormData = options.body instanceof FormData;
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: isFormData ? options.headers || {} : { "Content-Type": "application/json", ...(options.headers || {}) },
    body: isFormData || options.body == null || typeof options.body === "string" ? options.body : JSON.stringify(options.body),
  });
  if (!response.ok) {
    let detail = `请求失败：${response.status}`;
    try {
      const data = await response.json();
      detail = data.detail || data.message || detail;
    } catch {
      // 非 JSON 错误体保留默认 HTTP 状态提示，避免二次异常掩盖真实原因。
    }
    const error = new Error(detail);
    // 调用方需要区分 revision 冲突和普通失败；保留 HTTP 状态可避免用文案猜错误类型。
    error.status = response.status;
    throw error;
  }
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json") ? response.json() : response.blob();
}

async function loadModelServicesInBackground() {
  // Model health can legitimately take several seconds when the local CAM++/VAD processes are
  // starting. It is supplementary capability metadata, so awaiting it in the same Promise.all as
  // meetings made every route look empty until the slowest probe finished. Load it independently:
  // the voiceprint page starts in an explicit checking state, then rerenders only that manager when
  // the truthful readiness response arrives. A failed probe remains visible as unavailable and never
  // fabricates a ready model or prevents meetings, templates, dictionaries, and rooms from loading.
  state.modelServices = {
    voiceprint: { ready: false, mode: "checking", message: "正在检查声纹模型服务" },
  };
  try {
    state.modelServices = await apiRequest("/api/model-services/status");
  } catch (error) {
    state.modelServices = {
      voiceprint: { ready: false, mode: "unavailable", message: error.message || "runtime unavailable" },
    };
  }
  renderVoiceprintManager();
}

async function loadInitialData() {
  try {
    state.serviceError = "";
    // Start the slower model probe concurrently but intentionally do not await it. Core product data
    // should reach the first viewport as soon as its own APIs complete.
    void loadModelServicesInBackground();
    const [overview, meetings, voiceprints, groups, libraries, manual, documents, rules, replacements, templates, rooms] = await Promise.all([
      apiRequest("/api/dashboard/overview"),
      apiRequest("/api/meetings"),
      apiRequest("/api/voiceprints"),
      apiRequest("/api/voiceprint-groups"),
      apiRequest("/api/dictionaries/keyword-libraries"),
      apiRequest("/api/optimization/manual-keywords"),
      apiRequest("/api/optimization/document-keywords/files"),
      apiRequest("/api/dictionaries/sensitive-rules"),
      apiRequest("/api/optimization/replacement-rules"),
      apiRequest("/api/minute-templates?source=all"),
      apiRequest("/api/meeting-rooms"),
    ]);
    state.overview = overview.items || [];
    state.meetings = meetings.items || [];
    // Source records may have changed through realtime persistence or another browser.  Drop only
    // derived display snapshots so the next detail render refetches the matching frozen safe view.
    state.displayTranscriptViews = {};
    state.voiceprints = voiceprints.items || [];
    state.voiceprintGroups = groups.items || [];
    state.keywordLibraries = libraries.items || [];
    state.manualKeywords = manual.items || [];
    state.optimizationDocuments = documents.items || [];
    state.sensitiveRules = rules.items || [];
    state.replacementRules = replacements.items || [];
    state.templates = templates.items || [];
    state.rooms = rooms.items || [];
    state.currentMeetingId = state.currentMeetingId || meetingRecords()[0]?.id || state.meetings[0]?.id || "";
  } catch (error) {
    state.serviceError = `${error.message}。请确认后端已启动在 ${API_BASE}`;
    showToast(state.serviceError, "error");
  }
  render();
}

async function refreshConfigData() {
  const [voiceprints, groups, libraries, manual, documents, rules, replacements, templates, modelServices] = await Promise.all([
    apiRequest("/api/voiceprints"),
    apiRequest("/api/voiceprint-groups"),
    apiRequest("/api/dictionaries/keyword-libraries"),
    apiRequest("/api/optimization/manual-keywords"),
    apiRequest("/api/optimization/document-keywords/files"),
    apiRequest("/api/dictionaries/sensitive-rules"),
    apiRequest("/api/optimization/replacement-rules"),
    apiRequest("/api/minute-templates?source=all"),
    apiRequest("/api/model-services/status").catch((error) => ({
      voiceprint: { ready: false, mode: "unavailable", message: error.message || "runtime unavailable" },
    })),
  ]);
  state.voiceprints = voiceprints.items || [];
  state.voiceprintGroups = groups.items || [];
  state.modelServices = modelServices || {};
  state.keywordLibraries = libraries.items || [];
  state.manualKeywords = manual.items || [];
  state.optimizationDocuments = documents.items || [];
  state.sensitiveRules = rules.items || [];
  state.replacementRules = replacements.items || [];
  state.templates = templates.items || [];
  render();
}

async function refreshMeetings() {
  const params = new URLSearchParams({
    search: $("recordSearch")?.value || "",
    status: $("statusFilter")?.value || "all",
    minutesStatus: $("minutesFilter")?.value || "all",
    libraryId: $("libraryFilter")?.value || "all",
    date: $("dateFilter")?.value || "",
  });
  const data = await apiRequest(`/api/meetings?${params.toString()}`);
  state.meetings = data.items || [];
  state.currentMeetingId = state.currentMeetingId || meetingRecords()[0]?.id || state.meetings[0]?.id || "";
  renderRecordsPage();
}

function routeTo(route) {
  state.route = route;
  $$(".page").forEach((page) => page.classList.toggle("active", page.id === `page-${route}`));
  $$(".side-nav-item").forEach((item) => item.classList.toggle("active", item.dataset.route === route));
  const titles = {
    records: ["会议列表", "创建快速会议并管理实时转写、纪要、待办与对接状态。"],
    // 标题副文案保持功能导向，避免在具体业务页重复出现产品名造成视觉噪声。
    import: ["导入转写", "导入音视频生成离线转写记录，处理完成后进入编辑详情。"],
    voiceprints: ["声纹库", "按分组维护发言人声纹模型和注册样本。"],
    hotwords: ["识别优化", "手动、文档、智能和强制替换四类识别优化。"],
    sensitive: ["禁忌词", "配置禁忌词显示方式、大小写和启用范围。"],
    templates: ["纪要模板", "维护我的模板和系统模板。"],
    integration: ["系统对接", "跟踪普通会议系统的推送、归档和回传状态。"],
  };
  const [title, subtitle] = titles[route] || titles.records;
  $("pageTitle").textContent = title;
  $("pageSubtitle").textContent = subtitle;
  render();
  if (route === "import" && state.importView === "detail") loadMeetingJobs();
}

function render() {
  renderServiceBanner();
  renderTemplateSelects();
  renderRecordsPage();
  renderImportPage();
  renderMeetingDetailWorkspace();
  renderVoiceprintManager();
  renderOptimizationCenter();
  renderSensitivePage();
  renderTemplateCenter();
  renderIntegrationPage();
}

function renderServiceBanner() {
  const banner = $("serviceBanner");
  banner.hidden = !state.serviceError;
  banner.textContent = state.serviceError;
}

function renderRecordsPage() {
  renderLibraryFilter();
  $("recordsOverview").innerHTML = state.overview.map((item) => `
    <article class="overview-card"><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.value)}</strong><em>${escapeHtml(item.hint || "")}</em></article>
  `).join("");
  const records = meetingRecords();
  const visibleIds = new Set(records.map((item) => String(item.id)));
  state.selectedMeetingIds = new Set(Array.from(state.selectedMeetingIds).filter((id) => visibleIds.has(String(id))));
  if ($("batchExportBtn")) $("batchExportBtn").disabled = state.selectedMeetingIds.size === 0;
  $("recordsTotal").textContent = `共 ${records.length} 条`;
  $("recordsTableBody").innerHTML = records.length ? records.map((meeting, index) => `
    <tr>
      <td><input type="checkbox" data-record-select="${escapeHtml(meeting.id)}" ${state.selectedMeetingIds.has(String(meeting.id)) ? "checked" : ""} aria-label="选择${escapeHtml(meeting.meetingName || meeting.fileName || "会议")}" /></td>
      <td>${index + 1}</td>
      <td><button class="file-link" data-open-meeting="${meeting.id}">▣ ${escapeHtml(meeting.meetingName || meeting.fileName || "未命名会议")}</button></td>
      <td>${formatMeetingDuration(meeting)}</td>
      <td class="mono">${escapeHtml(meeting.createdAt || "")}</td>
      <td>${badge(processStatusText(meeting.processStatus), meeting.processStatus)}</td>
      <td>${badge(minutesStatusText(meeting.minutesStatus), meeting.minutesStatus)}</td>
      <td class="row-actions"><button data-open-meeting="${meeting.id}">查看</button><button data-download-record="${meeting.id}">下载</button><button data-delete-record="${meeting.id}">删除</button></td>
    </tr>
  `).join("") : `<tr><td colspan="8" class="empty-cell">暂无会议，请点击“快速会议”创建并开启实时转写。</td></tr>`;
}

function openMeetingDetail(meetingId) {
  // 实时/快速会议和导入转写共用一套转写编辑工作台，但入口语义不同：
  // 会议列表打开的是“实时会议详情”，导入台账打开的是“导入转写详情”。
  if (String(meetingId || "") !== String(state.currentMeetingId || "") || state.detailMode !== "realtime") {
    resetTranscriptEditing();
  }
  state.currentMeetingId = meetingId || state.currentMeetingId;
  state.detailMode = "realtime";
  state.importView = "detail";
  // Realtime detail and import detail share the same DOM, so entering the realtime lane must invalidate
  // any visible import-generated AI card before render has a chance to reuse it.
  state.detailWorkspaceKey = "";
  state.activeDetailTool = "";
  state.runningDetailTool = "";
  if (!state.realtimeRunning) setRealtimeStatus("idle");
  nextDetailToolRunToken();
  routeTo("import");
  $$(".side-nav-item").forEach((item) => item.classList.toggle("active", item.dataset.route === "records"));
  $("pageTitle").textContent = "实时会议";
  $("pageSubtitle").textContent = "查看实时转写、发言人、纪要、待办和标记。";
  loadMeetingJobs().catch((error) => showToast(error.message || "任务状态加载失败", "error"));
}

function openImportDetail(meetingId) {
  // Import detail is a separate product flow from realtime meetings. Reset the shared AI panel state so
  // a previous realtime summary/minutes draft cannot appear beside an offline transcription.
  state.detailWorkspaceKey = "";
  state.activeDetailTool = "";
  state.runningDetailTool = "";
  nextDetailToolRunToken();
  // 导入台账里的“查看”进入同一个导入转写模块详情区。
  // 这里先设置 meetingId 和 importView，再走 routeTo("import")，保证顶部标题、左侧导航和详情数据同步刷新。
  if (String(meetingId || "") !== String(state.currentMeetingId || "") || state.detailMode !== "import") {
    resetTranscriptEditing();
  }
  state.currentMeetingId = meetingId || state.currentMeetingId;
  state.detailMode = "import";
  state.importView = "detail";
  if (!state.realtimeRunning) setRealtimeStatus("idle");
  routeTo("import");
  loadMeetingJobs().catch((error) => showToast(error.message || "任务状态加载失败", "error"));
}

function backToImportLedger() {
  // 返回台账只切换模块内部视图，不清空 currentMeetingId；用户再次进入详情时仍可保留最近上下文。
  state.importView = "ledger";
  routeTo("import");
}

function segmentFingerprint(meeting = getCurrentMeeting()) {
  // 右侧 AI 工具的结果依赖转写正文。只按 meetingId 缓存会导致“同一详情页新增实时片段后仍显示旧摘要”；
  // 这里取片段数量、最后片段时间戳和文本长度组成轻量指纹，既稳定又避免把整篇转写塞进 key。
  const segments = meeting?.segments || [];
  const last = segments[segments.length - 1] || {};
  const textSize = segments.reduce((sum, segment) => sum + String(segment.text || "").length, 0);
  return `${segments.length}:${last.id || "none"}:${last.startMs || 0}:${last.endMs || 0}:${textSize}`;
}

function detailContextKey(meeting = getCurrentMeeting()) {
  return `${state.detailMode}:${meeting?.id || state.currentMeetingId || "none"}:${segmentFingerprint(meeting)}`;
}

function detailWorkspaceContextKey(meeting) {
  return detailContextKey(meeting);
}

function resetDetailToolPanelForContext(meeting) {
  const nextKey = detailWorkspaceContextKey(meeting);
  if (state.detailWorkspaceKey === nextKey) return;
  state.detailWorkspaceKey = nextKey;
  state.activeDetailTool = "";
  state.runningDetailTool = "";
  nextDetailToolRunToken();
  const panel = $("detailToolPanel");
  if (panel) {
    // 右侧 AI 面板的 DOM 是共享的。每次切换“实时会议/导入转写”或切换会议时都要清空旧结果，
    // 防止导入转写生成的摘要残留到实时会议详情里。
    const label = state.detailMode === "import" ? "导入转写" : "实时会议";
    panel.innerHTML = `<article class="tool-result-card detail-tool-card detail-tool-empty"><p>请选择右侧工具生成当前${label}的结果。</p></article>`;
  }
  setDetailToolRunning("", false);
}

function renderImportPage() {
  renderHotwordPicker("importHotwordPicker");
  const ledgerView = $("importLedgerView");
  const detailView = $("importDetailView");
  if (ledgerView && detailView) {
    ledgerView.hidden = state.importView !== "ledger";
    detailView.hidden = state.importView !== "detail";
  }
  const keyword = ($("importSearchInput")?.value || "").trim();
  const uploadedRows = state.selectedFiles.map((file, index) => ({
    id: `selected-${index}`,
    fileName: file.name,
    createdAt: "刚刚",
    duration: "待识别",
    statusText: state.importFileStatuses[file.name] || "待上传",
    meetingId: state.importResults.find((item) => item.fileName === file.name)?.meeting?.id || "",
  }));
  const historyRows = importRecords().map((meeting) => ({
    id: meeting.id,
    fileName: meeting.fileName || meeting.meetingName || "未命名文件",
    createdAt: meeting.createdAt || "",
    duration: formatMeetingDuration(meeting),
    statusText: processStatusText(meeting.processStatus),
    meetingId: meeting.id,
  }));
  // 表格先显示本批次文件，再补充历史导入记录；按文件名搜索时两类数据一起过滤，符合参考图的“文件名称”搜索。
  const rows = [...uploadedRows, ...historyRows]
    .filter((row, index, array) => array.findIndex((item) => item.fileName === row.fileName && item.meetingId === row.meetingId) === index)
    .filter((row) => !keyword || row.fileName.includes(keyword));
  $("importFileList").innerHTML = rows.length ? rows.map((row, index) => `
    <tr>
      <td><input type="checkbox" /></td>
      <td>${index + 1}</td>
      <td>${escapeHtml(row.fileName)}</td>
      <td>${escapeHtml(row.duration)}</td>
      <td>${escapeHtml(row.createdAt)}</td>
      <td>${escapeHtml(row.statusText)}</td>
      <td class="row-actions">
        ${row.meetingId ? `<button data-open-import="${escapeHtml(row.meetingId)}">查看</button><button data-download-record="${escapeHtml(row.meetingId)}">下载</button>` : `<button disabled>处理中</button>`}
        ${row.meetingId ? `<button data-delete-record="${escapeHtml(row.meetingId)}">删除</button>` : ""}
      </td>
    </tr>
  `).join("") : `<tr><td colspan="7" class="empty-cell">暂无导入记录，请点击“导入音视频”选择文件。</td></tr>`;
  if ($("importTotal")) $("importTotal").textContent = `共${rows.length}条记录`;
  const startButton = $("startImportBtn");
  if (startButton) {
    startButton.disabled = state.importProcessing || state.selectedFiles.length === 0;
    startButton.textContent = state.importProcessing ? "转写处理中…" : `开始转写${state.selectedFiles.length ? `（${state.selectedFiles.length}）` : ""}`;
  }
}

function formatMeetingDuration(meeting) {
  const durationMs = Math.max(0, ...(meeting.segments || []).map((segment) => Number(segment.endMs || segment.startMs || 0)));
  return durationMs ? formatTime(durationMs) : "未知";
}

async function loadDisplayTranscriptView(meetingId) {
  // The browser never derives masking from mutable global rule data.  The backend returns the
  // meeting's frozen display policy instead, and this cache is deliberately not merged into the
  // editable meeting record so a mask cannot overwrite stored transcript source text.
  state.displayTranscriptViewLoading[meetingId] = true;
  try {
    const view = await apiRequest(`/api/meetings/${meetingId}/transcript-view?target=display`);
    if (view?.meetingId === meetingId && view.target === "display") {
      state.displayTranscriptViews[meetingId] = view;
      if (meetingId === state.currentMeetingId) renderMeetingDetailWorkspace();
    }
  } catch (error) {
    // Keep the editor blank instead of falling back to stored source text when the safe view is
    // unavailable.  A transient error must not become an accidental disclosure in the UI.
    if (meetingId === state.currentMeetingId) showToast(error.message || "安全展示内容加载失败", "warning");
  } finally {
    delete state.displayTranscriptViewLoading[meetingId];
  }
}

function renderPipeline() {
  if (!$("importPipeline")) return;
  const job = state.jobs[0];
  const steps = job?.steps || ["uploaded", "transcoding", "asr", "voiceprint", "alignment", "minutes", "completed"];
  const labels = { uploaded: "上传", transcoding: "转码", asr: "ASR", voiceprint: "声纹", alignment: "对齐", minutes: "纪要", completed: "完成", waiting_model_config: "待配置" };
  const current = Math.max(0, steps.indexOf(job?.currentStep || "uploaded"));
  $("importPipeline").innerHTML = steps.map((step, index) => `<div class="pipeline-step ${index < current ? "done" : index === current ? "active" : ""}"><span>${index + 1}</span><strong>${labels[step] || step}</strong></div>`).join("");
}

function renderArtifactStaleBanner(result = {}, meeting = getCurrentMeeting()) {
  const currentRevision = Number(meeting?.transcriptRevision || 0);
  const stale = result.status === "stale"
    || (Number.isFinite(Number(result.sourceTranscriptRevision)) && Number(result.sourceTranscriptRevision) < currentRevision);
  if (!stale) return "";
  // 片段 ID、内部版本号和来源范围属于系统审计数据，不是用户阅读内容。
  // 过期时仅保留中文业务提示和明确动作，后端仍可继续保存完整溯源信息。
  return `<section class="artifact-provenance" data-artifact-status="stale"><span class="artifact-stale-banner" role="status">会议转写内容已更新，当前结果为历史版本。</span><button type="button" data-detail-tool-regenerate="${escapeHtml(state.activeDetailTool || "minutes")}">重新生成</button></section>`;
}

function scrollToSourceSegment(id, startMs = 0) {
  const segment = document.querySelector(`[data-segment-id="${CSS.escape(String(id))}"]`);
  if (!segment) return;
  // Source navigation never changes route or mutates the transcript. It only focuses the durable
  // segment and advances the local player position so reviewers keep their current workbench context.
  state.playbackActiveSegmentId = String(id);
  state.playbackInteractionStarted = true;
  $$(".speech-segment.is-active").forEach((item) => item.classList.remove("is-active"));
  segment.classList.add("is-active");
  const audioPlayer = $("bottomAudioPlayer");
  const seekMs = Math.max(0, Number(startMs) || 0);
  if (audioPlayer) audioPlayer.dataset.seekMs = String(seekMs);
  seekDetailMediaElement(seekMs);
  const clock = $("audioClock");
  if (clock) clock.textContent = `${formatTime(seekMs)} / source`;
  segment.scrollIntoView({ behavior: "smooth", block: "center" });
}

function seekDetailMediaElement(seekMs) {
  const mediaElement = $("detailMediaElement");
  if (!(mediaElement instanceof HTMLMediaElement)) return false;
  const normalizedSeekMs = Math.max(0, Number(seekMs) || 0);
  // A source link may be clicked while the browser is still loading duration metadata. Persist the
  // requested offset on the media element itself so the loadedmetadata handler can finish the same
  // user action instead of silently leaving playback at zero.
  mediaElement.dataset.pendingSeekMs = String(normalizedSeekMs);
  if (mediaElement.readyState < HTMLMediaElement.HAVE_METADATA || !Number.isFinite(mediaElement.duration)) return false;
  mediaElement.currentTime = Math.min(normalizedSeekMs / 1000, Math.max(0, mediaElement.duration));
  delete mediaElement.dataset.pendingSeekMs;
  return true;
}

function syncPlaybackActiveSegment() {
  const mediaElement = $("detailMediaElement");
  const meeting = getCurrentMeeting();
  if (!(mediaElement instanceof HTMLMediaElement) || !meeting) return;
  const currentMs = Math.max(0, mediaElement.currentTime * 1000);
  // 浏览器为新音频建立元数据时也会产生位于 0 秒的 timeupdate。该事件不是用户播放操作，
  // 因此在 interactionStarted=false 时必须保持无高亮；来源跳转和播放按钮会显式开启联动。
  const active = state.playbackInteractionStarted
    ? (meeting.segments || []).find((segment) => currentMs >= Number(segment.startMs || 0)
      && currentMs < Number(segment.endMs || segment.startMs || 0))
    : null;
  state.playbackActiveSegmentId = active ? String(active.id) : "";
  $$(".speech-segment").forEach((segment) => {
    segment.classList.toggle("is-active", Boolean(active) && segment.dataset.segmentId === String(active.id));
  });
  const clock = $("audioClock");
  if (clock) clock.textContent = `${formatTime(currentMs)} / ${formatTime((mediaElement.duration || 0) * 1000)}`;
}

function hasMeetingRecordedMedia(meeting = getCurrentMeeting()) {
  return Boolean(
    meeting?.audioUrl
    || meeting?.audio_url
    || meeting?.mediaUrl
    || meeting?.media_url
    || meeting?.audio?.url
    || (meeting?.files || []).length,
  );
}

async function loadDetailMediaSource(meeting) {
  const mediaElement = $("detailMediaElement");
  const sourceKey = `${meeting?.id || "none"}:${meeting?.files?.[0]?.id || "no-file"}`;
  if (!(mediaElement instanceof HTMLMediaElement) || !meeting?.id || !(meeting.files || []).length) return;
  if (state.detailMediaSourceKey === sourceKey || state.detailMediaLoadingKey === sourceKey) return;
  state.detailMediaLoadingKey = sourceKey;
  try {
    // The existing download route is POST-only. Convert its response to a browser-owned Blob URL so
    // source references, play/pause and timeupdate all operate on a genuine media transport.
    const response = await fetch(`${API_BASE}/api/meetings/${encodeURIComponent(meeting.id)}/exports/audio`, { method: "POST" });
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    const nextUrl = URL.createObjectURL(await response.blob());
    if (state.detailMediaObjectUrl) URL.revokeObjectURL(state.detailMediaObjectUrl);
    state.detailMediaObjectUrl = nextUrl;
    state.detailMediaSourceKey = sourceKey;
    mediaElement.src = nextUrl;
    mediaElement.load();
    bindDetailMediaEvents();
    syncRealtimeControls(state.detailMode === "import");
  } catch (error) {
    showToast(`会议录音加载失败：${error.message || "未知错误"}`, "warning");
  } finally {
    if (state.detailMediaLoadingKey === sourceKey) state.detailMediaLoadingKey = "";
  }
}

async function toggleDetailMediaPlayback() {
  const mediaElement = $("detailMediaElement");
  if (!(mediaElement instanceof HTMLMediaElement) || !mediaElement.getAttribute("src")) return false;
  // Once a meeting exposes a real recording, the compact transport becomes playback control. A
  // meeting without a recording keeps the existing realtime-recognition fallback in the click
  // handler, preserving the deliberate separation between live capture and recorded review.
  if (mediaElement.paused) await mediaElement.play();
  else mediaElement.pause();
  const button = $("realtimePlayBtn");
  if (button) {
    button.textContent = mediaElement.paused ? "▶" : "⏸";
    button.title = mediaElement.paused ? "播放会议录音" : "暂停会议录音";
    button.setAttribute("aria-label", button.title);
  }
  return true;
}

function bindDetailMediaEvents() {
  const mediaElement = $("detailMediaElement");
  if (!(mediaElement instanceof HTMLMediaElement) || mediaElement.dataset.eventsBound === "true") return;
  mediaElement.dataset.eventsBound = "true";
  mediaElement.addEventListener("loadedmetadata", () => {
    if (mediaElement.dataset.pendingSeekMs !== undefined) seekDetailMediaElement(mediaElement.dataset.pendingSeekMs);
    syncPlaybackActiveSegment();
  });
  mediaElement.addEventListener("timeupdate", syncPlaybackActiveSegment);
  mediaElement.addEventListener("play", () => {
    // 从这一刻开始，timeupdate 才代表真实播放进度，可安全驱动逐字稿高亮。
    state.playbackInteractionStarted = true;
    syncPlaybackActiveSegment();
    const button = $("realtimePlayBtn");
    if (button) button.textContent = "⏸";
  });
  mediaElement.addEventListener("pause", () => {
    const button = $("realtimePlayBtn");
    if (button && !state.realtimeRunning) button.textContent = "▶";
  });
}

function syncDetailMediaElement(meeting) {
  const mediaElement = $("detailMediaElement");
  if (!(mediaElement instanceof HTMLMediaElement)) return;
  // Accept backend naming variations without inventing a source URL. This preserves the honest
  // no-audio state while making an explicitly supplied source available to source-link seeking.
  const sourceUrl = meeting?.audioUrl || meeting?.audio_url || meeting?.mediaUrl || meeting?.media_url || meeting?.audio?.url || "";
  const sourceKey = `${meeting?.id || "none"}:${sourceUrl || meeting?.files?.[0]?.id || "no-file"}`;
  if (mediaElement.dataset.sourceUrl === sourceKey) return;
  // 切换会议/录音时清除上一场的播放定位。否则新媒体的 00:00 事件可能沿用旧会议的活动片段。
  state.playbackActiveSegmentId = "";
  state.playbackInteractionStarted = false;
  if (state.detailMediaObjectUrl && state.detailMediaSourceKey !== sourceKey) {
    URL.revokeObjectURL(state.detailMediaObjectUrl);
    state.detailMediaObjectUrl = "";
    state.detailMediaSourceKey = "";
  }
  mediaElement.dataset.sourceUrl = sourceKey;
  if (sourceUrl) mediaElement.src = sourceUrl;
  else if ((meeting?.files || []).length) void loadDetailMediaSource(meeting);
  else mediaElement.removeAttribute("src");
  mediaElement.load();
  bindDetailMediaEvents();
}

function isPureRealtimeContextEcho(text) {
  // 历史版本曾把识别优化词表当成语音正文写入数据库。这里仅匹配截图中“完整且纯粹”的
  // 三项上下文串：统一全/半角、空白、句末标点后必须全等，因而“会议讨论了智能转写和
  // 声纹注册方案”这类正常句子不会被误删。此函数只服务显示层，绝不修改后端原始片段。
  const normalized = String(text || "")
    .normalize("NFKC")
    .trim()
    .replace(/；/g, ";")
    .replace(/\s+/g, "")
    .replace(/[。.!！?？]+$/g, "");
  return normalized === "智能转写;声纹注册;强制对齐";
}

function transcriptSpeakerIdentityKey(segment = {}) {
  // 身份键按可信度从高到低选择。实时簇必须同时带 session token，因为暂停后新会话会从
  // speaker-1 重新编号；只按簇号合并会把两个真实人物误放进同一个框。
  const voiceprintId = String(segment.voiceprintId || "").trim();
  if (voiceprintId) return `voiceprint:${voiceprintId}`;
  const clusterId = String(segment.speakerClusterId || "").trim();
  if (clusterId) return `cluster:${segment.realtimeSessionToken || "session"}:${clusterId}`;
  const diarizationId = String(
    segment.diarizationSpeakerKey
    || segment.diarizationSpeaker
    || segment.speakerKey
    || segment.speakerId
    || segment.speaker_id
    || "",
  ).trim();
  if (diarizationId) return `diarization:${diarizationId}`;
  const speakerName = String(renderSpeakerIdentity(segment) || "未识别发言人").trim();
  return `name:${segment.realtimeSessionToken || "record"}:${speakerName}`;
}

function groupTranscriptSegments(segments = []) {
  // 只合并“相邻的同一轮发言”。三秒阈值允许 ASR/VAD 分片边界的小空隙，但会在长停顿后
  // 新建发言框。缺失时间戳时无法证明间隔不超过三秒，因此保守地新开框，避免误合并历史脏数据。
  const groups = [];
  for (const segment of segments) {
    const identityKey = transcriptSpeakerIdentityKey(segment);
    const previousGroup = groups[groups.length - 1];
    const previousSegment = previousGroup?.segments?.[previousGroup.segments.length - 1];
    const hasComparableTimes = previousSegment?.endMs !== null
      && previousSegment?.endMs !== undefined
      && segment.startMs !== null
      && segment.startMs !== undefined
      && Number.isFinite(Number(previousSegment.endMs))
      && Number.isFinite(Number(segment.startMs));
    const gapMs = hasComparableTimes ? Number(segment.startMs) - Number(previousSegment.endMs) : 0;
    const mayMerge = previousGroup
      && previousGroup.identityKey === identityKey
      && hasComparableTimes
      && gapMs <= 3000;
    if (mayMerge) {
      previousGroup.segments.push(segment);
      previousGroup.endMs = Math.max(Number(previousGroup.endMs || 0), Number(segment.endMs || segment.startMs || 0));
      continue;
    }
    groups.push({
      blockId: `block-${segment.id}`,
      identityKey,
      speakerName: renderSpeakerIdentity(segment) || "未识别发言人",
      startMs: Number(segment.startMs || 0),
      endMs: Number(segment.endMs || segment.startMs || 0),
      segments: [segment],
    });
  }
  return groups;
}

function resetTranscriptEditing() {
  state.transcriptEditMode = false;
  state.transcriptEditRevision = 0;
  state.transcriptEditOriginals = {};
  state.transcriptEditDrafts = {};
  state.transcriptUndoStack = [];
  state.transcriptRedoStack = [];
  state.transcriptHistoryLock = false;
  state.transcriptSaveStatus = "readonly";
  state.selectedTranscriptSegmentIds = new Set();
}

function transcriptEditIsDirty() {
  return Object.keys(state.transcriptEditDrafts).some(
    (segmentId) => String(state.transcriptEditDrafts[segmentId] ?? "")
      !== String(state.transcriptEditOriginals[segmentId] ?? ""),
  );
}

function syncTranscriptEditToolbar() {
  const editing = state.transcriptEditMode;
  const dirty = transcriptEditIsDirty();
  const editButton = $("editResultBtn");
  const saveButton = $("saveTranscriptBtn");
  const cancelButton = $("cancelTranscriptEditBtn");
  const undoButton = $("undoTranscriptBtn");
  const redoButton = $("redoTranscriptBtn");
  if (editButton) {
    editButton.hidden = editing;
    editButton.disabled = !meetingHasTranscriptText();
  }
  if (saveButton) {
    saveButton.hidden = !editing;
    saveButton.disabled = !dirty || state.transcriptSaveStatus === "saving";
  }
  if (cancelButton) cancelButton.hidden = !editing;
  if (undoButton) {
    undoButton.disabled = !editing || state.transcriptUndoStack.length === 0 || state.transcriptSaveStatus === "saving";
  }
  if (redoButton) {
    redoButton.disabled = !editing || state.transcriptRedoStack.length === 0 || state.transcriptSaveStatus === "saving";
  }
  const status = $("transcriptSaveStatus");
  if (status) {
    const labels = {
      readonly: "只读",
      clean: "编辑原始逐字稿",
      dirty: "有未保存修改",
      saving: "正在保存…",
      saved: "已保存",
      conflict: "内容已被其他操作更新，请刷新后重试",
    };
    const statusKey = editing && state.transcriptSaveStatus === "clean" && dirty ? "dirty" : state.transcriptSaveStatus;
    status.textContent = labels[statusKey] || labels.readonly;
    status.dataset.status = statusKey;
  }
  applyTranscriptViewStyle();
}

function applyTranscriptViewStyle() {
  // 统一把样式写到编辑器 CSS 变量，普通段落和编辑 textarea 会同步继承。
  const editor = $("transcriptEditor");
  if (!editor) return;
  const style = state.transcriptViewStyle;
  const fontFamilies = {
    "source-han": '"Source Han Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif',
    system: 'system-ui, -apple-system, "Segoe UI", sans-serif',
    serif: 'SimSun, "Songti SC", serif',
  };
  const colors = ["#292524", "#175cd3", "#b42318"];
  const alignments = ["left", "justify", "center"];
  editor.style.setProperty("--transcript-font-family", fontFamilies[style.fontFamily] || fontFamilies["source-han"]);
  editor.style.setProperty("--transcript-font-size", `${Number(style.fontSize) || 18}px`);
  editor.style.setProperty("--transcript-font-weight", style.bold ? "700" : "400");
  editor.style.setProperty("--transcript-font-style", style.italic ? "italic" : "normal");
  editor.style.setProperty("--transcript-decoration", `${style.underline ? "underline" : ""} ${style.strike ? "line-through" : ""}`.trim() || "none");
  editor.style.setProperty("--transcript-color", colors[style.colorIndex % colors.length]);
  editor.style.setProperty("--transcript-align", alignments[style.alignIndex % alignments.length]);
  $("transcriptFontFamily") && ($("transcriptFontFamily").value = style.fontFamily);
  $("transcriptFontSize") && ($("transcriptFontSize").value = String(style.fontSize));
  document.querySelectorAll("[data-transcript-style]").forEach((button) => {
    const key = button.dataset.transcriptStyle;
    button.classList.toggle("active", ["bold", "italic", "underline", "strike"].includes(key) && Boolean(style[key]));
    if (key === "color") button.style.setProperty("--toolbar-letter-color", colors[style.colorIndex % colors.length]);
    if (key === "align") button.dataset.align = alignments[style.alignIndex % alignments.length];
  });
}

function toggleTranscriptViewStyle(styleName) {
  // textarea 无法安全保存“局部富文本”且会破坏 segment 来源；因此按钮明确作用于整个工作台显示。
  if (["bold", "italic", "underline", "strike"].includes(styleName)) {
    state.transcriptViewStyle[styleName] = !state.transcriptViewStyle[styleName];
  } else if (styleName === "color") {
    state.transcriptViewStyle.colorIndex = (state.transcriptViewStyle.colorIndex + 1) % 3;
  } else if (styleName === "align") {
    state.transcriptViewStyle.alignIndex = (state.transcriptViewStyle.alignIndex + 1) % 3;
  }
  applyTranscriptViewStyle();
}

function startTranscriptEditing() {
  const meeting = getCurrentMeeting();
  if (!meeting || !(meeting.segments || []).length) return showToast("当前没有可编辑的逐字稿", "warning");
  state.transcriptEditMode = true;
  state.transcriptEditRevision = Number(meeting.transcriptRevision || 0);
  state.transcriptEditOriginals = Object.fromEntries(
    (meeting.segments || []).map((segment) => [String(segment.id), String(segment.text || "")]),
  );
  state.transcriptEditDrafts = { ...state.transcriptEditOriginals };
  state.transcriptUndoStack = [];
  state.transcriptRedoStack = [];
  state.transcriptSaveStatus = "clean";
  state.selectedTranscriptSegmentIds = new Set();
  renderMeetingDetailWorkspace();
  showToast("已进入原始逐字稿编辑模式，禁忌词会在保存后重新应用到展示层", "info");
}

function cancelTranscriptEditing() {
  const hadChanges = transcriptEditIsDirty();
  resetTranscriptEditing();
  renderMeetingDetailWorkspace();
  if (hadChanges) showToast("已放弃本次未保存修改", "info");
}

function pushTranscriptUndoSnapshot() {
  if (!state.transcriptEditMode || state.transcriptHistoryLock) return;
  state.transcriptUndoStack.push({ ...state.transcriptEditDrafts });
  if (state.transcriptUndoStack.length > 80) state.transcriptUndoStack.shift();
  state.transcriptRedoStack = [];
}

function restoreTranscriptDraftSnapshot(snapshot) {
  state.transcriptHistoryLock = true;
  state.transcriptEditDrafts = { ...snapshot };
  state.transcriptSaveStatus = transcriptEditIsDirty() ? "dirty" : "clean";
  renderMeetingDetailWorkspace();
  state.transcriptHistoryLock = false;
}

function undoTranscriptEdit() {
  const snapshot = state.transcriptUndoStack.pop();
  if (!snapshot) return;
  state.transcriptRedoStack.push({ ...state.transcriptEditDrafts });
  restoreTranscriptDraftSnapshot(snapshot);
}

function redoTranscriptEdit() {
  const snapshot = state.transcriptRedoStack.pop();
  if (!snapshot) return;
  state.transcriptUndoStack.push({ ...state.transcriptEditDrafts });
  restoreTranscriptDraftSnapshot(snapshot);
}

async function saveTranscriptEdits() {
  const meeting = getCurrentMeeting();
  if (!meeting || !state.transcriptEditMode) return;
  const updates = Object.entries(state.transcriptEditDrafts)
    .filter(([segmentId, text]) => String(text) !== String(state.transcriptEditOriginals[segmentId] ?? ""))
    .map(([segmentId, text]) => ({ segmentId, text }));
  if (!updates.length) return cancelTranscriptEditing();

  state.transcriptSaveStatus = "saving";
  syncTranscriptEditToolbar();
  try {
    await apiRequest(`/api/meetings/${meeting.id}/segments/batch`, {
      method: "PATCH",
      body: {
        expectedTranscriptRevision: state.transcriptEditRevision,
        updates,
      },
    });
    delete state.displayTranscriptViews[meeting.id];
    resetTranscriptEditing();
    await loadInitialData();
    await loadDisplayTranscriptView(meeting.id);
    showToast("逐字稿已保存，旧的 AI 结果已按版本规则标记", "success");
  } catch (error) {
    state.transcriptSaveStatus = error.status === 409 ? "conflict" : "dirty";
    syncTranscriptEditToolbar();
    throw error;
  }
}

function renderMeetingDetailWorkspace() {
  const meeting = getCurrentMeeting();
  if (!meeting) {
    // The detail page is shared by realtime and import flows. When the selected record disappears after
    // filtering/deleting/refreshing, rendering nothing would leave stale transcript and AI cards on screen.
    // Clear every visible panel so the user sees an honest empty state instead of another record's content.
    $("detailTitle").textContent = state.detailMode === "import" ? "转写详情" : "实时会议";
    $("detailStatusStrip").textContent = "当前记录不存在，请返回列表重新选择。";
    $("speakerList").innerHTML = "";
    $("transcriptEditor").innerHTML = `<div class="empty-state">当前记录不存在，请返回列表重新选择。</div>`;
    $("detailToolPanel").innerHTML = `<article class="tool-result-card detail-tool-card detail-tool-empty"><p>当前记录不存在，AI 工具已清空。</p></article>`;
    syncRealtimeControls(true);
    return;
  }
  resetDetailToolPanelForContext(meeting);
  const imported = state.detailMode === "import" || isImportedMeeting(meeting);
  $("detailTitle").textContent = imported ? meeting.fileName || "转写详情" : meeting.meetingName || "实时会议";
  $("detailStatusStrip").textContent = `ASR：Qwen3-ASR-1.7B｜声纹：${meeting.enableDiarization ? "开启" : "关闭"}｜纪要：${minutesStatusText(meeting.minutesStatus)}｜对接：${meeting.integrationStatus?.todoPush || "待推送"}`;
  syncDetailMediaElement(meeting);
  // 导入转写与实时会议共用详情 DOM，但历史上下文回声只来自旧实时链路。
  // 构造新的显示数组可同时清理正文、说话人计数和搜索选项，又不会污染 meeting.segments。
  const displayableSegments = (meeting.segments || []).filter(
    (segment) => !(!imported && isPureRealtimeContextEcho(segment.text)),
  );
  const displayMeeting = { ...meeting, segments: displayableSegments };
  renderSpeakerPanel(displayMeeting);
  const displayView = state.displayTranscriptViews[meeting.id];
  const displayViewCoversSegments = displayView
    && displayableSegments.every((segment) => (displayView.segments || []).some((viewSegment) => viewSegment.id === segment.id));
  if (!displayViewCoversSegments && !state.displayTranscriptViewLoading[meeting.id]) {
    loadDisplayTranscriptView(meeting.id);
  }
  const displaySegmentsById = new Map((displayView?.segments || []).map((segment) => [segment.id, segment]));
  const speakerNames = [...new Set(displayableSegments.map((segment) => renderSpeakerIdentity(segment) || "未分配"))];
  const workbenchBar = $("transcriptWorkbenchBar");
  const speakerFilter = $("transcriptSpeakerFilter");
  const hasVisibleTranscript = displayableSegments.some((segment) => String(displaySegmentsById.get(segment.id)?.displayText || segment.text || "").trim());
  if (workbenchBar) workbenchBar.hidden = !hasVisibleTranscript;
  if (speakerFilter) {
    speakerFilter.innerHTML = `<option value="">全部发言人</option>${speakerNames.map((name) => `<option value="${escapeHtml(name)}" ${state.transcriptSpeakerFilter === name ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}`;
    speakerFilter.hidden = speakerNames.length <= 1;
    // 当说话人从多人回落为单人时，清空已失效的筛选值，避免正文被一个不可见控件继续过滤。
    if (speakerFilter.hidden && state.transcriptSpeakerFilter) state.transcriptSpeakerFilter = "";
  }
  const query = state.transcriptQuery.trim().toLowerCase();
  const transcriptBlocks = groupTranscriptSegments(displayableSegments).filter((block) => {
    const speakerMatches = !state.transcriptSpeakerFilter || state.transcriptSpeakerFilter === block.speakerName;
    const textMatches = !query || block.segments.some((segment) => {
      const visibleText = state.transcriptEditMode
        ? state.transcriptEditDrafts[String(segment.id)] ?? segment.text ?? ""
        : displaySegmentsById.get(segment.id)?.displayText || "";
      return `${block.speakerName} ${visibleText}`.toLowerCase().includes(query);
    });
    return speakerMatches && textMatches;
  });
  const segmentHtml = transcriptBlocks.map((block) => {
    const blockActive = block.segments.some((segment) => state.playbackActiveSegmentId === String(segment.id));
    const editableParagraphHtml = block.segments.map((segment) => {
      const segmentId = String(segment.id);
      const selected = state.selectedTranscriptSegmentIds.has(segmentId);
      const displayText = displaySegmentsById.get(segment.id)?.displayText || "";
      const text = state.transcriptEditDrafts[segmentId] ?? segment.text ?? "";
      return `
        <div class="speech-segment speech-block-paragraph ${state.playbackActiveSegmentId === segmentId ? "is-active" : ""} ${selected ? "is-selected" : ""}" data-segment-id="${escapeHtml(segmentId)}">
          <div class="speech-paragraph-meta">
            <label class="segment-select-label"><input type="checkbox" data-select-transcript-segment="${escapeHtml(segmentId)}" ${selected ? "checked" : ""} />选择</label>
            <button type="button" class="segment-time-button" data-seek-segment="${escapeHtml(segmentId)}" data-seek-ms="${Number(segment.startMs || 0)}">${formatTime(segment.startMs || 0)}</button>
          </div>
          <textarea class="transcript-source-editor" data-transcript-edit-segment="${escapeHtml(segmentId)}">${escapeHtml(text)}</textarea>
        </div>
      `;
    }).join("");
    // 只读态对用户呈现一个真正连续的正文片段，而不是“同一外框里的多行片段”。每个底层
    // segment 仍由行内 span 保存稳定 ID，AI 来源跳转和播放高亮可以定位原时间戳；进入编辑态
    // 才恢复逐段 textarea，确保保存、撤销、敏感词和 revision 契约完全不变。
    const mergedDisplayHtml = `
      <div class="speech-block-merged-body">
        <p class="transcript-display-text transcript-merged-text">${block.segments.map((segment, index) => {
          const segmentId = String(segment.id);
          const displayText = displaySegmentsById.get(segment.id)?.displayText || "";
          const separator = index < block.segments.length - 1 && displayText ? " " : "";
          return `<span class="speech-segment merged-segment-fragment ${state.playbackActiveSegmentId === segmentId ? "is-active" : ""}" data-segment-id="${escapeHtml(segmentId)}" data-segment-display-text="${escapeHtml(segmentId)}">${escapeHtml(displayText)}${separator}</span>`;
        }).join("")}</p>
      </div>`;
    return `
      <article class="speech-block ${blockActive ? "is-active" : ""}" data-transcript-block="${escapeHtml(block.blockId)}">
        <header class="speech-block-header">
          <strong>${escapeHtml(block.speakerName)}</strong>
          <span>${formatTime(block.startMs)}${block.endMs > block.startMs ? ` – ${formatTime(block.endMs)}` : ""}</span>
        </header>
        <div class="speech-block-body">${state.transcriptEditMode ? editableParagraphHtml : mergedDisplayHtml}</div>
      </article>
    `;
  }).join("");
  const draftHtml = renderRealtimeDraftSegment();
  $("transcriptEditor").innerHTML = segmentHtml || draftHtml ? `${segmentHtml}${draftHtml}` : (displayableSegments.length ? `<div class="empty-state">没有符合当前筛选条件的转写片段。</div>` : renderRealtimeEmptyState(meeting, imported));
  syncTranscriptEditToolbar();
  renderAudioTimeline(displayMeeting);
  const audioPlayer = $("bottomAudioPlayer");
  if (audioPlayer) {
    // Import detail never exposes live recognition. When its uploaded recording exists, the same
    // compact bar is a playback-only transport; otherwise it stays hidden rather than offering a
    // misleading realtime fallback.
    const recordedMedia = hasMeetingRecordedMedia(meeting);
    audioPlayer.hidden = imported && !recordedMedia;
    audioPlayer.setAttribute("aria-hidden", audioPlayer.hidden ? "true" : "false");
  }
  const playButton = $("realtimePlayBtn");
  if (playButton) {
    const recordedMedia = hasMeetingRecordedMedia(meeting);
    playButton.hidden = imported && !recordedMedia;
    playButton.disabled = imported && !recordedMedia;
    playButton.textContent = compactRealtimeIcon(imported);
    playButton.title = state.realtimeRunning ? "暂停实时识别" : "开始会议识别";
  }
  syncRealtimeControls(imported);
  const endButton = $("endMeetingBtn");
  if (endButton) {
    // 结束会议只对实时/快速会议有意义；导入音视频没有实时流，隐藏可避免误操作。
    endButton.hidden = imported;
    endButton.disabled = imported || !state.currentMeetingId;
  }
  const backButton = $("backToImportLedgerBtn");
  if (backButton) backButton.textContent = imported ? "返回台账" : "返回会议列表";
  syncDetailWorkbenchState();
}

function syncDetailWorkbenchState() {
  const workbench = document.querySelector(".detail-workbench");
  if (!workbench) return;
  const speakerPanel = $("speakerPanel");
  const toolDock = document.querySelector(".right-tool-dock");
  const speakerCollapsed = Boolean(state.detailPanelCollapsed.speaker);
  const toolsCollapsed = Boolean(state.detailPanelCollapsed.tools);

  // 详情页 DOM 是在线会议和导入转写共用的，这里只同步 class 和 aria，
  // 不直接删除任何内容，保证收起后再次展开时编辑内容、工具结果和滚动状态不会丢失。
  workbench.classList.toggle("speaker-collapsed", speakerCollapsed);
  workbench.classList.toggle("tools-collapsed", toolsCollapsed);
  speakerPanel?.classList.toggle("is-collapsed", speakerCollapsed);
  toolDock?.classList.toggle("is-collapsed", toolsCollapsed);

  const speakerToggle = document.querySelector('[data-detail-collapse="speaker"]');
  if (speakerToggle) {
    speakerToggle.textContent = speakerCollapsed ? "›" : "‹";
    speakerToggle.title = speakerCollapsed ? "展开发言人列表" : "收起发言人列表";
    speakerToggle.setAttribute("aria-label", speakerToggle.title);
    speakerToggle.setAttribute("aria-expanded", String(!speakerCollapsed));
  }

  const toolsToggle = document.querySelector('[data-detail-collapse="tools"]');
  if (toolsToggle) {
    toolsToggle.textContent = toolsCollapsed ? "‹" : "›";
    toolsToggle.title = toolsCollapsed ? "展开 AI 工具栏" : "收起 AI 工具栏";
    toolsToggle.setAttribute("aria-label", toolsToggle.title);
    toolsToggle.setAttribute("aria-expanded", String(!toolsCollapsed));
  }

  setDetailToolRunning(state.runningDetailTool, Boolean(state.runningDetailTool));
}

function toggleDetailPanel(panel) {
  if (!["speaker", "tools"].includes(panel)) return;
  // 只切换当前面板，不重置另一个面板；用户可能会同时收起左右两侧来获得最大的转写编辑宽度。
  state.detailPanelCollapsed[panel] = !state.detailPanelCollapsed[panel];
  syncDetailWorkbenchState();
}

function setDetailToolRunning(tool, isRunning) {
  const normalizedTool = isRunning ? tool : "";
  $$(".right-tool-dock button[data-detail-tool]").forEach((button) => {
    const buttonTool = button.dataset.detailTool;
    const active = Boolean(normalizedTool && buttonTool === normalizedTool);
    // 运行态只标记正在调用的那个按钮；其余按钮保留可见，方便用户理解“当前是哪个工具在跑”。
    button.classList.toggle("is-running", active);
    button.classList.toggle("active", active || (!normalizedTool && state.activeDetailTool === buttonTool));
    button.setAttribute("aria-busy", active ? "true" : "false");
  });
}

function nextDetailToolRunToken() {
  // 运行 token 只用于前端视图一致性，不影响后端任务执行。旧请求完成后仍可缓存结果，
  // 但只有 token 与当前激活工具一致时，才允许继续写入右侧面板。
  state.detailToolRunToken += 1;
  return state.detailToolRunToken;
}

function isCurrentDetailToolRun(tool, runToken) {
  return state.detailToolRunToken === runToken && state.activeDetailTool === tool;
}

function meetingHasTranscriptText(meeting = getCurrentMeeting()) {
  return Boolean((meeting?.segments || []).some((segment) => String(segment.text || "").trim()));
}

function shouldShowRealtimeLowVolumeEmpty(meeting = getCurrentMeeting()) {
  // A low-volume frame is only a local moment in the microphone stream; it must not describe the whole
  // meeting while the backend is still transcribing earlier chunks. The user can speak loudly, then pause,
  // and the next tiny silent frame would otherwise repaint the editor as "low volume" before the transcript
  // message or polling result arrives. Gate the large empty-state copy behind "no transcript" and
  // "no in-flight ASR chunk"; the bottom player still shows the lightweight status for diagnostics.
  return (
    state.realtimeStatus === "low_volume" &&
    state.realtimeInFlightChunks <= 0 &&
    !meetingHasTranscriptText(meeting)
  );
}

function setRealtimeStatus(status, detail = {}, options = {}) {
  // 这个状态机只负责“实时会议”链路，不参与导入转写。它把连接状态、麦克风质量和 ASR 结果拆开，
  // 这样低音量不会被误显示成暂停，用户也能知道当前到底是在采集、等待有效语音还是服务异常。
  const nextStatus = status || "idle";
  const changed = state.realtimeStatus !== nextStatus;
  state.realtimeStatus = nextStatus;
  state.realtimeStatusDetail = { ...(detail || {}) };
  if (options.render && changed) renderMeetingDetailWorkspace();
  else updateRealtimeStatusInline();
}

function realtimeStatusText() {
  const status = state.realtimeStatus;
  if (status === "transcribing") return "正在识别";
  if (status === "low_volume") return "音量偏低";
  if (status === "asr_empty") return "等待有效语音";
  if (status === "error") return "识别异常";
  if (status === "paused") return "已暂停";
  if (state.realtimeRunning || status === "listening") return "正在采集";
  return "等待音频";
}

function renderRealtimeEmptyState(meeting, imported = false) {
  if (imported) {
    return `<div class="empty-state">导入文件处理完成后，转写文本会在这里展示。</div>`;
  }
  if (!state.realtimeRunning && state.realtimeStatus !== "paused") {
    return `<div class="empty-state">请点击会议开始按钮后开始实时转写。</div>`;
  }
  if (shouldShowRealtimeLowVolumeEmpty(meeting)) {
    return `<div class="empty-state">当前麦克风音量偏低，请靠近麦克风或检查输入设备。</div>`;
  }
  if (state.realtimeStatus === "asr_empty") {
    return `<div class="empty-state">已采集到语音，但当前分片未识别到有效文本，请继续发言。</div>`;
  }
  if (state.realtimeStatus === "error") {
    return `<div class="empty-state">实时识别服务异常，请稍后重新开始会议。</div>`;
  }
  if (state.realtimeStatus === "paused") {
    return `<div class="empty-state">实时识别已暂停，点击播放按钮可继续采集。</div>`;
  }
  return `<div class="empty-state">正在采集麦克风，等待可识别语音。</div>`;
}

function renderRealtimeDraftSegment() {
  // Provider-native realtime ASR emits partial text before it decides an utterance is final. Showing this draft
  // is the visible difference between a market-style realtime product and the old "wait for a WAV chunk" flow.
  // The draft is deliberately read-only and never persisted; the final transcript event replaces it with a
  // normal editable segment after the provider endpointing/VAD marks the utterance complete.
  const text = String(state.realtimeDraftText || "").trim();
  if (!text || state.detailMode !== "realtime") return "";
  return `
    <article id="realtimeDraftSegment" class="speech-segment speech-segment-draft" aria-live="polite">
      <header><strong>实时预识别</strong><span>正在输入</span><button disabled>预览</button></header>
      <textarea id="realtimeDraftTextArea" readonly></textarea>
    </article>
  `;
}

function updateRealtimeDraftInline() {
  // Provider partial events may arrive several times per second. Re-running renderMeetingDetailWorkspace here
  // recreates every editable transcript textarea, speaker row and player control; on long meetings that both
  // delays visible text and can discard a user's cursor/selection. Only the ephemeral draft node changes.
  const editor = $("transcriptEditor");
  if (!editor || state.detailMode !== "realtime") return;
  // Interim provider hypotheses have not reached the persisted, frozen display-view boundary.
  // Show only a neutral placeholder until a final segment is safely available from that endpoint.
  const visibleText = String(state.realtimeDraftText || "").trim() ? "正在准备安全展示..." : "";
  let draft = $("realtimeDraftSegment");
  if (!visibleText) {
    draft?.remove();
    return;
  }

  if (!draft) {
    // The first partial replaces the listening empty state; subsequent partials reuse this exact node. Existing
    // finalized segments stay untouched, which is the append/replace behavior users expect from live captions.
    editor.querySelector(".empty-state")?.remove();
    editor.insertAdjacentHTML("beforeend", renderRealtimeDraftSegment());
    draft = $("realtimeDraftSegment");
  }
  const textArea = $("realtimeDraftTextArea");
  if (textArea && textArea.value !== visibleText) textArea.value = visibleText;

  // Follow new speech only when the user was already near the bottom. Someone reviewing an earlier paragraph
  // should not be pulled away on every hypothesis update.
  const distanceFromBottom = editor.scrollHeight - editor.scrollTop - editor.clientHeight;
  if (draft && distanceFromBottom < 120) draft.scrollIntoView({ block: "nearest" });
}

function updateRealtimeStatusInline() {
  const audioClock = $("audioClock");
  if (!audioClock) return;
  audioClock.textContent = `${audioClock.dataset.durationLabel || "等待音频"} · ${realtimeStatusText()}`;
}

function syncRealtimeCaptureMode() {
  // This data attribute is intentionally non-visual. Browser smoke tests and support diagnostics can verify
  // whether AudioWorklet is active without exposing implementation labels in the meeting UI.
  const player = $("bottomAudioPlayer");
  if (player) player.dataset.captureMode = state.realtimeCaptureMode || "inactive";
}

function renderNoTranscriptToolPrompt(tool) {
  const message = "请先开始会议识别或导入音视频文件，生成转写内容后再使用 AI 工具。";
  return `
    <article class="tool-result-card detail-tool-card detail-tool-empty">
      <header class="detail-tool-result-head">
        <div>
          <h3>${escapeHtml(detailToolTitle(tool))}</h3>
          <p>当前会议还没有可分析的转写文本</p>
        </div>
      </header>
      <div class="detail-tool-stream">${escapeHtml(message)}</div>
    </article>
  `;
}

function realtimeActionLabel(imported = false) {
  if (imported) return "离线音频";
  return state.realtimeRunning ? "暂停实时识别" : "开始实时识别";
}

function compactRealtimeIcon(imported = false) {
  // Keep the bottom transport as a fixed-width player control. The business action text lives on the
  // top "开始会议" button; this button only carries the traditional play/pause symbols.
  if (imported) return "▶";
  return state.realtimeRunning ? "⏸" : "▶";
}

function syncRealtimeControls(imported = false) {
  const startButton = $("startRealtimeMeetingBtn");
  const playButton = $("realtimePlayBtn");
  const meeting = getCurrentMeeting();
  const recordedMedia = hasMeetingRecordedMedia(meeting);
  const mediaElement = $("detailMediaElement");
  if (startButton) {
    // 顶部按钮才是用户在实时会议详情里显式“开始会议/开始实时转写”的入口；
    // 导入转写详情是离线文件流程，必须隐藏它，避免两个独立功能在同一详情页里混淆。
    startButton.hidden = imported;
    startButton.disabled = imported || state.realtimeRunning || state.realtimeConnecting || !state.currentMeetingId;
    startButton.textContent = state.realtimeConnecting ? "连接中" : (state.realtimeRunning ? "识别中" : "开始会议");
    startButton.title = state.realtimeConnecting ? "正在连接实时转写服务" : (state.realtimeRunning ? "实时转写正在进行" : "开始会议并启动实时转写");
  }
  if (playButton) {
    // Imported records use this control only for recorded playback. Realtime records without a
    // recording keep the live-recognition toggle, so the two product lanes share visuals but not actions.
    playButton.hidden = imported && !recordedMedia;
    playButton.disabled = !state.currentMeetingId || (imported && !recordedMedia);
    const playbackMode = recordedMedia && mediaElement instanceof HTMLMediaElement && Boolean(mediaElement.getAttribute("src"));
    playButton.textContent = playbackMode ? (mediaElement.paused ? "▶" : "⏸") : compactRealtimeIcon(imported);
    playButton.classList.toggle("is-running", state.realtimeRunning);
    const label = playbackMode
      ? (mediaElement.paused ? "播放会议录音" : "暂停会议录音")
      : (imported ? "录音正在加载" : (state.realtimeRunning ? "暂停实时识别" : "开始实时识别"));
    playButton.setAttribute("aria-label", label);
    playButton.title = label;
  }
}

function detailToolProgressStages(tool, result = {}) {
  // 后端会在 ui.progressStages 中返回最贴近实际工作流的阶段；前端保留本地兜底，
  // 确保接口异常或旧数据回放时仍能显示“正在生成”的明确进度。
  return result.ui?.progressStages?.length ? result.ui.progressStages : DETAIL_TOOL_PROGRESS[tool] || ["准备上下文", "调用 AI", "整理结果", "完成生成"];
}

function detailToolTitle(tool, result = {}) {
  return result.ui?.title || DETAIL_TOOL_TITLES[tool] || "AI 结果";
}

function sanitizeAiDisplayText(value) {
  // 防御旧 AI 结果中已经混入的实时片段主键和英文版本标记；只清理结构化内部标识，
  // 不改写普通中文正文。真正的来源字段仍保留在接口对象中供后端审计。
  return String(value || "")
    .replace(/\brt-rec-[a-z0-9-]+\b/gi, "")
    .replace(/\bRevision\s+\d+\b/gi, "")
    .replace(/[ \t]+\n/g, "\n")
    .trim();
}

function detailToolEditableText(tool, result = {}) {
  if (result.ui?.editableText) return sanitizeAiDisplayText(result.ui.editableText);
  let text = "";
  if (tool === "summary") {
    const lines = [result.overview || result.text || ""];
    const keyPoints = result.keyPoints || result.highlights || [];
    if (keyPoints.length) lines.push("", "关键要点：", ...keyPoints.map((item, index) => `${index + 1}. ${typeof item === "string" ? item : item.title || item.content || ""}`));
    text = lines.join("\n");
  } else if (tool === "minutes") {
    text = result.content || result.text || "";
  } else if (tool === "todos") {
    const items = result.items || result.todos || [];
    text = items.map((item, index) => `${index + 1}. ${item.title || item.taskName || item.content || "待办"}`).join("\n");
  } else {
    text = result.text || result.content || result.message || "";
  }
  return sanitizeAiDisplayText(text);
}

function detailToolDraftKey(tool, meetingId = state.currentMeetingId) {
  return `${meetingId || "none"}:${tool}`;
}

function cacheDetailToolDraft(tool, result = {}, content = detailToolEditableText(tool, result), savedAt = "") {
  const meeting = getCurrentMeeting() || {};
  const draft = {
    tool,
    title: detailToolTitle(tool, result),
    content,
    savedAt,
    ui: result.ui || { title: detailToolTitle(tool, result), editableText: content },
  };
  state.detailToolDrafts[detailToolDraftKey(tool, meeting.id || state.currentMeetingId)] = draft;
  return draft;
}

function savedDetailToolDraft(tool) {
  const meeting = getCurrentMeeting() || {};
  const key = detailToolDraftKey(tool, meeting.id || state.currentMeetingId);
  return state.detailToolDrafts[key] || meeting.aiToolDrafts?.[tool] || null;
}

function detailToolDraftKey(tool, meetingId = state.currentMeetingId, contextKey = detailContextKey(), minutesVersionId = "") {
  // AI draft content depends on three things: the business lane (realtime/import), the selected meeting,
  // and the current transcript version. Minutes additionally depend on the selected immutable
  // version: without that ID, reopening a historical edit could reuse a draft from another version.
  const selectedMinutesVersionId = minutesVersionId || state.minutesVersionIds[meetingId] || "none";
  return `${contextKey}:${meetingId || "none"}:${tool}${tool === "minutes" ? `:${selectedMinutesVersionId}` : ""}`;
}

function cacheDetailToolDraft(tool, result = {}, content = detailToolEditableText(tool, result), savedAt = "") {
  const meeting = getCurrentMeeting() || {};
  const contextKey = detailContextKey(meeting);
  const draft = {
    tool,
    title: detailToolTitle(tool, result),
    content,
    savedAt,
    contextKey,
    ui: result.ui || { title: detailToolTitle(tool, result), editableText: content },
  };
  state.detailToolDrafts[detailToolDraftKey(
    tool,
    meeting.id || state.currentMeetingId,
    contextKey,
    tool === "minutes" ? result.versionId || state.minutesVersionIds[meeting.id || state.currentMeetingId] : "",
  )] = draft;
  return draft;
}

function savedDetailToolDraft(tool) {
  const meeting = getCurrentMeeting() || {};
  const contextKey = detailContextKey(meeting);
  const key = detailToolDraftKey(tool, meeting.id || state.currentMeetingId, contextKey);
  const localDraft = state.detailToolDrafts[key];
  if (localDraft?.contextKey === contextKey) return localDraft;
  // Backend saved drafts are useful after a refresh, but only when this meeting actually has transcript text.
  // For an empty realtime meeting, showing an old imported draft is worse than showing nothing, so we block it.
  return meetingHasTranscriptText(meeting) ? meeting.aiToolDrafts?.[tool] || null : null;
}

function showSavedDetailToolResult(tool) {
  const draft = savedDetailToolDraft(tool);
  const panel = $("detailToolPanel");
  if (!draft || !panel) return false;
  state.activeDetailTool = tool;
  // 保存过的草稿转回和生成接口一致的 ui 结构，复用完成态渲染，避免保存草稿和新生成结果长得不一样。
  panel.innerHTML = renderDetailToolCompleted(
    tool,
    { ui: { title: draft.title || DETAIL_TOOL_TITLES[tool], editableText: draft.content || "" } },
    draft.content || "",
    draft.savedAt || "",
  );
  setDetailToolRunning("", false);
  return true;
}

function openDetailTool(tool) {
  state.activeDetailTool = tool;
  nextDetailToolRunToken();
  if (tool === "minutes") {
    // Reopening minutes should first expose durable history instead of silently generating a new
    // version or short-circuiting through the generic AI draft cache. Generation remains available
    // through the existing regenerate control or template selector, making creation explicit.
    loadMinutesVersions().then((version) => {
      const panel = $("detailToolPanel");
      if (version && panel) {
        panel.innerHTML = renderDetailToolResult("minutes", version);
        return;
      }
      runDetailTool("minutes").catch((error) => showToast(error.message || "Minutes generation failed", "error"));
    }).catch((error) => showToast(error.message || "Minutes history failed", "error"));
    return;
  }
  if (showSavedDetailToolResult(tool)) return;
  runDetailTool(tool).catch((error) => showToast(error.message || "工具执行失败", "error"));
}

function renderDetailToolGenerating(tool, stageIndex = 0, streamedText = "") {
  const stages = detailToolProgressStages(tool);
  const safeIndex = Math.min(stageIndex, stages.length - 1);
  // 生成中面板参考用户给的效果：顶部保留当前工具标题和转圈反馈，正文区域持续露出内容。
  // 按钮此时不显示“重新生成”，避免用户误以为可以并发启动第二次长链路生成。
  return `
    <article class="tool-result-card detail-tool-card detail-tool-card-running">
      <header class="detail-tool-result-head">
        <div>
          <h3>${escapeHtml(detailToolTitle(tool))}</h3>
          <p>正在执行会议 AI 工具，内容由 AI 生成，仅供参考</p>
        </div>
        <div class="detail-panel-action-row">
          <span class="tool-running-spinner" aria-hidden="true"></span>
          <button class="secondary-button detail-tool-working" type="button" disabled>正在生成</button>
        </div>
      </header>
      <ol class="detail-tool-progress">
        ${stages.map((stage, index) => `<li class="${index < safeIndex ? "done" : index === safeIndex ? "active" : ""}"><span>${index + 1}</span>${escapeHtml(stage)}</li>`).join("")}
      </ol>
      <div class="detail-tool-stream" aria-live="polite">${escapeHtml(streamedText || "正在准备生成内容...")}<span class="detail-stream-cursor" aria-hidden="true"></span></div>
    </article>
  `;
}

function renderDetailToolCompleted(tool, result = {}, text = detailToolEditableText(tool, result), savedAt = "") {
  // Streaming only changes how the editable body appears; it must not create a second, weaker
  // artifact view. Reuse the canonical provenance renderer after the typing animation completes so
  // current/stale status and source ranges remain available in both immediate and historical cards.
  return `
    <article class="tool-result-card detail-tool-card">
      <header class="detail-tool-result-head">
        <div>
          <h3>${escapeHtml(detailToolTitle(tool, result))}</h3>
          <p>${savedAt ? `已保存：${escapeHtml(savedAt)}｜` : ""}内容由 AI 生成，仅供参考</p>
        </div>
        <div class="detail-panel-action-row">
          <button type="button" data-detail-tool-copy="${tool}">复制</button>
          <button type="button" data-detail-tool-save="${tool}">保存</button>
          <button type="button" data-detail-tool-apply-minutes="${tool}">添加至纪要</button>
          <button type="button" class="primary-button" data-detail-tool-regenerate="${tool}">重新生成</button>
        </div>
      </header>
      ${tool === "minutes" ? renderMinutesVersionControls(result) : ""}
      ${renderArtifactStaleBanner(result)}
      <div class="detail-source-links">${renderSourceRangeButtons(result.sourceRanges)}</div>
      <textarea class="detail-tool-editor" data-detail-tool-editor="${tool}" spellcheck="false">${escapeHtml(sanitizeAiDisplayText(text) || "暂无生成内容")}</textarea>
    </article>
  `;
}

function startDetailToolProgressAnimation(tool, panel, runToken) {
  let stageIndex = 0;
  if (isCurrentDetailToolRun(tool, runToken)) panel.innerHTML = renderDetailToolGenerating(tool, stageIndex);
  const timer = window.setInterval(() => {
    if (!isCurrentDetailToolRun(tool, runToken)) {
      window.clearInterval(timer);
      return;
    }
    const stages = detailToolProgressStages(tool);
    stageIndex = Math.min(stageIndex + 1, Math.max(0, stages.length - 2));
    panel.innerHTML = renderDetailToolGenerating(tool, stageIndex);
  }, 700);
  return () => window.clearInterval(timer);
}

function streamDetailToolResult(tool, result, panel, runToken) {
  if (!isCurrentDetailToolRun(tool, runToken)) return Promise.resolve();
  const text = detailToolEditableText(tool, result);
  const chars = Array.from(text || "暂无生成内容");
  const stages = detailToolProgressStages(tool, result);
  let index = 0;
  const step = Math.max(1, Math.ceil(chars.length / 42));
  panel.innerHTML = renderDetailToolGenerating(tool, Math.max(0, stages.length - 2), "");
  return new Promise((resolve) => {
    const timer = window.setInterval(() => {
      if (!isCurrentDetailToolRun(tool, runToken)) {
        window.clearInterval(timer);
        resolve();
        return;
      }
      index = Math.min(chars.length, index + step);
      const current = chars.slice(0, index).join("");
      panel.innerHTML = index >= chars.length
        ? renderDetailToolCompleted(tool, result, current)
        : renderDetailToolGenerating(tool, Math.max(0, stages.length - 2), current);
      if (index >= chars.length) {
        window.clearInterval(timer);
        resolve();
      }
    }, 24);
  });
}

async function copyDetailToolResult(tool) {
  const editor = document.querySelector(`[data-detail-tool-editor="${tool}"]`);
  const text = editor?.value || "";
  if (!text.trim()) return showToast("暂无可复制内容", "warning");
  await navigator.clipboard.writeText(text);
  showToast("已复制生成结果", "success");
}

async function applyDetailToolResultToMinutes(tool) {
  const editor = document.querySelector(`[data-detail-tool-editor="${tool}"]`);
  const content = editor?.value?.trim() || "";
  if (!content) return showToast("暂无可添加至纪要的内容", "warning");
  await apiRequest(`/api/meetings/${state.currentMeetingId}/minutes/draft`, {
    method: "POST",
    // Carry the currently viewed version so an edit made after history navigation is written to
    // that version's separate edited layer, never to whichever version happens to be current.
    body: { sourceTool: tool, content, versionId: state.minutesVersionIds[state.currentMeetingId] || null },
  });
  showToast("已添加至会议纪要", "success");
  await refreshMeetings();
}

async function saveDetailToolResult(tool) {
  const editor = document.querySelector(`[data-detail-tool-editor="${tool}"]`);
  const content = editor?.value?.trim() || "";
  if (!content) return showToast("暂无可保存内容", "warning");
  const saved = await apiRequest(`/api/meetings/${state.currentMeetingId}/ai-tools/${tool}/draft`, {
    method: "POST",
    body: { title: DETAIL_TOOL_TITLES[tool] || "AI 结果", content },
  });
  cacheDetailToolDraft(tool, { ui: { title: saved.title, editableText: saved.content } }, saved.content, saved.savedAt);
  showToast("已保存生成结果", "success");
  await refreshMeetings();
  showSavedDetailToolResult(tool);
}

function updateDetailSelectedText(event = null) {
  const target = event?.target || document.activeElement;
  let text = "";
  let segmentId = "";
  // 编辑态是 textarea，可直接通过 selectionStart/selectionEnd 读取局部选择；只读态现在把
  // 连续同一人的多个底层片段放进一个段落内，点击目标会是 span，必须走浏览器 Selection。
  // 显式检查 value 和 selectionStart，避免把只读 span 当输入框后调用 undefined.slice。
  if (
    target?.matches?.("[data-segment-display-text]")
    && typeof target.value === "string"
    && typeof target.selectionStart === "number"
  ) {
    const start = target.selectionStart ?? 0;
    const end = target.selectionEnd ?? 0;
    text = start !== end ? target.value.slice(start, end).trim() : "";
    segmentId = target.closest(".speech-segment")?.dataset.segmentId || "";
  } else {
    const selection = window.getSelection();
    text = selection?.toString().trim() || "";
    const anchor = selection?.anchorNode?.parentElement;
    segmentId = anchor?.closest?.(".speech-segment")?.dataset.segmentId || "";
  }
  if (text) {
    state.detailSelectedText = text;
    state.detailSelectedSegmentId = segmentId;
  }
}

function selectedTextForDetailMark() {
  updateDetailSelectedText();
  return {
    text: state.detailSelectedText || window.getSelection().toString().trim(),
    segmentId: state.detailSelectedSegmentId || "",
  };
}

function renderAudioTimeline(meeting) {
  // 之前这里写死了 00:03:47/00:05:33，用户会误以为有一段真实音频进度条。
  // 现在按已有片段的最大结束时间计算总时长；没有片段时显示等待状态，避免伪造进度。
  const segments = meeting.segments || [];
  const durationMs = Math.max(0, ...segments.map((segment) => Number(segment.endMs || segment.startMs || 0)));
  const audioClock = $("audioClock");
  if (audioClock) {
    const durationLabel = durationMs ? `00:00/${formatTime(durationMs)}` : "等待音频";
    audioClock.dataset.durationLabel = durationLabel;
    audioClock.textContent = `${durationLabel} · ${realtimeStatusText()}`;
  }
  const waveform = document.querySelector("#bottomAudioPlayer .waveform");
  if (waveform) {
    waveform.style.setProperty("--progress", state.realtimeRunning && durationMs ? "35%" : "0%");
    waveform.classList.toggle("empty", !durationMs);
    waveform.title = durationMs ? "根据当前转写片段生成的音频时间轴" : "开始实时会议或导入音频后显示时间轴";
  }
}

function renderSpeakerPanel(meeting) {
  const navigationTabs = document.querySelectorAll("[data-detail-navigation]");
  navigationTabs.forEach((button) => button.classList.toggle("active", button.dataset.detailNavigation === state.detailNavigationMode));
  const defaultSpeakerChoice = document.querySelector("#speakerPanel [data-speaker='']");
  if (defaultSpeakerChoice) defaultSpeakerChoice.hidden = state.detailNavigationMode === "chapters";
  if ($("addSpeakerFromDetailBtn")) $("addSpeakerFromDetailBtn").hidden = state.detailNavigationMode === "chapters";
  const navigationHint = $("navigationModeHint");

  if (state.detailNavigationMode === "chapters") {
    const summary = meeting.summaryArtifact || meeting.summary || {};
    const chapters = summary.sections || summary.generatedContent?.sections || [];
    $("detailNavigationTitle").textContent = "章节速览";
    $("speakerCount").textContent = `${chapters.length}章`;
    if (navigationHint) navigationHint.textContent = "按议题自动划分逐字稿，点击可跳转到对应录音；不参与发言人识别。";
    $("speakerList").innerHTML = chapters.length
      ? chapters.map((chapter, index) => {
        const source = chapter.sourceRanges?.[0] || {};
        const segmentId = source.segmentId || chapter.segmentId || chapter.sourceSegmentId || "";
        const startMs = Number(source.startMs ?? chapter.startMs ?? 0);
        return `<button class="speaker-row chapter-row" data-source-segment="${escapeHtml(segmentId)}" data-source-start-ms="${startMs}" ${segmentId ? "" : "disabled title=\"该章节暂无可定位来源\""}>
          <span><strong>${index + 1}. ${escapeHtml(chapter.title || `章节 ${index + 1}`)}</strong><small>${formatTime(startMs)} ${escapeHtml(chapter.content || chapter.summary || "")}</small></span>
        </button>`;
      }).join("")
      : `<div class="empty-state compact-empty">生成 AI 摘要后显示章节及音频定位</div>`;
    return;
  }

  const speakers = Array.from(
    (meeting.segments || []).reduce((map, segment) => {
      const name = segment.speakerName || "未识别发言人";
      const current = map.get(name) || { name, title: segment.speakerTitle || "", count: 0, confidence: 0 };
      current.count += 1;
      current.title = current.title || segment.speakerTitle || "";
      current.confidence = Math.max(current.confidence, Number(segment.voiceprintConfidence || 0));
      map.set(name, current);
      return map;
    }, new Map()).values(),
  );
  $("detailNavigationTitle").textContent = "发言人列表";
  $("speakerCount").textContent = `${speakers.length}人`;
  if (navigationHint) navigationHint.textContent = "按发言人筛选、定位和校正逐字稿。";
  $("speakerList").innerHTML = speakers.map((speaker, index) => `
    <button class="speaker-row" data-rename-speaker="${escapeHtml(speaker.name)}" data-speaker-title="${escapeHtml(speaker.title || "")}">
      <span>${escapeHtml(speaker.name)}${speaker.title ? `<small>${escapeHtml(speaker.title)}</small>` : ""}${speaker.confidence ? `<small>声纹 ${Math.round(speaker.confidence * 100)}%</small>` : ""}</span>
      <kbd>F${index + 1}</kbd>
    </button>
  `).join("");
}

function voiceprintRuntime() {
  // The settings health payload is the authority for enrollment. Missing data is deliberately
  // treated as unavailable instead of optimistic so a transient status request cannot enable a
  // fake registration action.
  return state.modelServices.voiceprint || {
    ready: false,
    mode: "unavailable",
    message: "声纹运行时状态尚未加载",
  };
}

function renderSpeakerRenameRuntimeControls() {
  const runtime = voiceprintRuntime();
  const dialog = $("speakerRenameDialog");
  let controls = $("speakerRenameRuntimeControls");
  if (!controls) {
    controls = document.createElement("div");
    controls.id = "speakerRenameRuntimeControls";
    controls.className = "modal-hint";
    dialog.querySelector(".modal-hint")?.before(controls);
  }
  // The two explicit values mirror the backend contract. Sync is disabled only when the capability
  // is known unavailable; meeting-only remains usable because it does not depend on model runtime.
  controls.innerHTML = `
    <label>保存范围<select id="speakerRenameSyncMode">
      <option value="meeting_only">仅本次会议</option>
      <option value="sync_voiceprint" ${runtime.ready ? "" : "disabled"}>同步声纹库</option>
    </select></label>
    <p>${escapeHtml(runtime.ready ? "声纹运行时已就绪；同步仍会在缺少真实样本时给出提示。" : `声纹运行时不可用：${runtime.message || "请先完成模型部署"}。仍可仅修改本次会议。`)}</p>
  `;
}

function openSpeakerRenameDialog(name, title = "") {
  state.speakerRenameMode = "rename";
  $("speakerRenameOldName").value = name || "";
  $("speakerRenameOriginal").value = name || "未识别发言人";
  $("speakerRenameName").value = name || "";
  $("speakerRenameTitle").value = title || "";
  renderSpeakerRenameRuntimeControls();
  $("speakerRenameDialog").showModal();
}

function openAssignSpeakerDialog() {
  if (!state.transcriptEditMode) {
    startTranscriptEditing();
    showToast("已进入编辑模式，请先勾选一个或多个底层片段，再点击添加发言人", "info");
    return;
  }
  if (!state.selectedTranscriptSegmentIds.size) {
    showToast("请先勾选需要分配发言人的片段", "warning");
    return;
  }
  state.speakerRenameMode = "assign";
  $("speakerRenameOldName").value = "";
  $("speakerRenameOriginal").value = `已选择 ${state.selectedTranscriptSegmentIds.size} 个片段`;
  $("speakerRenameName").value = "";
  $("speakerRenameTitle").value = "";
  renderSpeakerRenameRuntimeControls();
  // 片段分配是会议内的原子操作，不允许在这个入口误选“同步声纹库”。
  if ($("speakerRenameSyncMode")) {
    $("speakerRenameSyncMode").value = "meeting_only";
    $("speakerRenameSyncMode").disabled = true;
  }
  $("speakerRenameDialog").showModal();
}

async function renameSpeakerAcrossSegments(event) {
  event.preventDefault();
  if (state.speakerCorrectionSaving) return;
  const oldName = $("speakerRenameOldName").value || "";
  const name = $("speakerRenameName").value.trim();
  const department = $("speakerRenameTitle").value.trim();
  const syncMode = $("speakerRenameSyncMode")?.value || "meeting_only";
  const meeting = getCurrentMeeting();
  if (!meeting || !name || (state.speakerRenameMode !== "assign" && !oldName)) return showToast("请填写发言人姓名", "warning");

  const submitButton = event.currentTarget.querySelector('button[type="submit"]');
  const originalText = submitButton?.textContent || "保存";
  state.speakerCorrectionSaving = true;
  if (submitButton) {
    submitButton.disabled = true;
    submitButton.textContent = "保存中...";
  }
  try {
    if (state.speakerRenameMode === "assign") {
      // 所选片段必须在同一次 revision 校验和事务中完成分配；任何片段失效都由后端整体回滚。
      await apiRequest(`/api/meetings/${meeting.id}/segments/batch`, {
        method: "PATCH",
        body: {
          expectedTranscriptRevision: Number(meeting.transcriptRevision || state.transcriptEditRevision || 0),
          updates: Array.from(state.selectedTranscriptSegmentIds).map((segmentId) => ({ segmentId, speakerName: name })),
        },
      });
      state.selectedTranscriptSegmentIds.clear();
      state.speakerRenameMode = "rename";
      $("speakerRenameDialog").close();
      resetTranscriptEditing();
      await loadInitialData();
      showToast("已为所选片段原子分配发言人", "success");
      return;
    }
    // One server request owns the scope, durable rename, and one revision bump. The browser no
    // longer PATCHes each segment or creates voiceprint records itself, which prevented partial
    // client failures from producing an untraceable mix of transcript and library state.
    const result = await apiRequest(`/api/meetings/${meeting.id}/speaker-correction`, {
      method: "POST",
      body: { oldName, name, department, syncMode },
    });
    $("speakerRenameDialog").close();
    if (result.warning) showToast(`发言人已修改：${result.warning}`, "warning");
    else if (syncMode === "sync_voiceprint") showToast("发言人与声纹库元数据已同步", "success");
    else showToast("发言人已仅在本次会议中修改", "success");
    await loadInitialData();
  } finally {
    state.speakerCorrectionSaving = false;
    if (submitButton) {
      submitButton.disabled = false;
      submitButton.textContent = originalText;
    }
  }
}

function renderVoiceprintManager() {
  const groups = state.voiceprintGroups.length ? state.voiceprintGroups : [{ id: "vg-all", name: "全部" }];
  $("voiceprintGroupList").innerHTML = groups.map((group) => `
    <div class="voiceprint-group-row ${state.selectedVoiceprintGroupId === group.id ? "active" : ""}">
      <button class="voiceprint-group-select" data-voiceprint-group="${escapeHtml(group.id)}" title="查看${escapeHtml(group.name)}">${escapeHtml(group.name)}</button>
      ${group.isSystem ? "" : `<button class="voiceprint-group-edit" data-edit-voiceprint-group="${escapeHtml(group.id)}" title="修改分组名称和说明" aria-label="编辑${escapeHtml(group.name)}">编辑</button>`}
    </div>
  `).join("");
  const keyword = ($("voiceprintSearch")?.value || "").trim();
  const rows = state.voiceprints.filter((item) => {
    const inGroup = state.selectedVoiceprintGroupId === "vg-all" || item.groupId === state.selectedVoiceprintGroupId;
    const inSearch = !keyword || `${item.name}${item.department}${item.groupName}`.includes(keyword);
    return inGroup && inSearch;
  });
  syncVoiceprintSelectionState(rows);
  $("voiceprintTableBody").innerHTML = rows.length ? rows.map((item) => `
    <tr>
      <td><input type="checkbox" data-voiceprint-check="${item.id}" ${state.selectedVoiceprintIds.has(item.id) ? "checked" : ""} /></td>
      <td>${escapeHtml(item.name || item.speakerName)}</td>
      <td>${escapeHtml(item.department || "未分配")}</td>
      <td>${escapeHtml(item.lastMatchedAt || item.updatedAt || "未识别")}</td>
      <td>${escapeHtml(item.groupName || "未分组")}</td>
      <td>${badge(voiceprintStatusText(item), item.registerStatus || "pending")}</td>
      <td class="row-actions"><button data-edit-voiceprint="${item.id}">编辑</button><button data-upload-sample="${item.id}">上传样本</button><button data-delete-voiceprint="${item.id}">删除</button></td>
    </tr>
  `).join("") : `<tr><td colspan="7" class="empty-cell">当前分组暂无声纹模型。</td></tr>`;
  syncVoiceprintSelectionState(rows);
}

function openVoiceprintGroupDialog(group = null) {
  const editing = group && !group.isSystem ? group : null;
  state.voiceprintGroupEditingId = editing?.id || "";
  $("voiceprintGroupDialogTitle").textContent = editing ? "编辑声纹分组" : "新建声纹分组";
  $("voiceprintGroupName").value = editing?.name || "";
  $("voiceprintGroupDescription").value = editing?.description || "";
  $("deleteVoiceprintGroupBtn").hidden = !editing;
  $("voiceprintGroupDialog").showModal();
  $("voiceprintGroupName").focus();
}

async function submitVoiceprintGroup(event) {
  event.preventDefault();
  const name = $("voiceprintGroupName").value.trim();
  const description = $("voiceprintGroupDescription").value.trim();
  if (!name) return showToast("请填写声纹分组名称", "warning");
  const duplicate = state.voiceprintGroups.some(
    (item) => item.id !== state.voiceprintGroupEditingId && String(item.name || "").trim().toLowerCase() === name.toLowerCase(),
  );
  if (duplicate) return showToast("声纹分组名称已存在", "warning");
  const editingId = state.voiceprintGroupEditingId;
  if (editingId) {
    await apiRequest(`/api/voiceprint-groups/${editingId}`, { method: "PATCH", body: { name, description } });
    showToast("声纹分组已更新", "success");
  } else {
    const created = await apiRequest("/api/voiceprint-groups", { method: "POST", body: { name, description } });
    state.selectedVoiceprintGroupId = created.id;
    showToast("声纹分组已创建", "success");
  }
  $("voiceprintGroupDialog").close();
  state.voiceprintGroupEditingId = "";
  await refreshConfigData();
}

function requestVoiceprintGroupDeletion() {
  const group = state.voiceprintGroups.find((item) => item.id === state.voiceprintGroupEditingId);
  if (!group || group.isSystem) return showToast("系统声纹分组不可删除", "warning");
  $("voiceprintGroupDialog").close();
  openActionConfirmDialog({
    title: "删除声纹分组",
    description: `删除“${group.name}”后，组内声纹资料会移动到“未分组”，声纹模型本身不会删除。`,
    confirmLabel: "删除分组",
    onConfirm: async () => {
      await apiRequest(`/api/voiceprint-groups/${group.id}`, { method: "DELETE" });
      if (state.selectedVoiceprintGroupId === group.id) state.selectedVoiceprintGroupId = "vg-all";
      state.voiceprintGroupEditingId = "";
      await refreshConfigData();
      showToast("声纹分组已删除，组内资料已移至未分组", "success");
    },
  });
}

function syncVoiceprintSelectionState(visibleRows = []) {
  const visibleIds = new Set(visibleRows.map((row) => String(row.id)));
  // Filter/group changes remove invisible selections so an operator can inspect every row that a
  // batch action will affect. The buttons and tri-state checkbox derive from that visible set.
  state.selectedVoiceprintIds = new Set(
    [...state.selectedVoiceprintIds].filter((id) => visibleIds.has(String(id))),
  );
  const selectedCount = state.selectedVoiceprintIds.size;
  const deleteButton = $("batchDeleteVoiceprintBtn");
  const downloadButton = $("batchDownloadVoiceprintBtn");
  if (deleteButton) deleteButton.disabled = selectedCount === 0;
  if (downloadButton) downloadButton.disabled = selectedCount === 0;
  const selectCurrent = $("selectCurrentVoiceprints");
  if (selectCurrent) {
    selectCurrent.disabled = visibleRows.length === 0;
    selectCurrent.checked = visibleRows.length > 0 && selectedCount === visibleRows.length;
    selectCurrent.indeterminate = selectedCount > 0 && selectedCount < visibleRows.length;
  }
}

function renderOptimizationCenter() {
  $$(".optimization-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.optTab === state.optimizationTab));
  $$(".optimization-panel").forEach((panel) => panel.classList.toggle("active", panel.id.toLowerCase().includes(state.optimizationTab)));
  const currentManual = state.manualKeywords.find((item) => item.language === state.optimizationLanguage) || state.manualKeywords[0] || {};
  $("manualKeywordsInput").value = (currentManual.words || []).join("；");
  $("replacementRuleList").innerHTML = state.replacementRules.map((rule) => `
    <div class="rule-row ${rule.enabled === false ? "is-disabled" : ""}">
      <span>${escapeHtml(rule.wrongWord)}</span><b>→</b><strong>${escapeHtml(rule.correctWord)}</strong>
      <em>${rule.enabled === false ? "停用" : "启用"}</em>
      <div class="row-actions">
        <button type="button" data-edit-replacement="${escapeHtml(rule.id)}">编辑</button>
        <button type="button" data-toggle-replacement="${escapeHtml(rule.id)}">${rule.enabled === false ? "启用" : "停用"}</button>
        <button type="button" data-delete-replacement="${escapeHtml(rule.id)}">删除</button>
      </div>
    </div>
  `).join("") || `<div class="empty-state">暂无强制替换规则</div>`;
}

function renderSensitivePage() {
  const modeLabels = { hide: "隐藏", space: "空白", stars: "星号" };
  $("sensitiveRuleList").innerHTML = state.sensitiveRules.map((rule) => `
    <div class="rule-row ${rule.enabled === false ? "is-disabled" : ""}">
      <span>${escapeHtml(rule.word)}</span>
      <b>${escapeHtml(modeLabels[rule.displayMode || rule.replacement] || rule.displayMode || rule.replacement)}</b>
      <em>${rule.enabled === false ? "停用" : "启用"} · ${escapeHtml(rule.applyScope || rule.scope || "display,export")}</em>
      <div class="row-actions">
        <button type="button" data-edit-sensitive="${escapeHtml(rule.id)}">编辑</button>
        <button type="button" data-toggle-sensitive="${escapeHtml(rule.id)}">${rule.enabled === false ? "启用" : "停用"}</button>
        <button type="button" data-delete-sensitive="${escapeHtml(rule.id)}">删除</button>
      </div>
    </div>
  `).join("") || `<div class="empty-state">暂无禁忌词规则</div>`;
}

function renderTemplateCenter() {
  const keyword = ($("templateSearch")?.value || "").trim();
  $$(".template-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.templateSource === state.templateSource));
  const templates = state.templates.filter((template) => (template.source || "my") === state.templateSource && (!keyword || template.name.includes(keyword)));
  const importCard = state.templateSource === "my" ? `<article id="importTemplateCard" class="template-card import-card" data-import-template><span>＋</span><strong>导入模板</strong><em>支持 docx/txt/pptx 识别与标签配置</em></article>` : "";
  $("templateGrid").innerHTML = importCard + templates.map((template) => `
    <article class="template-card ${template.previewType || "custom"}">
      <div class="template-preview" data-preview-template="${template.id}">${renderTemplateMiniPreview(template)}</div>
      <footer><strong>${escapeHtml(template.name)}</strong><p>${escapeHtml(template.description || template.type)}</p><div>${(template.tags || []).map((tag) => `<span class="chip">${escapeHtml(tag)}</span>`).join("")}</div>
      <div class="row-actions"><button data-preview-template="${template.id}">查看/配置</button>${template.isSystem ? `<button data-copy-template="${template.id}">复制到我的模板</button>` : `<button data-default-template="${template.id}">设为默认</button><button data-delete-template="${template.id}">删除</button>`}</div></footer>
    </article>
  `).join("");
}

function renderTemplateMiniPreview(template) {
  // 根据模板类型绘制轻量预览，不复制第三方素材，只保留“表格型/红头型/专题型/通用型”的信息结构。
  const type = template.previewType || "custom";
  if (type === "redhead") {
    return `<div class="mini-template redhead"><h4>会议纪要</h4><p>（ 年第 次）</p><span>会议主题：</span><span>会议时间：</span><span>会议地点：</span></div>`;
  }
  if (type === "enterprise") {
    return `<div class="mini-template table"><h4>会议纪要</h4><div>会议主题</div><div>会议时间</div><div>会议地点</div><div>主持人</div><p>会议纪要：</p></div>`;
  }
  if (type === "topic") {
    return `<div class="mini-template topic"><h4>专题会议纪要</h4><p>专题背景</p><p>核心观点</p><p>风险问题</p></div>`;
  }
  return `<div class="mini-template general"><h4>${escapeHtml(template.name || "会议纪要")}</h4><p>会议信息</p><p>会议纪要</p><p>待办事项</p></div>`;
}

function renderTemplateTagEditor() {
  const tags = ["会议主题", "会议时间", "会议地点", "主持人", "记录人", "参会人", "会议纪要", "会议待办", "文本输入"];
  $("templateTagEditor").innerHTML = tags.map((tag) => `
    <button type="button" class="${state.importingTemplateTags.includes(tag) ? "active" : ""}" data-template-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>
  `).join("");
}

function openTemplateImportDialog() {
  // 打开弹窗时重置临时状态，保证每次导入都从干净的标签配置开始。
  state.importingTemplateFile = null;
  state.importingTemplateParsed = null;
  state.importingTemplateTags = ["会议主题", "会议时间", "会议地点", "主持人", "记录人", "参会人", "会议纪要"];
  $("templateImportForm").reset();
  $("templateParseStatus").textContent = "未上传";
  $("templateImportPreviewTitle").textContent = "模板预览";
  $("templateImportPreviewBody").textContent = "请选择本地模板文件。";
  renderTemplateTagEditor();
  $("templateImportDialog").showModal();
}

async function parseTemplateFile() {
  const file = $("templateFileInput")?.files?.[0];
  if (!file) return showToast("请先选择本地模板文件", "warning");
  state.importingTemplateFile = file;
  $("templateImportName").value = $("templateImportName").value || file.name.replace(/\.[^.]+$/, "");
  $("templateParseStatus").textContent = "识别中";
  setTemplateImportBusy(true);
  const form = new FormData();
  form.append("file", file);
  form.append("name", $("templateImportName").value.trim() || file.name.replace(/\.[^.]+$/, ""));
  form.append("templateType", $("templateImportType").value.trim() || "自定义会议");
  form.append("tags", state.importingTemplateTags.join(","));
  form.append("isDefault", $("templateImportDefault").checked ? "true" : "false");
  try {
    // 识别动作必须走后端。后端会解析 txt/docx/pptx、生成标签绑定和预览内容；
    // 前端只负责展示结果，不再用文件名伪造 docx/pptx 预览。
    const parsed = await apiRequest("/api/minute-templates/parse-file", { method: "POST", body: form });
    state.importingTemplateParsed = parsed;
    state.importingTemplateTags = parsed.tags?.length ? parsed.tags : state.importingTemplateTags;
    $("templateImportPreviewTitle").textContent = parsed.name || $("templateImportName").value || "导入模板预览";
    $("templateImportPreviewBody").innerHTML = renderTemplatePaper(parsed.content || "", state.importingTemplateTags);
    $("templateParseStatus").textContent = "已识别";
    showToast("模板识别完成，请确认标签后保存", "success");
  } catch (error) {
    $("templateParseStatus").textContent = "识别失败";
    throw error;
  } finally {
    setTemplateImportBusy(false);
  }
  renderTemplateTagEditor();
}

function renderTemplatePaper(text, tags = []) {
  const escaped = escapeHtml(text || "");
  const tagList = tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
  return `<div class="template-paper-tags">${tagList}</div><pre>${escaped}</pre>`;
}

function setTemplateImportBusy(isBusy) {
  // 导入弹窗里“识别”和“保存”都可能触发文件上传；统一禁用按钮可以防止用户连续点击导致重复请求。
  ["parseTemplateFileBtn", "saveImportedTemplateBtn"].forEach((id) => {
    const button = $(id);
    if (button) button.disabled = isBusy;
  });
}

async function saveImportedTemplate() {
  const file = state.importingTemplateFile || $("templateFileInput")?.files?.[0];
  if (!file) return showToast("请先上传并识别模板文件", "warning");
  if (!state.importingTemplateParsed) {
    // 用户可能选中文件后直接点保存。这里自动补一次后端识别，保证落库数据和预览逻辑一致。
    await parseTemplateFile();
  }
  const form = new FormData();
  form.append("file", file);
  form.append("name", $("templateImportName").value.trim() || file.name.replace(/\.[^.]+$/, ""));
  form.append("templateType", $("templateImportType").value.trim() || "自定义会议");
  form.append("tags", state.importingTemplateTags.join(","));
  form.append("isDefault", $("templateImportDefault").checked ? "true" : "false");
  $("templateParseStatus").textContent = "保存中";
  setTemplateImportBusy(true);
  try {
    await apiRequest("/api/minute-templates/import-file", { method: "POST", body: form });
    $("templateImportDialog").close();
    state.templateSource = "my";
    showToast("模板已导入并生成标签配置", "success");
    await refreshConfigData();
  } finally {
    setTemplateImportBusy(false);
  }
}

function openTemplatePreview(id) {
  const template = state.templates.find((item) => item.id === id);
  if (!template) return;
  $("templatePreviewModalTitle").textContent = template.name;
  $("templatePreviewModalBody").innerHTML = `
    <div class="template-preview-layout">
      <aside><h3>文本标签</h3>${(template.tagBindings || []).map((item) => `<span>${escapeHtml(item.tag)}</span>`).join("")}</aside>
      <section class="template-preview-document">${renderTemplateMiniPreview(template)}<pre>${escapeHtml(template.content || "")}</pre></section>
    </div>
  `;
  $("templatePreviewDialog").showModal();
}

function renderIntegrationPage() {
  const meeting = getCurrentMeeting() || {};
  const status = meeting.integrationStatus || {};
  $("integrationStatusGrid").innerHTML = ["todoPush", "minutesArchive", "transcriptExport", "audioReturn"].map((key) => `<article class="integration-card"><strong>${integrationLabel(key)}</strong><span>${escapeHtml(status[key] || "待处理")}</span></article>`).join("");
}

function renderLibraryFilter() {
  const select = $("libraryFilter");
  const current = select.value || "all";
  select.innerHTML = `<option value="all">全部词库</option>${state.keywordLibraries.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}`;
  select.value = state.keywordLibraries.some((item) => item.id === current) ? current : "all";
}

function renderTemplateSelects() {
  // render() 会在搜索、轮询和配置刷新时频繁执行。先读取当前选项，重建 option 后再恢复，
  // 防止用户已经选好的模板、声纹库或优化来源被一次无关重绘重置。
  const previousSelectValues = Object.fromEntries(
    ["createTemplate", "importTemplateSelect", "createVoiceprintLibrary", "importVoiceprintGroup"]
      .map((id) => [id, $(id)?.value || ""]),
  );
  const options = state.templates.filter((item) => !item.isSystem).map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("");
  ["createTemplate", "importTemplateSelect"].forEach((id) => {
    if (!$(id)) return;
    $(id).innerHTML = options;
    if (Array.from($(id).options).some((option) => option.value === previousSelectValues[id])) $(id).value = previousSelectValues[id];
  });
  const voiceprintOptions = state.voiceprintGroups
    .filter((item) => item.id !== "vg-all")
    .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`)
    .join("");
  ["createVoiceprintLibrary", "importVoiceprintGroup"].forEach((id) => {
    if (!$(id)) return;
    $(id).innerHTML = `<option value="vg-all">全部可用声纹</option>${voiceprintOptions}`;
    if (Array.from($(id).options).some((option) => option.value === previousSelectValues[id])) $(id).value = previousSelectValues[id];
  });
  renderOptimizationPicker("createOptimizationPicker");
  renderOptimizationPicker("importOptimizationPicker");
  renderDocumentKeywordPicker("createDocumentKeywordPicker", state.selectedCreateDocumentIds);
  renderDocumentKeywordPicker("importDocumentKeywordPicker", state.selectedImportDocumentIds);
}

function renderDocumentKeywordPicker(id, selectedIds) {
  const host = $(id);
  if (!host) return;
  const confirmedDocuments = state.optimizationDocuments.filter((item) => item.confirmed === true && (item.keywords || []).length);
  host.innerHTML = confirmedDocuments.length
    ? confirmedDocuments.map((item) => `
      <label class="chip-check" title="${escapeHtml((item.keywords || []).join("、"))}">
        <input type="checkbox" data-document-keyword-pick="${escapeHtml(item.id)}" data-document-keyword-host="${escapeHtml(id)}" ${selectedIds.has(String(item.id)) ? "checked" : ""} />
        ${escapeHtml(item.filename || item.id)} · ${(item.keywords || []).length}词
      </label>
    `).join("")
    : `<span class="muted">暂无已确认词表，请先在识别优化中上传并确认</span>`;
}

function selectedDocumentKeywordIds(hostId) {
  return Array.from(document.querySelectorAll(`#${hostId} [data-document-keyword-pick]:checked`))
    .map((input) => input.dataset.documentKeywordPick)
    .filter(Boolean);
}

function optimizationProfileFromPicker(hostId, mode = "auto") {
  const selected = new Set(
    Array.from(document.querySelectorAll(`#${hostId} [data-optimization-pick]:checked`))
      .map((input) => input.dataset.optimizationPick),
  );
  // 下拉模式是快捷预设；auto 才读取下面的精细复选结果。所有来源都显式写入冻结快照，
  // 避免空对象在兼容逻辑中被解释为“全部开启”。
  if (mode === "manual") return { manual: true, document: false, smart: false, replacement: false };
  if (mode === "document") return { manual: false, document: true, smart: false, replacement: false };
  if (mode === "replacement") return { manual: false, document: false, smart: false, replacement: true };
  if (mode === "off") return { manual: false, document: false, smart: false, replacement: false };
  return Object.fromEntries(["manual", "document", "smart", "replacement"].map((key) => [key, selected.has(key)]));
}

function parseParticipantNames(value) {
  return [...new Set(String(value || "").split(/[，,;；\n]/).map((item) => item.trim()).filter(Boolean))];
}

function renderOptimizationPicker(id) {
  const host = $(id);
  if (!host) return;
  const previouslySelected = new Set(
    Array.from(host.querySelectorAll("[data-optimization-pick]:checked")).map((input) => input.dataset.optimizationPick),
  );
  const hasExistingControls = host.querySelector("[data-optimization-pick]") !== null;
  const manualCount = state.manualKeywords.reduce((count, item) => count + (item.words || []).length, 0);
  const replacementCount = state.replacementRules.length;
  const options = [
    { id: "manual", label: `手动关键词${manualCount ? ` ${manualCount}` : ""}`, checked: manualCount > 0 },
    { id: "document", label: "文档关键词", checked: false },
    { id: "smart", label: "智能推荐", checked: true },
    { id: "replacement", label: `强制替换${replacementCount ? ` ${replacementCount}` : ""}`, checked: replacementCount > 0 },
  ];
  // 快速会议只需要选择识别优化策略，不再让用户误以为这里是在选择“领域”或旧热词库。
  // 真正的关键词、文档和替换规则仍在“识别优化”页面维护，这里只是决定本次会议启用哪些优化类型。
  host.innerHTML = options.map((item) => {
    const checked = hasExistingControls ? previouslySelected.has(item.id) : item.checked;
    return `<label class="chip-check"><input type="checkbox" data-optimization-pick="${item.id}" ${checked ? "checked" : ""} />${escapeHtml(item.label)}</label>`;
  }).join("");
}

function renderHotwordPicker(id) {
  const host = $(id);
  if (!host) return;
  // 导入转写仍然需要按词库传 `keyword_library_ids` 给后端 ASR 热词增强；
  // 快速会议弹窗已经改为“识别优化模式”，两套控件语义不同，不能共用同一个渲染函数。
  host.innerHTML = state.keywordLibraries.map((item) => `
    <label class="chip-check">
      <input type="checkbox" data-hotword-pick="${item.id}" ${item.enabled ? "checked" : ""} />
      ${escapeHtml(item.name)}
    </label>
  `).join("");
}

function openQuickMeetingDialog() {
  const dialog = $("createMeetingDialog");
  const titleInput = $("createMeetingTitle");
  if (titleInput) {
    // 按参考图的“快速会议”场景，打开弹窗时自动给出可识别的默认主题，用户仍可手动修改。
    const now = new Date();
    const stamp = `${now.getMonth() + 1}-${now.getDate()} ${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
    titleInput.value = `快速会议 ${stamp}`;
  }
  state.createMeetingAttachmentFile = null;
  if ($("createMeetingAttachment")) $("createMeetingAttachment").value = "";
  if ($("createMeetingAttachmentName")) $("createMeetingAttachmentName").textContent = "未选择文件";
  if ($("clearCreateMeetingAttachmentBtn")) $("clearCreateMeetingAttachmentBtn").hidden = true;
  dialog?.showModal();
}

async function createMeeting(event) {
  event.preventDefault();
  const shouldStartTranscription = $("createTranscriptionEnabled")?.checked !== false;
  const meeting = await apiRequest("/api/meetings", {
    method: "POST",
    body: {
      meetingName: $("createMeetingTitle").value.trim() || "未命名会议",
      meetingLocation: $("createMeetingLocation")?.value.trim() || "",
      // 快速会议与导入转写使用同一套冻结配置；参会人、声纹库、优化来源、备注和附件元数据
      // 必须在会议开始前提交，不能只展示在弹窗里。
      language: $("createLanguage").value,
      translateDirection: "无",
      audioSource: $("createAudioSource").value,
      templateId: $("createTemplate").value,
      keywordLibraryIds: [],
      enableDiarization: $("createDiarization").checked,
      participantNames: parseParticipantNames($("createParticipants")?.value),
      voiceprintGroupId: $("createVoiceprintLibrary")?.value || "vg-all",
      optimizationProfile: optimizationProfileFromPicker("createOptimizationPicker", $("createOptimizationMode")?.value || "auto"),
      documentKeywordDocumentIds: selectedDocumentKeywordIds("createDocumentKeywordPicker"),
      notes: $("createMeetingNotes")?.value.trim() || "",
      attachments: state.createMeetingAttachmentFile ? [{
        name: state.createMeetingAttachmentFile.name,
        size: state.createMeetingAttachmentFile.size,
        contentType: state.createMeetingAttachmentFile.type || "application/octet-stream",
      }] : [],
    },
  });
  if (state.createMeetingAttachmentFile) {
    const attachmentForm = new FormData();
    attachmentForm.append("file", state.createMeetingAttachmentFile);
    try {
      await apiRequest(`/api/meetings/${meeting.id}/attachments`, { method: "POST", body: attachmentForm });
    } catch (error) {
      // 会议创建和附件上传是两个可恢复阶段。附件失败不能抹掉已创建会议，但必须明确告知用户。
      showToast(`会议已创建，但附件上传失败：${error.message}`, "warning");
    }
  }
  state.createMeetingAttachmentFile = null;
  $("createMeetingDialog").close();
  showToast("会议已创建", "success");
  await loadInitialData();
  openMeetingDetail(meeting.id);
  if (shouldStartTranscription) {
    try {
      // 快速会议的“创建会议”和“启动麦克风实时转写”是两个阶段。
      // 浏览器可能因为权限、设备占用或非安全上下文拒绝麦克风；这种失败不能抹掉已经成功落库的会议。
      await startRealtimeTranscription();
    } catch (error) {
      const realtimeMessage = error.message ? `：${error.message}` : "";
      showToast(`会议已创建，但麦克风不可用，实时转写未启动${realtimeMessage}`, "warning");
    }
  }
}

async function startImport() {
  const files = state.selectedFiles.length ? state.selectedFiles : Array.from($("audioFileInput")?.files || []);
  if (!files.length) return showToast("请先选择音视频文件", "warning");
  setImportProcessingState(true);
  const importedResults = [];
  let successCount = 0;
  let failedCount = 0;
  try {
    for (const file of files) {
      try {
        updateImportFileStatus(file.name, "上传和ASR处理中");
        const form = new FormData();
        form.append("file", file);
        // The visible option remains localized, while the submitted value is the ASR provider code.
        // Keeping a code fallback prevents dynamically missing controls from reviving the old label bug.
        form.append("language", $("importLanguage")?.value || "zh");
        form.append("template_id", $("importTemplateSelect")?.value || "");
        form.append("keyword_library_ids", selectedKeywordLibraryIds("importHotwordPicker").join(","));
        form.append("enable_diarization", $("importDiarization").checked ? "true" : "false");
        form.append("participant_names", JSON.stringify(parseParticipantNames($("importParticipants")?.value)));
        form.append("voiceprint_group_id", $("importVoiceprintGroup")?.value || "vg-all");
        form.append("optimization_profile", JSON.stringify(optimizationProfileFromPicker("importOptimizationPicker")));
        // 文档词表只提交用户在本批导入中明确勾选的已确认记录；上传但未确认的候选永远不会进入 ASR。
        form.append("document_keyword_document_ids", JSON.stringify(selectedDocumentKeywordIds("importDocumentKeywordPicker")));
        form.append("notes", $("importNotes")?.value.trim() || "");
        // 导入页提交的是“转写任务”，不是用户手动创建会议。后端会在任务内部创建承载记录，
        // 但这个记录只用于后续查看/导出，不再自动把用户带到实时会议页面。
        const imported = await apiRequest("/api/imports/transcribe", { method: "POST", body: form });
        const transcribed = imported.transcription || {};
        const fileStatus = transcribed.status === "failed"
          ? `识别失败：${transcribed.message || "真实 ASR 未返回有效结果"}`
          : transcribed.asrFallback
            ? "已完成（本地兜底）"
            : transcribed.status === "completed"
              ? "已完成"
              : transcribed.status
                ? `处理中：${transcribed.status}`
                : "已提交";
        importedResults.push({
          ...imported,
          fileName: file.name,
          statusText: fileStatus,
        });
        state.importResults = [...importedResults];
        updateImportFileStatus(file.name, fileStatus);
        if (transcribed.status === "failed") {
          failedCount += 1;
          showToast(transcribed.message || "导入转写识别失败，请检查音频或稍后重试", "error");
        } else {
          successCount += 1;
          // 会后处理在单独请求中执行，导入台账先完成并可用；摘要、发言总结、待办或纪要任一失败都可在任务列表单项查看。
          void runPostMeetingPipeline(imported.meeting?.id);
        }
        if (transcribed.asrFallback) {
          showToast(transcribed.message || "真实 ASR 不可用，已使用本地兜底转写", "warning");
        }
      } catch (error) {
        failedCount += 1;
        updateImportFileStatus(file.name, `失败：${error.message || "导入转写异常"}`);
        showToast(error.message || "导入转写失败", "error");
      }
    }
    if (!importedResults.length) return;
    // 导入转写完成后仍留在台账视图；正文不再直接铺在列表下方，避免台账被转写内容撑长。
    // 用户通过每条记录的“查看”进入详情页继续编辑、导出或生成纪要。
    if (successCount) {
      showToast("文件处理完成，已加入导入台账，请点击查看进入详情", "success");
    } else if (failedCount) {
      showToast("文件已加入导入台账，但识别失败，请查看失败原因后重试", "warning");
    }
    await loadInitialData();
    renderImportPage();
  } finally {
    setImportProcessingState(false);
  }
}

function setImportProcessingState(isProcessing) {
  // 导入处理是长链路：创建会议 -> 上传 -> ASR -> 声纹 -> 对齐 -> 详情页刷新。
  // 这里集中控制按钮禁用和页面刷新，避免每个 await 前后都散落 DOM 操作。
  state.importProcessing = isProcessing;
  renderImportPage();
}

async function runPostMeetingPipeline(meetingId) {
  if (!meetingId) return;
  try {
    const result = await apiRequest(`/api/meetings/${meetingId}/postprocess`, { method: "POST", body: {} });
    const failed = Object.values(result.items || {}).filter((item) => item.status === "failed");
    if (failed.length) showToast(`会后处理完成，但有 ${failed.length} 项失败，可在任务状态中重试`, "warning");
    else showToast("章节、摘要、发言总结、待办和纪要已生成", "success");
    if (state.currentMeetingId === meetingId) await loadInitialData();
  } catch (error) {
    showToast(`会后处理未完成：${error.message}`, "warning");
  }
}

function updateImportFileStatus(fileName, status) {
  // 文件对象本身由浏览器管理，不能作为可持久状态。用文件名做轻量 key，足够支撑当前批量导入列表展示。
  state.importFileStatuses = { ...state.importFileStatuses, [fileName]: status };
  renderImportPage();
}

async function loadMeetingJobs() {
  if (!state.currentMeetingId) return;
  const data = await apiRequest(`/api/meetings/${state.currentMeetingId}/jobs`);
  state.jobs = data.items || [];
  renderPipeline();
}

function websocketBaseUrl() {
  // API_BASE 支持 http/https 和 query 参数覆盖。WebSocket 需要把协议替换成 ws/wss，
  // 其它主机、端口和路径规则保持一致，避免本地 8001、服务器域名、反向代理三套写法。
  return API_BASE.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
}

function newRealtimeSessionToken(meetingId = state.currentMeetingId) {
  // 每次点击“开始会议”都生成一个前端会话 token。WebSocket 是长连接，用户暂停、
  // 重新开始或切换会议时，旧连接仍可能晚几百毫秒返回 transcript；没有 token 时这些
  // 迟到消息会写进当前详情，表现成“会议没开始却有结果”。token 不参与鉴权，只用于
  // 前后端在同一浏览器会话内确认“这条实时结果属于当前这次采集”。
  const randomPart = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `rt-session-${meetingId || "meeting"}-${randomPart}`;
}

function isCurrentRealtimeEvent(event = {}) {
  // 后端会把 realtime_config/realtime_chunk 中的 sessionToken 原样回传。当前端已经
  // 进入新的实时会话后，旧 WebSocket 或旧轮询返回的数据必须被丢弃，否则会污染当前
  // 编辑器正文。这里同时校验 meetingId 和 sessionToken：meetingId 防串会议，
  // sessionToken 防同一会议内暂停/继续造成的旧结果晚到。
  if (event.meetingId && event.meetingId !== state.currentMeetingId) return false;
  if (!state.realtimeSessionToken) return true;
  return event.sessionToken === state.realtimeSessionToken;
}

const REALTIME_SPEAKER_UPDATE_FIELDS = [
  "speakerName",
  "speakerTitle",
  "speakerClusterId",
  "speakerSource",
  "voiceprintId",
  "voiceprintConfidence",
];

function isCurrentSpeakerUpdate(event = {}) {
  // 普通 transcript 事件暂时兼容少量旧后端消息；speaker_update 则是会在最终文本之后
  // 才完成的后台任务，最容易跨过“暂停/继续”边界迟到。因此它必须同时携带并严格匹配
  // meetingId 与 sessionToken。任一值为空都直接丢弃，不能在会话结束后更新当前页面。
  return Boolean(
    event.meetingId
    && event.sessionToken
    && state.currentMeetingId
    && state.realtimeSessionToken
    && String(event.meetingId) === String(state.currentMeetingId)
    && event.sessionToken === state.realtimeSessionToken
  );
}

async function ensureRealtimeMeeting() {
  // 用户可能直接进入“实时会议”页，没有先在弹窗里创建会议。此时自动创建一个会议，
  // 这样播放按钮和继续转写按钮永远有可落库的 meetingId。
  if (state.currentMeetingId && getCurrentMeeting()) return getCurrentMeeting();
  const meeting = await apiRequest("/api/meetings", {
    method: "POST",
    body: {
      meetingName: `实时会议 ${new Date().toLocaleString("zh-CN", { hour12: false })}`,
      audioSource: "麦克风阵列",
      keywordLibraryIds: [],
      enableDiarization: true,
    },
  });
  state.currentMeetingId = meeting.id;
  await loadInitialData();
  state.currentMeetingId = meeting.id;
  return meeting;
}

function encodeWavFromSamples(samples, sampleRate) {
  // 浏览器 MediaRecorder 常输出 webm/opus，很多 ASR HTTP 接口不能稳定识别。
  // 因此实时会议直接把 Web Audio 采集到的 Float32 PCM 编成 16-bit WAV，后端按 audio/wav 调用 qwen3-asr-flash。
  const bytesPerSample = 2;
  const dataSize = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);
  const writeString = (offset, text) => {
    for (let index = 0; index < text.length; index += 1) view.setUint8(offset + index, text.charCodeAt(index));
  };
  writeString(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, dataSize, true);
  let offset = 44;
  for (const sample of samples) {
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return buffer;
}

function analyzeRealtimeAudioQuality(samples, sampleRate) {
  // 这里只做轻量能量门控，不做完整 VAD：目标是挡掉“明显没有人在说话”的分片，
  // 真正的语义识别仍交给后端 ASR。为了避免安静会议室的小声发言被误杀，判断由两部分组成：
  // 1) 一组很低的基础门限，保证普通人声可以放行；
  // 2) 浏览器当前噪声底的倍数，避免空调声、键盘声或虚拟麦克风底噪被当成人声。
  if (!samples.length || !sampleRate) {
    return { durationMs: 0, rms: 0, peak: 0, activeRatio: 0, hasVoice: false, hasSpeechLikeAudio: false, reason: "empty" };
  }
  let squareSum = 0;
  let peak = 0;
  for (const sample of samples) {
    const absolute = Math.abs(sample);
    squareSum += absolute * absolute;
    peak = Math.max(peak, absolute);
  }
  const rms = Math.sqrt(squareSum / samples.length);
  const noiseFloor = state.realtimeNoiseFloorRms || REALTIME_MIN_RMS;
  const dynamicRmsThreshold = Math.max(REALTIME_MIN_RMS, noiseFloor * 3);
  const dynamicPeakThreshold = Math.max(REALTIME_MIN_PEAK, noiseFloor * 6);
  const activeThreshold = Math.max(REALTIME_ACTIVE_SAMPLE_LEVEL, noiseFloor * 2);
  let activeSamples = 0;
  for (const sample of samples) {
    if (Math.abs(sample) >= activeThreshold) activeSamples += 1;
  }
  const activeRatio = activeSamples / samples.length;
  const durationMs = Math.round((samples.length / sampleRate) * 1000);
  const hasSpeechLikeAudio =
    rms >= dynamicRmsThreshold &&
    peak >= dynamicPeakThreshold &&
    activeRatio >= REALTIME_MIN_ACTIVE_RATIO;
  if (!hasSpeechLikeAudio && rms < 0.02) {
    // 只用“未判成人声且整体能量很低”的帧更新噪声底。人声帧不能进入噪声模型，
    // 否则用户持续说话时门限会被越抬越高，最终又回到误判低音量的问题。
    state.realtimeNoiseFloorSamples += 1;
    const weight = state.realtimeNoiseFloorSamples === 1 ? 1 : 0.08;
    state.realtimeNoiseFloorRms = state.realtimeNoiseFloorRms
      ? state.realtimeNoiseFloorRms * (1 - weight) + rms * weight
      : rms;
  }
  return {
    durationMs,
    rms,
    peak,
    activeRatio,
    noiseFloor,
    hasVoice: hasSpeechLikeAudio,
    hasSpeechLikeAudio,
    reason: hasSpeechLikeAudio ? "voice" : "low_volume",
  };
}

function notifyRealtimeChunkSkipped(message = "当前音频分片音量过低，已跳过实时转写", detail = {}) {
  // 跳过静音是正常会议行为。这里把主要反馈放到编辑区和播放器状态里，toast 只做低频提醒；
  // 否则用户会看到“识别中/已暂停/低音量”多条消息堆叠，反而更难判断麦克风是否工作。
  setRealtimeStatus("low_volume", { ...detail, message });
  const now = Date.now();
  if (now - state.lastRealtimeSkipNoticeAt >= REALTIME_SKIP_NOTICE_MS) {
    state.lastRealtimeSkipNoticeAt = now;
    showToast(message, "warning");
  }
}

function mergeRealtimeAudioBuffers() {
  const totalLength = state.realtimeAudioBuffers.reduce((sum, buffer) => sum + buffer.length, 0);
  const merged = new Float32Array(totalLength);
  let offset = 0;
  for (const buffer of state.realtimeAudioBuffers) {
    merged.set(buffer, offset);
    offset += buffer.length;
  }
  state.realtimeAudioBuffers = [];
  return merged;
}

function resolveRealtimeStopWait() {
  // Both the explicit backend ``closed`` event and a transport-level close may finish the stop handshake.
  // Keeping resolution idempotent prevents an onmessage/onclose race from running cleanup twice.
  const resolver = state.realtimeStopResolver;
  state.realtimeStopResolver = null;
  if (resolver) resolver();
}

function waitForRealtimeServerClose(timeoutMs = 6000) {
  // DashScope finalizes its buffered utterance after ``session.finish``. The backend forwards that final
  // transcript and only then sends ``closed``; waiting here is what keeps the last spoken sentence visible.
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      state.realtimeStopResolver = null;
      resolve();
    }, timeoutMs);
    state.realtimeStopResolver = () => {
      window.clearTimeout(timer);
      resolve();
    };
  });
}

function isActiveRealtimeConnection(socket, sessionToken) {
  // socket 防止旧连接回调操作新连接，token 防止同一 socket 之外的迟到业务消息串会话。
  // 两者必须同时匹配，任何一个变化都说明该异步回调已经过期，应当静默退出。
  return state.realtimeSocket === socket && state.realtimeSessionToken === sessionToken;
}

async function startRealtimeTranscription() {
  if (state.realtimeRunning || state.realtimeConnecting) {
    showToast("实时转写正在进行中", "info");
    return;
  }
  state.realtimeConnecting = true;
  syncRealtimeControls(false);
  let meeting;
  try {
    meeting = await ensureRealtimeMeeting();
  } catch (error) {
    state.realtimeConnecting = false;
    syncRealtimeControls(false);
    throw error;
  }
  state.realtimeActiveMeetingId = meeting.id;
  const sessionToken = newRealtimeSessionToken(meeting.id);
  state.realtimeSessionToken = sessionToken;
  const socket = new WebSocket(`${websocketBaseUrl()}/api/meetings/${encodeURIComponent(meeting.id)}/realtime`);
  state.realtimeSocket = socket;
  state.realtimeStopIntent = "";
  setRealtimeStatus("listening", {}, { render: true });
  return new Promise((resolve, reject) => {
    let opened = false;
    socket.onopen = async () => {
      if (!isActiveRealtimeConnection(socket, sessionToken)) return;
      opened = true;
      state.realtimeConnecting = false;
      state.realtimeRunning = true;
      state.realtimeChunkIndex = 0;
      state.realtimeInFlightChunks = 0;
      state.realtimeLastTranscriptAt = 0;
      state.realtimeDraftText = "";
      setRealtimeStatus("listening", {}, { render: true });
      startRealtimeServerSync();
      renderMeetingDetailWorkspace();
      try {
        await startMicrophoneCapture(socket, sessionToken);
        if (!isActiveRealtimeConnection(socket, sessionToken)) return;
        showToast("实时会议已开始，正在采集麦克风", "success");
      } catch {
        if (!isActiveRealtimeConnection(socket, sessionToken)) return;
        opened = false;
        state.realtimeConnecting = false;
        state.realtimeRunning = false;
        state.realtimeSocket = null;
        state.realtimeInFlightChunks = 0;
        stopRealtimeServerSync();
        setRealtimeStatus("error", { message: "麦克风不可用，实时会议未开始转写" }, { render: true });
        renderMeetingDetailWorkspace();
        socket.close();
        reject(new Error("麦克风不可用，实时会议未开始转写"));
        return;
      }
      resolve();
    };
    socket.onmessage = (event) => {
      if (!isActiveRealtimeConnection(socket, sessionToken)) return;
      handleRealtimeMessage(event.data);
    };
    socket.onerror = () => {
      if (!isActiveRealtimeConnection(socket, sessionToken)) return;
      const error = new Error("实时转写连接失败，请确认后端 WebSocket 可用");
      setRealtimeStatus("error", { message: error.message }, { render: true });
      if (!opened) reject(error);
      else showToast(error.message, "error");
    };
    socket.onclose = () => {
      if (!isActiveRealtimeConnection(socket, sessionToken)) return;
      resolveRealtimeStopWait();
      const userPaused = state.realtimeStopIntent === "pause";
      const stopIntent = state.realtimeStopIntent;
      stopRealtimeMediaCapture();
      state.realtimeSocket = null;
      state.realtimeConnecting = false;
      state.realtimeRunning = false;
      state.realtimeInFlightChunks = 0;
      stopRealtimeServerSync();
      // WebSocket 关闭有三种语义：用户主动暂停、用户结束会议、网络/服务异常断开。
      // 之前空 stopIntent 会被当成 idle，页面看起来像“自己暂停了”；现在异常断开明确显示 error，
      // 便于用户区分“我点了暂停”和“连接掉了，需要重新开始会议”。
      const nextClosedStatus = stopIntent === "pause"
        ? "paused"
        : stopIntent === "end"
          ? "idle"
          : state.realtimeStatus === "error"
            ? "error"
            : "error";
      setRealtimeStatus(
        nextClosedStatus,
        nextClosedStatus === "error" ? { message: "实时转写连接已断开，请点击开始会议重新连接" } : {},
      );
      if (stopIntent === "end") state.realtimeSessionToken = "";
      state.realtimeActiveMeetingId = "";
      state.realtimeStopIntent = "";
      renderMeetingDetailWorkspace();
      if (opened && userPaused) showToast("实时转写已暂停", "info");
      else if (opened && nextClosedStatus === "error") showToast("实时转写连接已断开，请重新开始会议", "warning");
    };
  });
}

async function startRealtimeFromImport() {
  // 旧入口保留为兼容函数，但产品入口已经收敛到“会议列表 -> 快速会议”。
  // 如果仍有外部脚本调用它，也统一打开实时会议详情，而不是混入导入转写台账。
  const meeting = await ensureRealtimeMeeting();
  openMeetingDetail(meeting.id);
  await startRealtimeTranscription();
}

function handleRealtimeMessage(rawMessage) {
  // 后端返回 `{type:"transcript", segment:{...}}`。这里先更新前端内存，让用户马上看到文本；
  // 后端同时已经 store.add_realtime_segment 落库，后续刷新页面仍能保留转写片段。
  let event;
  try {
    event = JSON.parse(rawMessage);
  } catch {
    return;
  }
  if (!isCurrentRealtimeEvent(event)) return;
  if (event.type === "closed") {
    // The backend emits this only after the upstream provider has returned its final buffered transcript.
    // Release stopRealtimeTranscription now; closing before this acknowledgement used to drop the last sentence.
    resolveRealtimeStopWait();
    return;
  }
  if (event.type === "speaker_update") {
    if (!isCurrentSpeakerUpdate(event)) return;
    const meeting = getCurrentMeeting();
    if (!meeting || !event.segmentId) return;
    const affectedIds = new Set([
      String(event.segmentId),
      ...(Array.isArray(event.affectedSegmentIds) ? event.affectedSegmentIds.map(String) : []),
    ]);
    const speakerPatch = {};
    // 说话人分析在最终文本之后异步返回。这里只允许拷贝身份字段，并且只更新后端明确列出的
    // 稳定 segmentId；绝不能用数组下标或“最后一段”定位，否则迟到的声纹结果会覆盖下一条
    // 转写文本。白名单还确保事件即使意外携带 embedding，也不会进入浏览器业务状态。
    for (const field of REALTIME_SPEAKER_UPDATE_FIELDS) {
      if (Object.prototype.hasOwnProperty.call(event, field)) speakerPatch[field] = event[field];
    }
    let updated = false;
    meeting.segments = (meeting.segments || []).map((segment) => {
      if (!affectedIds.has(String(segment.id))) return segment;
      updated = true;
      return { ...segment, ...speakerPatch };
    });
    if (updated) renderMeetingDetailWorkspace();
    return;
  }
  if (event.type === "error") {
    state.realtimeInFlightChunks = Math.max(0, state.realtimeInFlightChunks - 1);
    setRealtimeStatus("error", { message: event.message || "实时 ASR 调用失败" }, { render: true });
    showToast(event.message || "实时 ASR 调用失败", "error");
    return;
  }
  if (event.type === "status") {
    state.realtimeInFlightChunks = Math.max(0, state.realtimeInFlightChunks - 1);
    if (event.code === "streaming_unavailable") {
      state.realtimeUseStreaming = false;
      setRealtimeStatus("error", { message: event.message || "实时流式转写不可用" }, { render: true });
      showToast(event.message || "实时流式转写不可用", "error");
    } else if (event.code === "low_volume" || event.message?.includes("跳过实时转写")) {
      notifyRealtimeChunkSkipped(event.message || "当前音频分片音量过低，已跳过实时转写", event);
      renderMeetingDetailWorkspace();
      syncRealtimeMeetingFromServer({ render: true });
    } else if (event.code === "asr_empty" || event.message?.includes("未识别到有效语音")) {
      setRealtimeStatus("asr_empty", event, { render: true });
    } else {
      setRealtimeStatus(state.realtimeRunning ? "listening" : "idle", event);
    }
    return;
  }
  if (event.type === "partial_transcript") {
    // Partial text gives immediate feedback while the provider is still listening. It should feel live, but it
    // must stay separate from persisted transcript segments so later final text can replace it cleanly instead
    // of creating duplicate editable paragraphs.
    state.realtimeDraftText = event.text || "";
    state.realtimeLastTranscriptAt = Date.now();
    setRealtimeStatus("transcribing", { source: "partial_transcript" });
    updateRealtimeDraftInline();
    return;
  }
  if (event.type !== "transcript" || !event.segment) return;
  const meeting = getCurrentMeeting();
  if (!meeting) return;
  state.realtimeInFlightChunks = Math.max(0, state.realtimeInFlightChunks - 1);
  state.realtimeLastTranscriptAt = Date.now();
  const exists = (meeting.segments || []).some((segment) => segment.id === event.segment.id);
  if (!exists) {
    meeting.segments = [...(meeting.segments || []), event.segment];
    meeting.processStatus = "processing";
    meeting.status = "processing";
  }
  state.realtimeDraftText = "";
  setRealtimeStatus("transcribing", { segmentId: event.segment.id });
  renderMeetingDetailWorkspace();
}

async function stopRealtimeTranscription(markCompleted = false) {
  // 暂停只关闭当前 WebSocket；结束会额外把会议状态标记为 completed，便于列表页和后续 AI 工具判断。
  state.realtimeStopIntent = markCompleted ? "end" : "pause";
  stopRealtimeMediaCapture();
  const socket = state.realtimeSocket;
  if (socket && socket.readyState === WebSocket.OPEN) {
    const serverClosed = waitForRealtimeServerClose();
    socket.send("stop");
    await serverClosed;
    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) socket.close();
  } else if (socket && socket.readyState === WebSocket.CONNECTING) {
    socket.close();
  }
  // 仅当前连接仍归本次 stop 所有时才清空全局身份；旧 stop 的异步等待不能覆盖后来启动的会话。
  if (!socket || state.realtimeSocket === socket) {
    state.realtimeSocket = null;
    state.realtimeConnecting = false;
    state.realtimeRunning = false;
  }
  state.realtimeInFlightChunks = 0;
  state.realtimeDraftText = "";
  stopRealtimeServerSync();
  if (markCompleted && state.currentMeetingId) {
    setRealtimeStatus("idle");
    await apiRequest(`/api/meetings/${state.currentMeetingId}`, {
      method: "PATCH",
      body: { processStatus: "completed", status: "completed", minutesStatus: "ready" },
    });
    await loadInitialData();
    showToast("会议已结束，正在生成章节、摘要、待办和纪要", "success");
    void runPostMeetingPipeline(state.currentMeetingId);
  } else {
    setRealtimeStatus("paused");
    renderMeetingDetailWorkspace();
  }
}

async function copyTemplate(id) {
  await apiRequest(`/api/minute-templates/${id}/copy`, { method: "POST", body: {} });
  showToast("系统模板已复制到我的模板", "success");
  state.templateSource = "my";
  await refreshConfigData();
}

function importTemplate() {
  // 保留函数名是为了兼容既有事件绑定和测试标记；实际行为改为打开系统内导入弹窗。
  openTemplateImportDialog();
}

function batchDeleteVoiceprints() {
  const ids = Array.from(state.selectedVoiceprintIds);
  if (!ids.length) return showToast("请先选择声纹模型", "warning");
  openActionConfirmDialog({
    title: "删除声纹模型",
    description: `将删除已选择的 ${ids.length} 个声纹模型，此操作无法撤销。`,
    onConfirm: () => deleteVoiceprintsBatch(ids),
  });
}

async function deleteVoiceprintsBatch(ids) {
  await apiRequest("/api/voiceprints/batch-delete", { method: "POST", body: { ids } });
  state.selectedVoiceprintIds.clear();
  showToast("已批量删除声纹模型", "success");
  await refreshConfigData();
}

async function batchDownloadVoiceprints() {
  const ids = Array.from(state.selectedVoiceprintIds);
  if (!ids.length) return showToast("请先选择声纹模型", "warning");
  const result = await apiRequest("/api/voiceprints/batch-download", { method: "POST", body: { ids } });
  showToast(`已生成 ${result.count} 个声纹样本下载条目`, "success");
}

function openActionConfirmDialog({ title, description, confirmLabel = "删除", onConfirm }) {
  const dialog = $("confirmActionDialog");
  if (!dialog || typeof onConfirm !== "function") return;
  // Only this confirmed callback can reach a destructive request; row and toolbar clicks merely
  // prepare it. This protects template, voiceprint, and meeting records from accidental deletion.
  state.pendingActionConfirmation = { onConfirm };
  $("confirmActionTitle").textContent = title;
  $("confirmActionDescription").textContent = description;
  $("confirmActionConfirmBtn").textContent = confirmLabel;
  if (!dialog.open) dialog.showModal();
}

async function confirmPendingAction() {
  const pending = state.pendingActionConfirmation;
  if (!pending) return;
  const button = $("confirmActionConfirmBtn");
  if (button) button.disabled = true;
  try {
    await pending.onConfirm();
    $("confirmActionDialog")?.close();
    state.pendingActionConfirmation = null;
  } finally {
    if (button) button.disabled = false;
  }
}

function requestTemplateDeletion(id) {
  const template = state.templates.find((item) => String(item.id) === String(id));
  openActionConfirmDialog({
    title: "删除纪要模板",
    description: `将删除“${template?.name || "此模板"}”，此操作无法撤销。`,
    onConfirm: async () => {
      await apiRequest(`/api/minute-templates/${id}`, { method: "DELETE" });
      await refreshConfigData();
      showToast("纪要模板已删除", "success");
    },
  });
}

function requestVoiceprintDeletion(id) {
  const voiceprint = state.voiceprints.find((item) => String(item.id) === String(id));
  openActionConfirmDialog({
    title: "删除声纹模型",
    description: `将删除“${voiceprint?.name || voiceprint?.speakerName || "此声纹模型"}”，此操作无法撤销。`,
    onConfirm: async () => {
      await apiRequest(`/api/voiceprints/${id}`, { method: "DELETE" });
      await refreshConfigData();
      showToast("声纹模型已删除", "success");
    },
  });
}

function requestMeetingRecordDeletion(id) {
  const meeting = state.meetings.find((item) => String(item.id) === String(id));
  openActionConfirmDialog({
    title: "删除会议记录",
    description: `将删除“${meeting?.meetingName || meeting?.fileName || "此会议记录"}”及其转写结果，此操作无法撤销。`,
    onConfirm: () => deleteMeetingRecord(id),
  });
}

async function deleteMeetingRecord(id) {
  await apiRequest(`/api/meetings/${id}`, { method: "DELETE" });
  if (String(state.currentMeetingId) === String(id)) state.currentMeetingId = "";
  await loadInitialData();
  showToast("会议记录已删除", "success");
}

async function saveManualKeywords() {
  const words = $("manualKeywordsInput").value.split(/[；;\n,]/).map((item) => item.trim()).filter(Boolean);
  await apiRequest("/api/optimization/manual-keywords", { method: "POST", body: { language: state.optimizationLanguage, words, enabled: true, applyScope: "全部会议" } });
  showToast(words.length ? "关键词已保存，将用于后续会议" : "当前语言关键词已清空", "success");
  await refreshConfigData();
}

async function parseWordListFile(inputId, targetId) {
  const file = $(inputId)?.files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  const result = await apiRequest("/api/optimization/word-files/parse", { method: "POST", body: form });
  $(targetId).value = (result.words || []).join("；");
  $(inputId).value = "";
  showToast(`已从 ${result.filename || file.name} 解析 ${(result.words || []).length} 个词，请确认后保存`, "success");
}

async function downloadWordList(path, words, filename) {
  if (!words.length) return showToast("当前没有可导出的词条", "warning");
  const blob = await apiRequest(path, { method: "POST", body: { words } });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

async function uploadOptimizationDocument() {
  const file = $("optimizationDocumentInput").files[0];
  if (!file) return showToast("请先选择文档", "warning");
  const form = new FormData();
  form.append("file", file);
  const document = await apiRequest("/api/optimization/document-keywords/files", { method: "POST", body: form });
  const result = await apiRequest("/api/optimization/document-keywords/extract", { method: "POST", body: { documentId: document.id } });
  state.pendingDocumentKeywordId = document.id;
  state.pendingDocumentKeywords = result.keywords || [];
  $("documentKeywordResult").innerHTML = state.pendingDocumentKeywords.length
    ? `<p>请勾选确认后才会进入识别配置：</p>${state.pendingDocumentKeywords.map((word) => `<label class="chip-check"><input type="checkbox" data-document-candidate="${escapeHtml(word)}" checked />${escapeHtml(word)}</label>`).join("")}`
    : `<div class="empty-state">文档未抽取出有效关键词，请检查文件内容</div>`;
  $("confirmDocumentKeywordsBtn").disabled = state.pendingDocumentKeywords.length === 0;
  showToast("文档关键词已抽取，请人工确认", "success");
}

async function confirmDocumentKeywords() {
  const keywords = Array.from(document.querySelectorAll("[data-document-candidate]:checked"))
    .map((input) => input.dataset.documentCandidate).filter(Boolean);
  if (!state.pendingDocumentKeywordId || !keywords.length) return showToast("请至少勾选一个候选关键词", "warning");
  await apiRequest(`/api/optimization/document-keywords/${state.pendingDocumentKeywordId}/confirm`, { method: "POST", body: { keywords } });
  state.pendingDocumentKeywordId = "";
  state.pendingDocumentKeywords = [];
  $("confirmDocumentKeywordsBtn").disabled = true;
  $("documentKeywordResult").textContent = "词表已确认，可在快速会议或导入转写中显式勾选";
  await refreshConfigData();
  showToast("文档词表已确认，但不会自动影响未勾选的会议", "success");
}

async function generateSmartKeywords() {
  const meetingId = state.currentMeetingId || state.meetings[0]?.id || "";
  if (!meetingId) return showToast("请先创建或选择一场会议作为生成来源", "warning");
  const result = await apiRequest("/api/optimization/smart-keywords/generate", { method: "POST", body: { meetingId, limit: 12 } });
  state.smartKeywordMeetingId = meetingId;
  state.smartKeywordCandidates = result.keywords || [];
  $("smartKeywordResult").innerHTML = state.smartKeywordCandidates.length
    ? state.smartKeywordCandidates.map((word) => `<label class="chip-check"><input type="checkbox" data-smart-candidate="${escapeHtml(word)}" checked />${escapeHtml(word)}</label>`).join("")
    : `<span class="muted">当前会议内容不足，未生成候选词</span>`;
  $("confirmSmartKeywordsBtn").disabled = state.smartKeywordCandidates.length === 0;
  showToast("智能关键词已生成，请勾选确认", "success");
}

async function confirmSmartKeywords() {
  const confirmedTerms = Array.from(document.querySelectorAll("[data-smart-candidate]:checked"))
    .map((input) => input.dataset.smartCandidate).filter(Boolean);
  if (!state.smartKeywordMeetingId || !confirmedTerms.length) return showToast("请至少勾选一个智能关键词", "warning");
  await apiRequest("/api/optimization/smart-keywords/generate", {
    method: "POST",
    body: { meetingId: state.smartKeywordMeetingId, confirmedTerms, limit: 0 },
  });
  // 智能候选同时合并到当前语言的全局词表，便于后续会议使用；当前会议的确认记录仍独立保存用于审计。
  const current = state.manualKeywords.find((item) => item.language === state.optimizationLanguage);
  const words = [...new Set([...(current?.words || []), ...confirmedTerms])];
  await apiRequest("/api/optimization/manual-keywords", {
    method: "POST",
    body: { language: state.optimizationLanguage, words, enabled: true, applyScope: "后续会议" },
  });
  $("confirmSmartKeywordsBtn").disabled = true;
  await refreshConfigData();
  showToast("智能关键词已确认，并加入后续会议可用词表", "success");
}

async function saveReplacementRule() {
  const wrongWord = $("wrongWordInput").value.trim();
  const correctWord = $("correctWordInput").value.trim();
  if (!wrongWord || !correctWord) return showToast("请填写错误词和正确词", "warning");
  const duplicate = state.replacementRules.find((item) => item.wrongWord === wrongWord && item.id !== state.replacementEditingId);
  if (duplicate) return showToast(`“${wrongWord}”已存在替换规则，请直接编辑原规则`, "warning");
  const path = state.replacementEditingId
    ? `/api/optimization/replacement-rules/${state.replacementEditingId}`
    : "/api/optimization/replacement-rules";
  await apiRequest(path, { method: state.replacementEditingId ? "PATCH" : "POST", body: { wrongWord, correctWord, enabled: true, applyScope: "后续识别" } });
  $("wrongWordInput").value = "";
  $("correctWordInput").value = "";
  state.replacementEditingId = "";
  $("saveReplacementRuleBtn").textContent = "保存并应用";
  showToast("强制替换规则已保存，将只修改最终识别文本并保留原文审计", "success");
  await refreshConfigData();
}

async function saveForbiddenWords() {
  const words = $("forbiddenWordsInput").value.split(/[；;\n,]/).map((item) => item.trim()).filter(Boolean);
  if (!words.length) return showToast("请至少填写一个禁忌词", "warning");
  const displayMode = document.querySelector("input[name='displayMode']:checked")?.value || "hide";
  const caseSensitive = $("caseSensitiveToggle").checked;
  const scope = [
    $("sensitiveScopeDisplay")?.checked ? "display" : "",
    $("sensitiveScopeAi")?.checked ? "ai" : "",
    $("sensitiveScopeExport")?.checked ? "export" : "",
  ].filter(Boolean).join(",");
  if (!scope) return showToast("请至少选择一个应用范围", "warning");
  for (const word of words) {
    const existing = state.sensitiveRules.find((item) => item.word === word && item.id !== state.sensitiveEditingId);
    const editingId = state.sensitiveEditingId || existing?.id || "";
    await apiRequest(editingId ? `/api/dictionaries/sensitive-rules/${editingId}` : "/api/dictionaries/sensitive-rules", {
      method: editingId ? "PATCH" : "POST",
      body: { word, replacement: displayMode, displayMode, enabled: true, scope, caseSensitive, language: /[a-z]/i.test(word) ? "en" : "zh", applyScope: scope },
    });
  }
  state.sensitiveEditingId = "";
  $("forbiddenWordsInput").value = "";
  $("saveForbiddenWordsBtn").textContent = "保存并应用";
  showToast("禁忌词规则已保存，原始逐字稿不会被改写", "success");
  await refreshConfigData();
}

function editReplacementRule(ruleId) {
  const rule = state.replacementRules.find((item) => String(item.id) === String(ruleId));
  if (!rule) return;
  state.replacementEditingId = rule.id;
  $("wrongWordInput").value = rule.wrongWord || "";
  $("correctWordInput").value = rule.correctWord || "";
  $("saveReplacementRuleBtn").textContent = "保存修改";
  $("wrongWordInput").focus();
}

async function toggleReplacementRule(ruleId) {
  const rule = state.replacementRules.find((item) => String(item.id) === String(ruleId));
  if (!rule) return;
  await apiRequest(`/api/optimization/replacement-rules/${rule.id}`, { method: "PATCH", body: { enabled: rule.enabled === false } });
  await refreshConfigData();
}

function requestReplacementRuleDeletion(ruleId) {
  const rule = state.replacementRules.find((item) => String(item.id) === String(ruleId));
  openActionConfirmDialog({
    title: "删除强制替换规则",
    description: `删除“${rule?.wrongWord || "该词"} → ${rule?.correctWord || ""}”后，后续识别不再执行此替换。`,
    onConfirm: async () => {
      await apiRequest(`/api/optimization/replacement-rules/${ruleId}`, { method: "DELETE" });
      await refreshConfigData();
      showToast("强制替换规则已删除", "success");
    },
  });
}

function editSensitiveRule(ruleId) {
  const rule = state.sensitiveRules.find((item) => String(item.id) === String(ruleId));
  if (!rule) return;
  state.sensitiveEditingId = rule.id;
  $("forbiddenWordsInput").value = rule.word || "";
  const mode = rule.displayMode || rule.replacement || "hide";
  const modeInput = document.querySelector(`input[name="displayMode"][value="${CSS.escape(mode)}"]`);
  if (modeInput) modeInput.checked = true;
  $("caseSensitiveToggle").checked = Boolean(rule.caseSensitive);
  const scope = String(rule.applyScope || rule.scope || "display,export").toLowerCase();
  $("sensitiveScopeDisplay").checked = scope.includes("display") || scope.includes("展示");
  $("sensitiveScopeAi").checked = scope.includes("ai");
  $("sensitiveScopeExport").checked = scope.includes("export") || scope.includes("导出");
  $("saveForbiddenWordsBtn").textContent = "保存修改";
  $("forbiddenWordsInput").focus();
}

async function toggleSensitiveRule(ruleId) {
  const rule = state.sensitiveRules.find((item) => String(item.id) === String(ruleId));
  if (!rule) return;
  await apiRequest(`/api/dictionaries/sensitive-rules/${rule.id}`, { method: "PATCH", body: { enabled: rule.enabled === false } });
  await refreshConfigData();
}

function requestSensitiveRuleDeletion(ruleId) {
  const rule = state.sensitiveRules.find((item) => String(item.id) === String(ruleId));
  openActionConfirmDialog({
    title: "删除禁忌词规则",
    description: `删除“${rule?.word || "该禁忌词"}”后，新生成的展示、AI 和导出视图将不再应用此规则。`,
    onConfirm: async () => {
      await apiRequest(`/api/dictionaries/sensitive-rules/${ruleId}`, { method: "DELETE" });
      await refreshConfigData();
      showToast("禁忌词规则已删除", "success");
    },
  });
}

async function disableAllSensitiveRules() {
  if (!state.sensitiveRules.length) {
    $("forbiddenWordsInput").value = "";
    return;
  }
  await Promise.all(state.sensitiveRules.map((rule) => apiRequest(`/api/dictionaries/sensitive-rules/${rule.id}`, {
    method: "PATCH",
    body: { enabled: false },
  })));
  $("forbiddenWordsInput").value = "";
  await refreshConfigData();
  showToast("全部禁忌词已停用，历史原文未被修改", "success");
}

async function patchMeetingSegment(segmentId) {
  // A display view can contain stars, blanks, or removed text.  It has no editable source field,
  // so this guard makes it impossible for the old save button plumbing to submit a masked value.
  if (document.querySelector(`[data-segment-display-text="${segmentId}"]`)) {
    showToast("安全展示内容不可直接保存", "warning");
  }
}

async function runDetailTool(tool) {
  const meeting = getCurrentMeeting();
  if (!state.currentMeetingId || !meeting) {
    showToast("请选择一条会议记录后再使用右侧 AI 工具", "warning");
    return;
  }
  state.activeDetailTool = tool;
  if (tool !== "mark" && !meetingHasTranscriptText(meeting)) {
    nextDetailToolRunToken();
    const panel = $("detailToolPanel");
    if (panel) panel.innerHTML = renderNoTranscriptToolPrompt(tool);
    setDetailToolRunning("", false);
    showToast("请先开始会议识别或导入音视频文件", "warning");
    return;
  }
  const markSelection = selectedTextForDetailMark();
  const actions = {
    reorganize: () => apiRequest(`/api/meetings/${state.currentMeetingId}/discourse/reorganize`, { method: "POST", body: {} }),
    summary: () => apiRequest(`/api/meetings/${state.currentMeetingId}/summaries/generate`, { method: "POST", body: {} }),
    minutes: () => apiRequest(`/api/meetings/${state.currentMeetingId}/minutes/generate`, { method: "POST", body: { templateName: state.templates.find((item) => item.isDefault)?.name || "默认会议纪要模板" } }),
    todos: () => apiRequest(`/api/meetings/${state.currentMeetingId}/todos/extract`, { method: "POST", body: {} }),
    mark: () => apiRequest(`/api/meetings/${state.currentMeetingId}/highlights`, { method: "POST", body: { text: markSelection.text || "页面重点标记", segmentId: markSelection.segmentId || "" } }),
  };
  // Replace only the minutes action after the shared action table is created. Other tools retain
  // their established API paths, while minutes now sends the chosen durable template ID.
  actions.minutes = () => apiRequest(`/api/meetings/${state.currentMeetingId}/minutes/generate`, {
    method: "POST",
    body: {
      templateId: state.minutesTemplateIds[state.currentMeetingId]
        || meeting.processingConfig?.templateId
        || null,
    },
  });
  const panel = $("detailToolPanel");
  const runToken = nextDetailToolRunToken();
  state.runningDetailTool = tool;
  setDetailToolRunning(tool, true);
  const stopProgress = panel ? startDetailToolProgressAnimation(tool, panel, runToken) : () => {};
  try {
    let result = await (actions[tool] || actions.summary)();
    if (tool === "minutes") {
      // The route response is enough for immediate display, but the history endpoint is the
      // authority for ordered versions and stale status after a transcript edit or page refresh.
      result = (await loadMinutesVersions(result.versionId)) || result;
    }
    stopProgress();
    if (panel) await streamDetailToolResult(tool, result, panel, runToken);
    cacheDetailToolDraft(tool, result);
    if (isCurrentDetailToolRun(tool, runToken)) showToast("工具执行完成", "success");
    await refreshMeetings();
  } catch (error) {
    stopProgress();
    if (panel && isCurrentDetailToolRun(tool, runToken)) {
      panel.innerHTML = `<article class="tool-result-card"><h3>工具执行失败</h3><p>${escapeHtml(error.message || "请稍后重试")}</p></article>`;
    }
    throw error;
  } finally {
    // 无论后端成功、失败还是页面刷新，都要清理按钮运行态；结果内容留在右侧面板中供用户继续查看。
    if (isCurrentDetailToolRun(tool, runToken)) {
      state.runningDetailTool = "";
      setDetailToolRunning("", false);
      syncDetailWorkbenchState();
    }
  }
}

async function loadMinutesVersions(preferredVersionId = "") {
  const meetingId = state.currentMeetingId;
  if (!meetingId) return null;
  const response = await apiRequest(`/api/meetings/${meetingId}/minutes/versions`);
  const versions = response.items || [];
  state.minutesVersions[meetingId] = versions;
  const selectedId = preferredVersionId || state.minutesVersionIds[meetingId] || response.currentVersionId || "";
  const selected = versions.find((item) => item.versionId === selectedId) || versions.at(-1) || null;
  state.minutesVersionIds[meetingId] = selected?.versionId || "";
  return selected;
}

function renderMinutesVersionControls(result) {
  const meeting = getCurrentMeeting();
  if (!meeting) return "";
  const meetingId = meeting.id;
  const versions = state.minutesVersions[meetingId] || (result.versionId ? [result] : []);
  const selectedVersionId = state.minutesVersionIds[meetingId] || result.versionId || "";
  const selectedTemplateId = state.minutesTemplateIds[meetingId]
    || result.templateId
    || meeting.processingConfig?.templateId
    || "";
  const templateOptions = state.templates.map((template) => `
    <option value="${escapeHtml(template.id)}" ${template.id === selectedTemplateId ? "selected" : ""}>${escapeHtml(template.name)}</option>
  `).join("");
  const versionOptions = versions.map((version, index) => `
    <option value="${escapeHtml(version.versionId)}" ${version.versionId === selectedVersionId ? "selected" : ""}>
      版本 ${index + 1} · ${escapeHtml(version.templateSnapshot?.name || "会议纪要模板")}
    </option>
  `).join("");
  const edited = Boolean(result.editedContent);
  // 下拉框只呈现用户能理解的模板名和版本序号。内部版本主键仍作为 option value 提交，
  // 但不在可见文本中暴露；过期提示统一由通用中文提示负责，避免重复显示审计字段。
  return `
    <section class="tool-section" data-minutes-version-controls>
      <label>纪要模板<select data-minutes-template-select>${templateOptions}</select></label>
      <label>历史版本<select data-minutes-version-select>${versionOptions}</select></label>
      <p data-minutes-version-status>${edited ? "已保留人工修改" : "AI 生成内容"}</p>
      ${result.status === "stale" ? `<span class="artifact-stale-banner" data-minutes-stale-banner>逐字稿已更新，此纪要为历史结果，请确认后重新生成。</span>` : ""}
    </section>
  `;
}

function renderDetailToolResult(tool, result = {}) {
  // 右侧工具是给会议管理员直接使用的工作区，不应该展示后端调试 JSON。
  // 这里按工具类型把相同接口结果转换成可读块：摘要看要点，纪要看正文，待办看负责人和期限。
  const titles = {
    reorganize: "语篇规整结果",
    summary: "AI 摘要",
    minutes: "会议纪要",
    todos: "待办事项",
    mark: "标记结果",
  };
  if (tool === "summary") {
    return `
      <article class="tool-result-card">
        <h3>${titles.summary}</h3>
        ${renderArtifactStaleBanner(result)}
        <p>${escapeHtml(sanitizeAiDisplayText(result.overview || result.text) || "暂无摘要内容")}</p>
        ${renderToolList("关键要点", result.keyPoints || result.highlights)}
        ${renderToolList("决策事项", result.decisionItems)}
        ${renderToolList("风险提醒", result.riskFlags)}
        ${(result.speakerSummaries || []).length ? `<section class="tool-section"><h4>按发言人总结</h4>${result.speakerSummaries.map((item) => `<div class="speaker-summary-row"><strong>${escapeHtml(item.speakerName || "未识别发言人")}</strong><p>${escapeHtml(item.summary || "")}</p>${renderSourceRangeButtons(item.sourceRanges)}</div>`).join("")}</section>` : ""}
      </article>
    `;
  }
  if (tool === "minutes") {
    // A selected historical version exposes both layers. Prefer its human edit for the visible
    // document while retaining generatedContent in the same object for provenance and comparison.
    result = {
      ...result,
      content: result.editedContent || result.content || result.generatedContent?.content || result.text || "",
    };
    return `
      <article class="tool-result-card">
        <h3>${titles.minutes}</h3>
        ${renderArtifactStaleBanner(result)}
        ${renderMinutesVersionControls(result)}
        <p>${escapeHtml(sanitizeAiDisplayText(result.content || result.text) || "暂无纪要内容")}</p>
        ${renderToolList("导出段落", (result.exportBlocks || []).map((item) => `${item.heading || "段落"}：${item.body || ""}`))}
      </article>
    `;
  }
  if (tool === "todos") {
    const items = result.items || result.todos || [];
    return `
      <article class="tool-result-card">
        <h3>${titles.todos}</h3>
        ${renderArtifactStaleBanner(result)}
        ${items.length ? items.map((item, index) => `
          <section class="tool-todo-row">
            <input data-todo-field="title" data-todo-index="${index}" value="${escapeHtml(item.title || item.taskName || item.content || "待办")}" aria-label="待办标题" />
            <input data-todo-field="owner" data-todo-index="${index}" value="${escapeHtml(item.owner || item.ownerDept || item.assignee || "")}" placeholder="负责人" aria-label="负责人" />
            <input data-todo-field="deadline" data-todo-index="${index}" value="${escapeHtml(item.deadline || item.dueDate || "")}" placeholder="截止时间" aria-label="截止时间" />
            <select data-todo-field="status" data-todo-index="${index}" aria-label="办理状态"><option value="pending" ${(item.status || "pending") === "pending" ? "selected" : ""}>待处理</option><option value="doing" ${item.status === "doing" ? "selected" : ""}>进行中</option><option value="completed" ${item.status === "completed" ? "selected" : ""}>已完成</option></select>
            <div class="row-actions"><button type="button" data-save-todo="${index}">保存</button>${renderSourceRangeButtons(item.sourceRanges)}</div>
          </section>
        `).join("") : `<p>暂无待办事项。</p>`}
        ${items.length ? `<button class="primary-button" type="button" data-push-todos>推送已保存待办</button>` : ""}
      </article>
    `;
  }
  if (tool === "reorganize") {
    return `
      <article class="tool-result-card">
        <h3>${titles.reorganize}</h3>
        ${renderArtifactStaleBanner(result)}
        <p>${escapeHtml(sanitizeAiDisplayText(result.text || result.content) || "暂无规整内容")}</p>
        ${renderToolList("章节", (result.sections || []).map((item) => `${item.title || ""} ${item.content || ""}`.trim()))}
      </article>
    `;
  }
  return `
    <article class="tool-result-card">
      <h3>${escapeHtml(titles[tool] || "工具结果")}</h3>
      <p>${escapeHtml(sanitizeAiDisplayText(result.message || result.text) || "操作已完成。")}</p>
    </article>
  `;
}

function renderToolList(title, items = []) {
  const normalized = Array.isArray(items) ? items : [];
  if (!normalized.length) return "";
  return `
    <section class="tool-section">
      <h4>${escapeHtml(title)}</h4>
      <ul>${normalized.map((item) => `<li>${escapeHtml(sanitizeAiDisplayText(typeof item === "string" ? item : item.title || item.content || item.name || item.label || ""))}</li>`).join("")}</ul>
    </section>
  `;
}

function renderSourceRangeButtons(ranges = []) {
  const source = Array.isArray(ranges) ? ranges[0] : null;
  if (!source?.segmentId) return "";
  return `<button class="source-jump-button" type="button" data-source-segment="${escapeHtml(source.segmentId)}" data-source-start-ms="${Number(source.startMs || 0)}">定位原文 ${formatTime(source.startMs || 0)}</button>`;
}

async function saveTodoFromPanel(index) {
  const body = {};
  document.querySelectorAll(`[data-todo-index="${index}"][data-todo-field]`).forEach((input) => {
    body[input.dataset.todoField] = input.value;
  });
  await apiRequest(`/api/meetings/${state.currentMeetingId}/todos/${index}`, { method: "PATCH", body });
  showToast("待办已保存", "success");
  await refreshMeetings();
}

async function pushMeetingTodos() {
  const result = await apiRequest(`/api/meetings/${state.currentMeetingId}/todos/push`, { method: "POST", body: {} });
  showToast(`已生成系统对接载荷：${result.target}`, "success");
}

async function downloadExport(kind, meetingId = state.currentMeetingId) {
  if (!meetingId) return showToast("请先选择会议", "warning");
  const path = kind === "audio" ? `/api/meetings/${meetingId}/exports/audio` : `/api/meetings/${meetingId}/exports/docx`;
  const blob = await apiRequest(path, { method: "POST", body: kind === "audio" ? {} : { exportKind: kind } });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `meeting-${kind}.${kind === "audio" ? "mp3" : "docx"}`;
  link.click();
  URL.revokeObjectURL(url);
}

async function downloadMeetingArchive() {
  const meetingIds = Array.from(state.selectedMeetingIds);
  if (!meetingIds.length) return showToast("请先勾选需要导出的会议", "warning");
  const blob = await apiRequest("/api/meetings/exports/archive", {
    method: "POST",
    body: { meetingIds, exportKind: "transcript" },
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "会议逐字稿批量导出.zip";
  link.click();
  URL.revokeObjectURL(url);
  showToast(`已打包 ${meetingIds.length} 场会议`, "success");
}

function openVoiceprintDialog(item = {}) {
  state.entityDialog = { type: "voiceprint", id: item.id || "" };
  const runtime = voiceprintRuntime();
  $("entityDialogTitle").textContent = item.id ? "编辑声纹" : "导入声纹模型";
  const voiceprintGroups = state.voiceprintGroups.filter((group) => group.id !== "vg-all");
  const groupOptions = (voiceprintGroups.length ? voiceprintGroups : [{ id: "vg-ungrouped", name: "未分组" }])
    .map((group) => `<option value="${group.id}" ${item.groupId === group.id ? "selected" : ""}>${escapeHtml(group.name)}</option>`)
    .join("");
  $("entityFormFields").innerHTML = `
    <div class="entity-dialog-grid">
      <label>姓名<input name="name" value="${escapeHtml(item.name || "")}" required placeholder="请输入发言人姓名" /></label>
      <label>部门/职位<input name="department" value="${escapeHtml(item.department || "")}" placeholder="例如：办公室" /></label>
      <label>所属分组<select name="groupId">${groupOptions}</select></label>
      <label>声纹样本音频<input name="sampleFile" type="file" accept=".wav,.mp3,.m4a,.aac,.pcm" /></label>
      <label class="full-width">备注<input name="remark" value="${escapeHtml(item.remark || "")}" placeholder="样本来源、录制场景等" /></label>
      <label class="switch-line">启用<input name="enabled" type="checkbox" ${item.enabled !== false ? "checked" : ""} /></label>
    </div>
  `;
  const sampleInput = $("entityFormFields").querySelector('[name="sampleFile"]');
  if (sampleInput) {
    // Pending metadata is still editable while the sample control follows the authoritative
    // runtime probe. This prevents the frontend from promising registration it cannot perform.
    sampleInput.disabled = !runtime.ready;
    sampleInput.title = runtime.ready ? "" : (runtime.message || "声纹运行时不可用");
  }
  const runtimeHint = document.createElement("p");
  runtimeHint.className = "modal-hint";
  runtimeHint.textContent = runtime.ready
    ? "运行时已就绪，上传样本后会请求真实 embedding 注册。"
    : `运行时不可用：${runtime.message || "等待模型配置"}。可先保存人员资料为待上传样本。`;
  $("entityFormFields").appendChild(runtimeHint);
  $("entityDialog").showModal();
}

async function submitEntityDialog(event) {
  event.preventDefault();
  const submitButton = event.currentTarget.querySelector('button[type="submit"]');
  const originalSubmitText = submitButton?.textContent || "保存";
  if (submitButton) {
    // 声纹保存可能包含“资料落库 + 样本上传/注册”两段请求，保存期间禁用按钮避免重复创建人员。
    submitButton.disabled = true;
    submitButton.textContent = "保存中...";
  }
  const form = new FormData(event.currentTarget);
  const sampleFile = form.get("sampleFile");
  const payload = {
    name: form.get("name"),
    department: form.get("department"),
    groupId: form.get("groupId") || "vg-ungrouped",
    remark: form.get("remark"),
    enabled: form.get("enabled") === "on",
    samples: state.entityDialog.id ? undefined : 0,
  };
  try {
    const endpoint = state.entityDialog.id ? `/api/voiceprints/${state.entityDialog.id}` : "/api/voiceprints";
    const saved = await apiRequest(endpoint, { method: state.entityDialog.id ? "PATCH" : "POST", body: payload });
    let sampleUploadFailed = false;
    if (sampleFile instanceof File && sampleFile.size) {
      try {
        const uploadForm = new FormData();
        uploadForm.append("file", sampleFile);
        await apiRequest(`/api/voiceprints/${saved.id}/samples`, { method: "POST", body: uploadForm });
      } catch (error) {
        // 人员资料已经保存成功时，样本上传失败只影响注册状态，不应该让用户误以为整条声纹资料丢失。
        sampleUploadFailed = true;
        showToast(`声纹资料已保存，但样本上传失败：${error.message || "请稍后重试"}`, "warning");
      }
    }
    $("entityDialog").close();
    if (!sampleUploadFailed) showToast("声纹资料已保存", "success");
    await refreshConfigData();
  } finally {
    if (submitButton) {
      submitButton.disabled = false;
      submitButton.textContent = originalSubmitText;
    }
  }
}

function selectedKeywordLibraryIds(hostId) {
  return $$(`#${hostId} [data-hotword-pick]:checked`).map((input) => input.dataset.hotwordPick);
}

function getCurrentMeeting(options = {}) {
  const exact = state.meetings.find((item) => item.id === state.currentMeetingId);
  if (exact) return exact;
  // Detail pages must be strict: if the selected id is gone, showing the first realtime/import record would
  // mix unrelated transcript, AI draft and speaker data. List pages can still opt into the old fallback.
  if (options.allowFallback || state.importView !== "detail") {
    return meetingRecords()[0] || state.meetings[0];
  }
  return null;
}

async function syncRealtimeMeetingFromServer({ render = false } = {}) {
  // WebSocket transcript messages are the fast path, but the backend is the source of truth: it writes every
  // recognized realtime segment before sending the message back to the browser. If a browser misses a message
  // during a repaint, permission prompt, or temporary connection hiccup, polling the current meeting prevents
  // the user from seeing an empty editor while the server already has text saved.
  if (!state.currentMeetingId || state.detailMode === "import" || state.realtimeSyncing) return;
  const meetingId = state.currentMeetingId;
  state.realtimeSyncing = true;
  try {
    const latest = await apiRequest(`/api/meetings/${meetingId}`);
    if (!latest?.id || latest.id !== state.currentMeetingId) return;
    const previous = state.meetings.find((item) => item.id === latest.id);
    if (state.realtimeSessionToken) {
      // 轮询是 WebSocket 的兜底通道，也必须做会话隔离。保留“轮询前已经在页面里的历史片段”，
      // 同时只接受当前 sessionToken 新增的实时片段，避免旧连接晚落库后又被轮询拉回编辑区。
      const existingIds = new Set((previous?.segments || []).map((segment) => segment.id));
      latest.segments = (latest.segments || []).filter((segment) => (
        existingIds.has(segment.id) ||
        !segment.realtimeSessionToken ||
        segment.realtimeSessionToken === state.realtimeSessionToken
      ));
    }
    const previousFingerprint = segmentFingerprint(previous);
    const nextFingerprint = segmentFingerprint(latest);
    state.meetings = previous
      ? state.meetings.map((item) => (item.id === latest.id ? { ...item, ...latest } : item))
      : [latest, ...state.meetings];
    state.realtimeLastServerSyncAt = Date.now();
    if (previousFingerprint !== nextFingerprint && meetingHasTranscriptText(latest)) {
      state.realtimeLastTranscriptAt = Date.now();
      state.realtimeInFlightChunks = 0;
      setRealtimeStatus(state.realtimeRunning ? "transcribing" : state.realtimeStatus, { source: "server_sync" });
      if (render) renderMeetingDetailWorkspace();
    } else if (render && state.realtimeRunning) {
      updateRealtimeStatusInline();
    }
  } catch {
    // Polling is only a safety net. A transient refresh failure should not interrupt microphone capture or
    // replace the more useful WebSocket/ASR error messages already shown elsewhere in the realtime flow.
  } finally {
    state.realtimeSyncing = false;
  }
}

function startRealtimeServerSync() {
  // Keep this timer separate from audio endpointing. Audio flush runs every 500ms for responsiveness; server
  // sync is intentionally slower and only reconciles persisted meeting state back into the editor.
  if (state.realtimeSyncTimer) window.clearInterval(state.realtimeSyncTimer);
  state.realtimeSyncTimer = window.setInterval(() => {
    syncRealtimeMeetingFromServer({ render: true });
  }, REALTIME_SERVER_SYNC_MS);
}

function stopRealtimeServerSync() {
  if (state.realtimeSyncTimer) window.clearInterval(state.realtimeSyncTimer);
  state.realtimeSyncTimer = null;
  state.realtimeSyncing = false;
}

function realtimeBufferedDurationMs() {
  const totalLength = state.realtimeAudioBuffers.reduce((sum, buffer) => sum + buffer.length, 0);
  return Math.round((totalLength / Math.max(1, state.realtimeAudioSampleRate)) * 1000);
}

function currentRealtimeTimelineBaseMs(meeting = getCurrentMeeting()) {
  // Realtime recording can be paused and restarted many times in one meeting. Browser capture time starts
  // from zero for every new AudioContext, but the transcript editor is a single meeting timeline. Continue
  // from the largest existing segment end so resumed chunks become new rows after the previous transcript
  // instead of returning to 00:00 and visually colliding with the first row.
  const segments = meeting?.segments || [];
  return Math.max(0, ...segments.map((segment) => Number(segment.endMs || segment.startMs || 0)));
}

function trimRealtimeSilenceBuffer(maxDurationMs = REALTIME_IDLE_BUFFER_MS) {
  // When no speech has started, keeping every silent frame makes the next utterance carry old overlap and a long
  // silent tail into ASR. Keep only a tiny pre-roll so speech onset is protected without polluting the chunk.
  const sampleRate = Math.max(1, state.realtimeAudioSampleRate);
  const maxSamples = Math.max(0, Math.round((maxDurationMs / 1000) * sampleRate));
  const totalLength = state.realtimeAudioBuffers.reduce((sum, buffer) => sum + buffer.length, 0);
  if (totalLength <= maxSamples) return;
  const merged = mergeRealtimeAudioBuffers();
  state.realtimeAudioBuffers = maxSamples ? [merged.slice(Math.max(0, merged.length - maxSamples))] : [];
}

function shouldFlushRealtimeSegment(audioState = {}) {
  const silenceEndMs = audioState.silenceEndMs ?? REALTIME_SILENCE_END_MS;
  const sentenceEndSilenceMs = audioState.sentenceEndSilenceMs ?? REALTIME_SENTENCE_END_SILENCE_MS;
  const maxSegmentMs = audioState.maxSegmentMs ?? REALTIME_MAX_SEGMENT_MS;
  const elapsedMs = Math.max(0, audioState.elapsedMs || 0);
  const silenceMs = Math.max(0, audioState.silenceMs || 0);
  if (audioState.force && audioState.hasSpeech) return { flush: true, reason: "stop" };
  if (!audioState.hasSpeech) return { flush: false, reason: "" };
  // Balanced endpointing follows common ASR practice: VAD/silence decides natural utterance boundaries,
  // while the maximum segment length only prevents a long monologue from growing into an oversized request.
  if (elapsedMs >= maxSegmentMs) return { flush: true, reason: "max_segment" };
  if (audioState.looksSentenceEnd && silenceMs >= sentenceEndSilenceMs) return { flush: true, reason: "sentence_end" };
  if (silenceMs >= silenceEndMs) return { flush: true, reason: "silence_end" };
  return { flush: false, reason: "" };
}

function shouldSendStableRealtimeSegment({ durationMs = 0, speechMs = 0, force = false, reason = "" } = {}) {
  // “flush”只是说明声学端点到了，不等于这段音频已经适合写入正文。
  // 真实产品通常会把很短的停顿当作临时状态继续等待；否则同步 ASR 会用 1 秒左右的
  // 半句话猜词，产生“嗯嗯、对、那个”这种碎片。这里要求累计人声和上下文窗口都达标，
  // 只有最大时长兜底或用户结束时才允许在略短窗口下尝试提交。
  if (speechMs >= REALTIME_MIN_FINAL_SPEECH_MS && durationMs >= REALTIME_MIN_FINAL_SEGMENT_MS) return true;
  if ((force || reason === "max_segment") && speechMs >= REALTIME_MIN_FINAL_SPEECH_MS) return true;
  return false;
}

function flushRealtimeAudioChunk({ reason = "manual", force = false } = {}, socket = state.realtimeSocket) {
  // Send WAV only when endpointing says the current utterance is complete. The metadata frame immediately
  // before the WAV lets the backend store true start/end times instead of estimating from chunk_index.
  if (!socket || socket.readyState !== WebSocket.OPEN || !state.realtimeAudioBuffers.length) return;
  if (!force && realtimeBufferedDurationMs() < REALTIME_MIN_CHUNK_MS) return;
  const samples = mergeRealtimeAudioBuffers();
  if (!samples.length) return;
  const quality = analyzeRealtimeAudioQuality(samples, state.realtimeAudioSampleRate);
  if (!quality.hasSpeechLikeAudio && !state.realtimeSpeechStarted) {
    if (reason !== "stop") notifyRealtimeChunkSkipped("当前音频分片音量过低，已跳过实时转写", quality);
    state.realtimeSegmentStartMs = state.realtimeCapturedMs;
    state.realtimeLastVoiceMs = state.realtimeCapturedMs;
    return;
  }
  const endMs = Math.max(state.realtimeCapturedMs, state.realtimeSegmentStartMs + quality.durationMs);
  const startMs = Math.max(0, state.realtimeSegmentStartMs);
  const durationMs = Math.max(0, endMs - startMs);
  const speechMs = Math.max(0, state.realtimeSpeechAccumulatedMs || 0);
  if (!shouldSendStableRealtimeSegment({ durationMs, speechMs, force, reason })) {
    // 声学端点已经出现，但上下文还太短；保留缓冲区继续积累，而不是把短碎片送去 ASR。
    // UI 只显示“正在采集/积累上下文”，正文区不会新增一条低质量片段。
    setRealtimeStatus("listening", { code: "collecting_context", durationMs, speechMs, reason });
    return;
  }
  state.realtimePendingChunkMeta = {
    type: "realtime_chunk",
    startMs,
    endMs,
    reason,
    overlapMs: REALTIME_OVERLAP_MS,
    speechMs,
    contextText: realtimeTranscriptContextText(),
    sessionToken: state.realtimeSessionToken,
  };
  socket.send(JSON.stringify(state.realtimePendingChunkMeta));
  // From this point the browser has handed a real WAV chunk to the backend. Until a transcript/status/error
  // comes back, editor empty states should assume ASR is still working instead of blaming the microphone.
  state.realtimeInFlightChunks += 1;
  setRealtimeStatus("transcribing", { ...quality, reason });
  socket.send(encodeWavFromSamples(samples, state.realtimeAudioSampleRate));
  state.realtimeChunkIndex += 1;

  // Keep 300ms of audio at the next segment head. That overlap reduces clipped boundary words when the
  // local energy gate lands near a plosive, breath, or very short pause.
  const overlapSamples = Math.min(samples.length, Math.round((REALTIME_OVERLAP_MS / 1000) * state.realtimeAudioSampleRate));
  state.realtimeAudioBuffers = overlapSamples ? [samples.slice(samples.length - overlapSamples)] : [];
  state.realtimeSegmentStartMs = Math.max(0, endMs - REALTIME_OVERLAP_MS);
  state.realtimeLastVoiceMs = state.realtimeSegmentStartMs;
  state.realtimeSpeechStarted = false;
  state.realtimeSpeechAccumulatedMs = 0;
}

function resampleRealtimeFrame(samples, sourceRate, targetRate = REALTIME_STREAM_SAMPLE_RATE) {
  // Browser AudioContext commonly runs at 48kHz, while realtime ASR services are usually tuned for 16kHz
  // mono PCM. Sending the browser rate as-is can make speech sound too fast/slow or force provider-side
  // resampling on tiny frames. This lightweight linear resampler keeps the wire format predictable.
  if (!samples.length || sourceRate === targetRate) return samples;
  const ratio = sourceRate / targetRate;
  const nextLength = Math.max(1, Math.round(samples.length / ratio));
  const output = new Float32Array(nextLength);
  for (let index = 0; index < nextLength; index += 1) {
    const sourceIndex = index * ratio;
    const leftIndex = Math.floor(sourceIndex);
    const rightIndex = Math.min(samples.length - 1, leftIndex + 1);
    const weight = sourceIndex - leftIndex;
    output[index] = samples[leftIndex] * (1 - weight) + samples[rightIndex] * weight;
  }
  return output;
}

function encodePcm16FromSamples(samples) {
  // DashScope realtime expects little-endian signed 16-bit PCM frames. The Web Audio API gives Float32 samples
  // in [-1, 1], so each frame can be converted directly without waiting for WAV headers or a full utterance.
  // This is the main latency improvement: audio leaves the browser every processor tick instead of after a
  // silence endpoint and a WAV assembly step.
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return buffer;
}

async function startMicrophoneCapture(socket, sessionToken) {
  // Realtime capture is independent from import transcription: it streams microphone PCM, detects utterance
  // endpoints locally, then ships only complete speech windows to the existing backend ASR gateway.
  if (!navigator.mediaDevices?.getUserMedia) throw new Error("当前浏览器不支持麦克风采集");
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) throw new Error("当前浏览器不支持实时音频处理");
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  let audioContext;
  try {
    // Ask the browser audio engine to perform device-rate -> 16kHz conversion. Browser-native resamplers are
    // band-limited and preserve consonants much better than repeatedly applying our lightweight linear fallback
    // to tiny 48kHz frames. ``interactive`` also asks for the lowest practical capture latency.
    audioContext = new AudioContextClass({ sampleRate: REALTIME_STREAM_SAMPLE_RATE, latencyHint: "interactive" });
  } catch {
    // Older WebKit builds may reject constructor options. Their native rate remains supported by the explicit
    // fallback resampler below, so microphone capture still works instead of failing the whole meeting.
    audioContext = new AudioContextClass();
  }
  const source = audioContext.createMediaStreamSource(stream);
  let processor = null;
  if (audioContext.state === "suspended") {
    // 某些浏览器会在异步 getUserMedia 之后把新建的 AudioContext 继续挂起；
    // 如果不主动 resume，ScriptProcessor 的 onaudioprocess 不会稳定触发，页面就会显示“识别中”但永远没有音频帧。
    await audioContext.resume();
  }
  if (!isActiveRealtimeConnection(socket, sessionToken)) {
    // getUserMedia 与 AudioContext.resume 都可能等待用户授权。等待期间若会话已结束，立即释放
    // 刚取得的设备资源，绝不能再把它们挂到后来启动的新会话全局状态上。
    stream.getTracks().forEach((track) => track.stop());
    await audioContext.close();
    throw new Error("实时会话已失效");
  }
  state.realtimeMediaStream = stream;
  state.realtimeAudioContext = audioContext;
  state.realtimeAudioSource = source;
  state.realtimeAudioSampleRate = audioContext.sampleRate;
  state.realtimeAudioBuffers = [];
  const timelineBaseMs = currentRealtimeTimelineBaseMs();
  state.realtimeSegmentStartMs = timelineBaseMs;
  state.realtimeCapturedMs = timelineBaseMs;
  state.realtimeSpeechStarted = false;
  state.realtimeSpeechAccumulatedMs = 0;
  state.realtimeLastVoiceMs = timelineBaseMs;
  state.realtimePendingChunkMeta = null;
  state.realtimeNoiseFloorRms = 0;
  state.realtimeNoiseFloorSamples = 0;
  state.realtimeUseStreaming = true;
  socket.send(JSON.stringify({
    type: "realtime_config",
    streamingMode: REALTIME_STREAMING_MODE,
    audioFormat: "pcm16",
    sampleRate: REALTIME_STREAM_SAMPLE_RATE,
    language: "zh",
    mimeType: "audio/wav",
    chunkDurationMs: REALTIME_FLUSH_MS,
    endpointingMode: "balanced",
    silenceEndMs: REALTIME_SILENCE_END_MS,
    sentenceEndSilenceMs: REALTIME_SENTENCE_END_SILENCE_MS,
    maxSegmentMs: REALTIME_MAX_SEGMENT_MS,
    overlapMs: REALTIME_OVERLAP_MS,
    sessionToken,
  }));
  const handleRealtimeAudioFrame = (rawInput) => {
    // AudioWorklet transfers a standalone Float32Array; ScriptProcessor exposes a view backed by a reusable
    // AudioBuffer. Copy both forms so fallback buffering and quality analysis never observe mutated samples.
    if (!isActiveRealtimeConnection(socket, sessionToken)) return;
    const input = new Float32Array(rawInput);
    const frameDurationMs = Math.round((input.length / state.realtimeAudioSampleRate) * 1000);
    if (state.realtimeUseStreaming && socket.readyState === WebSocket.OPEN) {
      socket.send(encodePcm16FromSamples(resampleRealtimeFrame(input, state.realtimeAudioSampleRate)));
    }
    state.realtimeAudioBuffers.push(input);
    state.realtimeCapturedMs += frameDurationMs;
    const quality = analyzeRealtimeAudioQuality(input, state.realtimeAudioSampleRate);
    if (quality.hasSpeechLikeAudio) {
      if (!state.realtimeSpeechStarted) state.realtimeSegmentStartMs = Math.max(0, state.realtimeCapturedMs - frameDurationMs);
      state.realtimeSpeechStarted = true;
      state.realtimeSpeechAccumulatedMs += frameDurationMs;
      state.realtimeLastVoiceMs = state.realtimeCapturedMs;
      if (!state.realtimeUseStreaming && ["low_volume", "asr_empty", "idle"].includes(state.realtimeStatus)) {
        setRealtimeStatus("listening", quality, { render: true });
      } else if (!state.realtimeUseStreaming) {
        setRealtimeStatus("listening", quality);
      }
    } else if (!state.realtimeSpeechStarted) {
      trimRealtimeSilenceBuffer();
      state.realtimeSegmentStartMs = state.realtimeCapturedMs - realtimeBufferedDurationMs();
      state.realtimeLastVoiceMs = state.realtimeSegmentStartMs;
      if (!state.realtimeUseStreaming && state.realtimeCapturedMs >= REALTIME_MIN_CHUNK_MS) {
        setRealtimeStatus("low_volume", quality, { render: state.realtimeStatus !== "low_volume" });
      }
    }
    const decision = shouldFlushRealtimeSegment({
      hasSpeech: state.realtimeSpeechStarted,
      elapsedMs: state.realtimeCapturedMs - state.realtimeSegmentStartMs,
      silenceMs: state.realtimeCapturedMs - state.realtimeLastVoiceMs,
      // This lightweight browser gate cannot see ASR punctuation yet, so sentence-end uses the same acoustic
      // boundary and remains ready for a future interim-text signal without changing the backend contract.
      looksSentenceEnd: false,
      silenceEndMs: REALTIME_SILENCE_END_MS,
      sentenceEndSilenceMs: REALTIME_SENTENCE_END_SILENCE_MS,
      maxSegmentMs: REALTIME_MAX_SEGMENT_MS,
    });
    if (!state.realtimeUseStreaming && decision.flush) flushRealtimeAudioChunk({ reason: decision.reason }, socket);
  };

  if (audioContext.audioWorklet && window.AudioWorkletNode) {
    try {
      await audioContext.audioWorklet.addModule("./realtime-audio-worklet.js?v=20260711a");
      processor = new AudioWorkletNode(audioContext, "realtime-pcm-collector", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
      });
      processor.port.onmessage = (event) => handleRealtimeAudioFrame(event.data);
      state.realtimeCaptureMode = "audio_worklet";
    } catch {
      // A restrictive Content-Security-Policy or older browser can block worklet modules. The fallback keeps
      // realtime transcription available, albeit on the main thread, instead of failing microphone startup.
      processor = audioContext.createScriptProcessor(1024, 1, 1);
      processor.onaudioprocess = (event) => handleRealtimeAudioFrame(event.inputBuffer.getChannelData(0));
      state.realtimeCaptureMode = "script_processor";
    }
  } else {
    // At 16kHz a 1024-sample frame is 64ms, close to commercial streaming ASR frame sizes. The former 4096
    // frame became 256ms and added visible first-text latency before network/model processing.
    processor = audioContext.createScriptProcessor(1024, 1, 1);
    processor.onaudioprocess = (event) => handleRealtimeAudioFrame(event.inputBuffer.getChannelData(0));
    state.realtimeCaptureMode = "script_processor";
  }
  state.realtimeAudioProcessor = processor;
  syncRealtimeCaptureMode();
  source.connect(processor);
  processor.connect(audioContext.destination);
  state.realtimeTimer = window.setInterval(() => {
    const decision = shouldFlushRealtimeSegment({
      hasSpeech: state.realtimeSpeechStarted,
      elapsedMs: state.realtimeCapturedMs - state.realtimeSegmentStartMs,
      silenceMs: state.realtimeCapturedMs - state.realtimeLastVoiceMs,
      silenceEndMs: REALTIME_SILENCE_END_MS,
      sentenceEndSilenceMs: REALTIME_SENTENCE_END_SILENCE_MS,
      maxSegmentMs: REALTIME_MAX_SEGMENT_MS,
    });
    if (!state.realtimeUseStreaming && decision.flush) flushRealtimeAudioChunk({ reason: decision.reason }, socket);
  }, 500);
}

function stopRealtimeMediaCapture() {
  // Stop/pause and socket-close use the same cleanup path. We flush once before disconnecting so a final
  // short utterance is not lost just because the user clicked pause or end immediately after speaking.
  if (state.realtimeTimer) window.clearInterval(state.realtimeTimer);
  state.realtimeTimer = null;
  if (!state.realtimeUseStreaming) flushRealtimeAudioChunk({ reason: "stop", force: true });
  if (state.realtimeRecorder && state.realtimeRecorder.state !== "inactive") state.realtimeRecorder.stop();
  state.realtimeRecorder = null;
  if (state.realtimeAudioProcessor) state.realtimeAudioProcessor.disconnect();
  if (state.realtimeAudioSource) state.realtimeAudioSource.disconnect();
  if (state.realtimeAudioContext) state.realtimeAudioContext.close();
  state.realtimeAudioProcessor = null;
  state.realtimeCaptureMode = "";
  syncRealtimeCaptureMode();
  state.realtimeAudioSource = null;
  state.realtimeAudioContext = null;
  state.realtimeAudioBuffers = [];
  state.realtimeSegmentStartMs = 0;
  state.realtimeCapturedMs = 0;
  state.realtimeSpeechStarted = false;
  state.realtimeSpeechAccumulatedMs = 0;
  state.realtimeLastVoiceMs = 0;
  state.realtimePendingChunkMeta = null;
  state.realtimeUseStreaming = false;
  if (state.realtimeMediaStream) state.realtimeMediaStream.getTracks().forEach((track) => track.stop());
  state.realtimeMediaStream = null;
}

function collectTranscriptText() {
  const meeting = getCurrentMeeting();
  return (meeting?.segments || []).map((segment) => segment.text).join("\n");
}

function realtimeTranscriptContextText(meeting = getCurrentMeeting()) {
  // Realtime ASR quality drops sharply when every 3-9 second audio chunk is recognized as if it were the
  // beginning of a brand-new conversation. We therefore send only the latest clean transcript tail as
  // context: enough for continuity, small enough to avoid leaking old/imported meeting content into the
  // current realtime request or making every WebSocket metadata frame heavy.
  const text = (meeting?.segments || [])
    .map((segment) => String(segment.text || "").trim())
    .filter(Boolean)
    .join("\n");
  return text.slice(-REALTIME_CONTEXT_TAIL_CHARS);
}

function minutesStatusText(status) {
  return ({ ready: "可生成纪要", generating: "生成中", generated: "已生成" }[status] || "可生成纪要");
}

function processStatusText(status) {
  return ({ completed: "已完成", processing: "处理中", draft: "草稿", failed: "识别失败", waiting_model_config: "待配置模型" }[status] || "处理中");
}

function voiceprintStatusText(item) {
  return ({ registered: "已注册", pending_sample: "待上传样本", waiting_model_config: "待配置模型" }[item.registerStatus] || item.modelStatus || "待注册");
}

function integrationLabel(key) {
  return ({ todoPush: "待办推送", minutesArchive: "纪要归档", transcriptExport: "文稿导出", audioReturn: "音频回传" }[key] || key);
}

function badge(text, status = "") {
  return `<span class="status-badge status-${escapeHtml(status || "normal")}">${escapeHtml(text)}</span>`;
}

function formatTime(ms) {
  const seconds = Math.floor(ms / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function renderSpeakerIdentity(segment = {}) {
  // 后端会在声纹命中时返回 speakerTitle，当前承载“部门/职位”。
  // 页面统一在导入转写和实时会议详情里展示，用户提前录入声纹后能直接看到“谁 + 什么岗位”。
  const name = segment.speakerName || "";
  const title = segment.speakerTitle || "";
  return title && name ? `${name}（${title}）` : name;
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function showToast(message, type = "info") {
  const item = document.createElement("div");
  item.className = `toast toast-${type}`;
  item.textContent = message;
  $("toastHost").appendChild(item);
  setTimeout(() => item.remove(), 3000);
}

function bindEvents() {
  document.addEventListener("click", async (event) => {
    try {
      const target = event.target.closest("button, article, input");
    if (!target) return;
    if (target.dataset.route) {
      if (target.dataset.route === "import") state.importView = "ledger";
      routeTo(target.dataset.route);
    }
    if (target.dataset.openMeeting) openMeetingDetail(target.dataset.openMeeting);
    if (target.dataset.openImport) openImportDetail(target.dataset.openImport);
    if (target.dataset.renameSpeaker) openSpeakerRenameDialog(target.dataset.renameSpeaker, target.dataset.speakerTitle || "");
    if (target.dataset.voiceprintGroup) { state.selectedVoiceprintGroupId = target.dataset.voiceprintGroup; renderVoiceprintManager(); }
    if (target.dataset.editVoiceprintGroup) {
      openVoiceprintGroupDialog(state.voiceprintGroups.find((item) => item.id === target.dataset.editVoiceprintGroup));
    }
    if (target.dataset.templateSource) { state.templateSource = target.dataset.templateSource; renderTemplateCenter(); }
    if (target.dataset.optTab) { state.optimizationTab = target.dataset.optTab; renderOptimizationCenter(); }
    if (target.dataset.language) { state.optimizationLanguage = target.dataset.language; renderOptimizationCenter(); }
    if (target.dataset.copyTemplate) await copyTemplate(target.dataset.copyTemplate);
    if (target.dataset.importTemplate !== undefined) await importTemplate();
    if (target.dataset.previewTemplate) openTemplatePreview(target.dataset.previewTemplate);
    if (target.dataset.templateTag) {
      const tag = target.dataset.templateTag;
      state.importingTemplateTags = state.importingTemplateTags.includes(tag)
        ? state.importingTemplateTags.filter((item) => item !== tag)
        : [...state.importingTemplateTags, tag];
      renderTemplateTagEditor();
      const previewText = $("templateImportPreviewBody")?.innerText || "";
      $("templateImportPreviewBody").innerHTML = renderTemplatePaper(previewText, state.importingTemplateTags);
    }
    if (target.dataset.deleteTemplate) requestTemplateDeletion(target.dataset.deleteTemplate);
    if (target.dataset.defaultTemplate) await apiRequest(`/api/minute-templates/${target.dataset.defaultTemplate}`, { method: "PATCH", body: { isDefault: true } }).then(refreshConfigData);
    if (target.dataset.deleteVoiceprint) requestVoiceprintDeletion(target.dataset.deleteVoiceprint);
    if (target.dataset.deleteRecord) requestMeetingRecordDeletion(target.dataset.deleteRecord);
    if (target.dataset.downloadRecord) await downloadExport("transcript", target.dataset.downloadRecord);
    if (target.dataset.editReplacement) editReplacementRule(target.dataset.editReplacement);
    if (target.dataset.toggleReplacement) await toggleReplacementRule(target.dataset.toggleReplacement);
    if (target.dataset.deleteReplacement) requestReplacementRuleDeletion(target.dataset.deleteReplacement);
    if (target.dataset.editSensitive) editSensitiveRule(target.dataset.editSensitive);
    if (target.dataset.toggleSensitive) await toggleSensitiveRule(target.dataset.toggleSensitive);
    if (target.dataset.deleteSensitive) requestSensitiveRuleDeletion(target.dataset.deleteSensitive);
    if (target.dataset.editVoiceprint) openVoiceprintDialog(state.voiceprints.find((item) => item.id === target.dataset.editVoiceprint));
    if (target.dataset.uploadSample) openVoiceprintDialog(state.voiceprints.find((item) => item.id === target.dataset.uploadSample));
    if (target.dataset.editSegment) await patchMeetingSegment(target.dataset.editSegment);
    if (target.dataset.detailCollapse) toggleDetailPanel(target.dataset.detailCollapse);
    if (target.dataset.detailNavigation) {
      state.detailNavigationMode = target.dataset.detailNavigation;
      renderSpeakerPanel(getCurrentMeeting() || { segments: [] });
    }
    if (target.dataset.transcriptStyle) toggleTranscriptViewStyle(target.dataset.transcriptStyle);
    if (target.dataset.detailToolRegenerate) await runDetailTool(target.dataset.detailToolRegenerate);
    if (target.dataset.detailToolCopy) await copyDetailToolResult(target.dataset.detailToolCopy);
    if (target.dataset.detailToolSave) await saveDetailToolResult(target.dataset.detailToolSave);
    if (target.dataset.detailToolApplyMinutes) await applyDetailToolResultToMinutes(target.dataset.detailToolApplyMinutes);
    if (target.dataset.saveTodo !== undefined) await saveTodoFromPanel(Number(target.dataset.saveTodo));
    if (target.dataset.pushTodos !== undefined) await pushMeetingTodos();
    if (target.dataset.sourceSegment || target.dataset.minutesSourceSegment) {
      // Both legacy Task 6 and new generic provenance buttons resolve through the same durable
      // source jump. This avoids two subtly different navigation paths for historical minutes.
      scrollToSourceSegment(
        target.dataset.sourceSegment || target.dataset.minutesSourceSegment,
        target.dataset.sourceStartMs || 0,
      );
    }
    if (target.dataset.seekSegment) {
      scrollToSourceSegment(target.dataset.seekSegment, target.dataset.seekMs || 0);
    }
    if (target.dataset.detailTool) openDetailTool(target.dataset.detailTool);
    if (target.dataset.download) await downloadExport(target.dataset.download);
    if (target.dataset.closeDialog) {
      $(target.dataset.closeDialog)?.close();
      if (target.dataset.closeDialog === "confirmActionDialog") state.pendingActionConfirmation = null;
    }
    if (target.id === "openCreateMeetingBtn" || target.id === "quickMeetingBtn") openQuickMeetingDialog();
    if (target.id === "addSpeakerFromDetailBtn") openAssignSpeakerDialog();
    if (target.id === "addVoiceprintBtn") openVoiceprintDialog();
    if (target.id === "batchDeleteVoiceprintBtn") await batchDeleteVoiceprints();
    if (target.id === "batchDownloadVoiceprintBtn") await batchDownloadVoiceprints();
    if (target.id === "confirmActionConfirmBtn") await confirmPendingAction();
    if (target.id === "addVoiceprintGroupBtn") openVoiceprintGroupDialog();
    if (target.id === "deleteVoiceprintGroupBtn") requestVoiceprintGroupDeletion();
    if (target.id === "clearCreateMeetingAttachmentBtn") {
      event.preventDefault();
      state.createMeetingAttachmentFile = null;
      $("createMeetingAttachment").value = "";
      $("createMeetingAttachmentName").textContent = "未选择文件";
      target.hidden = true;
    }
    if (target.id === "saveManualKeywordsBtn") await saveManualKeywords();
    if (target.id === "clearManualKeywordsBtn") { $("manualKeywordsInput").value = ""; await saveManualKeywords(); }
    if (target.id === "importManualKeywordsBtn") $("manualKeywordsFileInput")?.click();
    if (target.id === "downloadKeywordsWordBtn") {
      const words = $("manualKeywordsInput").value.split(/[；;\n,]/).map((item) => item.trim()).filter(Boolean);
      await downloadWordList("/api/optimization/manual-keywords/export", words, "识别优化关键词.docx");
    }
    if (target.id === "uploadOptimizationDocumentBtn") await uploadOptimizationDocument();
    if (target.id === "confirmDocumentKeywordsBtn") await confirmDocumentKeywords();
    if (target.id === "generateSmartKeywordsBtn") await generateSmartKeywords();
    if (target.id === "confirmSmartKeywordsBtn") await confirmSmartKeywords();
    if (target.id === "saveReplacementRuleBtn") await saveReplacementRule();
    if (target.id === "saveForbiddenWordsBtn") await saveForbiddenWords();
    if (target.id === "clearForbiddenBtn") await disableAllSensitiveRules();
    if (target.id === "importForbiddenBtn") $("forbiddenWordsFileInput")?.click();
    if (target.id === "downloadForbiddenWordBtn") {
      const words = state.sensitiveRules.map((item) => item.word).filter(Boolean);
      await downloadWordList("/api/dictionaries/sensitive-rules/export", words, "禁忌词词表.docx");
    }
    if (target.id === "editResultBtn") startTranscriptEditing();
    if (target.id === "saveTranscriptBtn") await saveTranscriptEdits();
    if (target.id === "cancelTranscriptEditBtn") cancelTranscriptEditing();
    if (target.id === "undoTranscriptBtn") undoTranscriptEdit();
    if (target.id === "redoTranscriptBtn") redoTranscriptEdit();
    if (target.id === "parseTemplateFileBtn") await parseTemplateFile();
    if (target.id === "saveImportedTemplateBtn") await saveImportedTemplate();
    if (target.id === "backToImportLedgerBtn") {
      // 详情页同时服务“实时会议”和“导入转写”，返回目标必须跟随入口语义；
      // 不能只看 audioSource，否则从会议列表打开的异常/历史数据可能被带回导入台账。
      if (state.detailMode === "realtime") routeTo("records");
      else backToImportLedger();
    }
    if (target.id === "startImportBtn") await startImport();
    if (target.id === "startRealtimeMeetingBtn") await startRealtimeTranscription();
    if (target.id === "endMeetingBtn") await stopRealtimeTranscription(true);
    if (target.id === "realtimePlayBtn") {
      // Completed meetings may expose a real recording in the same compact transport. Prefer that
      // playback path when present; otherwise this remains the icon-only live recognition toggle the
      // realtime meeting flow has always used.
      const handledAsPlayback = await toggleDetailMediaPlayback();
      if (!handledAsPlayback) {
        if (state.detailMode === "import" && hasMeetingRecordedMedia()) {
          showToast("录音正在加载，请稍候", "info");
          return;
        }
        if (state.realtimeRunning) await stopRealtimeTranscription(false);
        else await startRealtimeTranscription();
      }
    }
    if (target.id === "rewindAudioBtn" || target.id === "forwardAudioBtn") {
      const media = $("detailMediaElement");
      if (!(media instanceof HTMLMediaElement) || !media.getAttribute("src")) showToast("当前会议没有可播放录音", "warning");
      else {
        // 用户主动前后跳转也属于音字联动操作；即使当前暂停，也要定位并高亮目标片段。
        state.playbackInteractionStarted = true;
        media.currentTime = Math.max(0, Math.min(media.duration || Infinity, media.currentTime + (target.id === "rewindAudioBtn" ? -5 : 5)));
        syncPlaybackActiveSegment();
      }
    }
    if (target.id === "playbackRateBtn") {
      const media = $("detailMediaElement");
      if (!(media instanceof HTMLMediaElement) || !media.getAttribute("src")) showToast("当前会议没有可播放录音", "warning");
      else {
        const rates = [1, 1.25, 1.5, 2];
        const currentIndex = rates.findIndex((rate) => Math.abs(rate - media.playbackRate) < 0.01);
        media.playbackRate = rates[(currentIndex + 1) % rates.length];
        target.textContent = `${media.playbackRate.toFixed(media.playbackRate === 1 ? 1 : 2).replace(/0$/, "")}x`;
      }
    }
    if (target.id === "downloadToggleBtn") $("downloadMenu").classList.toggle("open");
    if (target.id === "batchExportBtn") await downloadMeetingArchive();
    } catch (error) {
      showToast(error.message || "操作失败，请稍后重试", "error");
    }
  });

  document.addEventListener("selectionchange", () => updateDetailSelectedText());
  document.addEventListener("mouseup", (event) => {
    if ($("transcriptEditor")?.contains(event.target)) updateDetailSelectedText(event);
  });
  document.addEventListener("keyup", (event) => {
    if ($("transcriptEditor")?.contains(event.target)) updateDetailSelectedText(event);
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target.id === "transcriptFontFamily") {
      state.transcriptViewStyle.fontFamily = target.value;
      applyTranscriptViewStyle();
      return;
    }
    if (target.id === "transcriptFontSize") {
      state.transcriptViewStyle.fontSize = Number(target.value) || 18;
      applyTranscriptViewStyle();
      return;
    }
    if (target.dataset?.selectTranscriptSegment) {
      if (target.checked) state.selectedTranscriptSegmentIds.add(target.dataset.selectTranscriptSegment);
      else state.selectedTranscriptSegmentIds.delete(target.dataset.selectTranscriptSegment);
      renderMeetingDetailWorkspace();
      return;
    }
    if (target.dataset?.recordSelect) {
      if (target.checked) state.selectedMeetingIds.add(target.dataset.recordSelect);
      else state.selectedMeetingIds.delete(target.dataset.recordSelect);
      if ($("batchExportBtn")) $("batchExportBtn").disabled = state.selectedMeetingIds.size === 0;
    }
    if (target.dataset?.minutesTemplateSelect) {
      // Switching a template is an explicit new generation. The original meeting binding remains
      // untouched on the server, and the returned version is appended rather than overwritten.
      state.minutesTemplateIds[state.currentMeetingId] = target.value;
      state.minutesVersionIds[state.currentMeetingId] = "";
      runDetailTool("minutes").catch((error) => showToast(error.message || "Minutes generation failed", "error"));
    }
    if (target.dataset?.minutesVersionSelect) {
      // Version switching is read-only navigation in the browser. It never calls the draft route,
      // so edited text on the selected or previously selected historical version remains intact.
      state.minutesVersionIds[state.currentMeetingId] = target.value;
      const version = (state.minutesVersions[state.currentMeetingId] || []).find((item) => item.versionId === target.value);
      if (version && $("detailToolPanel")) $("detailToolPanel").innerHTML = renderDetailToolResult("minutes", version);
    }
    if (target.id === "audioFileInput") {
      state.selectedFiles = Array.from(target.files || []);
      state.importFileStatuses = Object.fromEntries(state.selectedFiles.map((file) => [file.name, "待上传"]));
      // 重新选择文件代表开始一批新的导入任务，清空上一批转写结果，避免用户把历史结果误认为本次识别输出。
      state.importResults = [];
      renderImportPage();
      // 选择文件后先留在队列，允许用户确认语言、声纹库和识别优化，再显式点击“开始转写”。
    }
    if (target.id === "createMeetingAttachment") {
      state.createMeetingAttachmentFile = target.files?.[0] || null;
      $("createMeetingAttachmentName").textContent = state.createMeetingAttachmentFile
        ? `${state.createMeetingAttachmentFile.name} · ${Math.max(1, Math.round(state.createMeetingAttachmentFile.size / 1024))} KB`
        : "未选择文件";
      $("clearCreateMeetingAttachmentBtn").hidden = !state.createMeetingAttachmentFile;
    }
    if (target.id === "templateFileInput") parseTemplateFile().catch((error) => showToast(error.message, "error"));
    if (target.dataset?.voiceprintCheck) {
      if (target.checked) state.selectedVoiceprintIds.add(target.dataset.voiceprintCheck);
      else state.selectedVoiceprintIds.delete(target.dataset.voiceprintCheck);
      syncVoiceprintSelectionState(Array.from(document.querySelectorAll("[data-voiceprint-check]")).map((input) => ({ id: input.dataset.voiceprintCheck })));
    }
    if (target.id === "selectCurrentVoiceprints") {
      const visibleChecks = Array.from(document.querySelectorAll("[data-voiceprint-check]"));
      visibleChecks.forEach((input) => {
        if (target.checked) state.selectedVoiceprintIds.add(input.dataset.voiceprintCheck);
        else state.selectedVoiceprintIds.delete(input.dataset.voiceprintCheck);
        input.checked = target.checked;
      });
      syncVoiceprintSelectionState(visibleChecks.map((input) => ({ id: input.dataset.voiceprintCheck })));
    }
    if (target.dataset?.documentKeywordPick) {
      const targetSet = target.dataset.documentKeywordHost === "createDocumentKeywordPicker"
        ? state.selectedCreateDocumentIds
        : state.selectedImportDocumentIds;
      if (target.checked) targetSet.add(target.dataset.documentKeywordPick);
      else targetSet.delete(target.dataset.documentKeywordPick);
    }
    if (target.id === "manualKeywordsFileInput") {
      parseWordListFile("manualKeywordsFileInput", "manualKeywordsInput").catch((error) => showToast(error.message, "error"));
    }
    if (target.id === "forbiddenWordsFileInput") {
      parseWordListFile("forbiddenWordsFileInput", "forbiddenWordsInput").catch((error) => showToast(error.message, "error"));
    }
  });

  ["recordSearch", "statusFilter", "minutesFilter", "libraryFilter", "dateFilter"].forEach((id) => {
    $(id)?.addEventListener("input", () => refreshMeetings().catch((error) => showToast(error.message, "error")));
  });
  $("voiceprintSearch")?.addEventListener("input", renderVoiceprintManager);
  $("templateSearch")?.addEventListener("input", renderTemplateCenter);
  $("globalSearch")?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if ($("recordSearch")) $("recordSearch").value = event.currentTarget.value.trim();
    routeTo("records");
    refreshMeetings().catch((error) => showToast(error.message, "error"));
  });
  $("createMeetingForm")?.addEventListener("submit", (event) => createMeeting(event).catch((error) => showToast(error.message, "error")));
  $("entityForm")?.addEventListener("submit", (event) => submitEntityDialog(event).catch((error) => showToast(error.message, "error")));
  $("speakerRenameForm")?.addEventListener("submit", (event) => renameSpeakerAcrossSegments(event).catch((error) => showToast(error.message, "error")));
  $("voiceprintGroupForm")?.addEventListener("submit", (event) => submitVoiceprintGroup(event).catch((error) => showToast(error.message, "error")));
  $("importSearchInput")?.addEventListener("input", renderImportPage);
  $("transcriptSearch")?.addEventListener("input", (event) => {
    state.transcriptQuery = event.target.value || "";
    renderMeetingDetailWorkspace();
  });
  $("transcriptSpeakerFilter")?.addEventListener("change", (event) => {
    state.transcriptSpeakerFilter = event.target.value || "";
    renderMeetingDetailWorkspace();
  });

  // beforeinput 发生在 textarea 值变化之前，正好把“上一步”压入撤销栈；input 再同步新值。
  document.addEventListener("beforeinput", (event) => {
    if (event.target?.dataset?.transcriptEditSegment) pushTranscriptUndoSnapshot();
  });
  document.addEventListener("input", (event) => {
    const segmentId = event.target?.dataset?.transcriptEditSegment;
    if (!segmentId || !state.transcriptEditMode) return;
    state.transcriptEditDrafts[segmentId] = event.target.value;
    state.transcriptSaveStatus = transcriptEditIsDirty() ? "dirty" : "clean";
    syncTranscriptEditToolbar();
  });
  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || String(event.key).toLowerCase() !== "s" || !state.transcriptEditMode) return;
    event.preventDefault();
    saveTranscriptEdits().catch((error) => showToast(error.message || "逐字稿保存失败", "error"));
  });
}

bindEvents();
loadInitialData();
