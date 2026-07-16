import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";

// This audit uses Chromium's built-in DevTools Protocol rather than a package dependency. The project
// deliberately has no frontend runtime dependencies, while local Windows installations still provide
// Edge/Chromium for a reproducible product check in developer and CI-like workspaces.
const frontendUrl = process.env.PRODUCT_AUDIT_URL || "http://127.0.0.1:5173";
const parsedFrontendUrl = new URL(frontendUrl);
const apiBase = parsedFrontendUrl.searchParams.get("api") || "http://127.0.0.1:8001";
const outputDir = resolve("test-results/product-audit");
const debugPort = Number(process.env.PRODUCT_AUDIT_CDP_PORT || 9229);
const browserPath = process.env.PRODUCT_AUDIT_BROWSER
  || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const viewports = [
  { name: "desktop-1600x1000", width: 1600, height: 1000 },
  { name: "desktop-1366x768", width: 1366, height: 768 },
  { name: "mobile-390x844", width: 390, height: 844, mobile: true },
];
const routes = ["records", "import", "voiceprints", "hotwords", "sensitive", "templates", "integration"];

function wait(milliseconds) {
  return new Promise((resolveWait) => setTimeout(resolveWait, milliseconds));
}

async function waitForJson(url, attempts = 50) {
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return response.json();
    } catch (error) {
      lastError = error;
    }
    await wait(100);
  }
  throw new Error(`Unable to reach ${url}: ${lastError?.message || "browser did not start"}`);
}

class CdpSession {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
    socket.addEventListener("message", (event) => {
      const payload = JSON.parse(event.data);
      if (payload.id) {
        const request = this.pending.get(payload.id);
        this.pending.delete(payload.id);
        if (payload.error) request.reject(new Error(payload.error.message));
        else request.resolve(payload.result);
      } else {
        this.events.push(payload);
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolveRequest, rejectRequest) => {
      this.pending.set(id, { resolve: resolveRequest, reject: rejectRequest });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "browser evaluation failed");
    return result.result.value;
  }

  drainEvents() {
    const events = this.events;
    this.events = [];
    return events;
  }
}

async function waitForEvaluation(session, expression, attempts = 40, intervalMs = 150) {
  // Backend-backed controls are rendered after the initial document load. Poll the actual DOM
  // condition instead of relying on one fixed sleep, which is flaky on a cold SQLite/model process.
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (await session.evaluate(expression)) return true;
    await wait(intervalMs);
  }
  return false;
}

async function openPage() {
  const targets = await waitForJson(`http://127.0.0.1:${debugPort}/json/list`);
  const target = targets.find((item) => item.type === "page");
  if (!target?.webSocketDebuggerUrl) throw new Error("CDP page target was not available");
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolveOpen, rejectOpen) => {
    socket.addEventListener("open", resolveOpen, { once: true });
    socket.addEventListener("error", () => rejectOpen(new Error("CDP WebSocket connection failed")), { once: true });
  });
  const session = new CdpSession(socket);
  await session.send("Page.enable");
  await session.send("Runtime.enable");
  await session.send("Log.enable");
  await session.send("Network.enable");
  return session;
}

function inspectLayoutExpression() {
  // Ancestor/descendant boxes naturally intersect (for example, a button's text span). The audit
  // only reports intersections between independent visible controls/text nodes so it flags real UI
  // collisions without treating normal composition as an error.
  return `(() => {
    const clippedRect = (node) => {
      const raw = node.getBoundingClientRect();
      let left = raw.left;
      let right = raw.right;
      let top = raw.top;
      let bottom = raw.bottom;
      // getBoundingClientRect 返回元素裁切前的布局框。逐字稿筛选框位于 overflow:hidden
      // 的中栏内，超出部分肉眼不可见；若不与每个裁剪祖先求交，审计会把不可见区域
      // 与相邻右栏按钮误报成“可见重叠”。auto/scroll 容器同样只审计当前视口。
      for (let parent = node.parentElement; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        const rect = parent.getBoundingClientRect();
        if (['hidden', 'clip', 'auto', 'scroll'].includes(style.overflowX)) {
          left = Math.max(left, rect.left);
          right = Math.min(right, rect.right);
        }
        if (['hidden', 'clip', 'auto', 'scroll'].includes(style.overflowY)) {
          top = Math.max(top, rect.top);
          bottom = Math.min(bottom, rect.bottom);
        }
      }
      return { left, right, top, bottom, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
    };
    const visible = (node) => {
      const style = getComputedStyle(node);
      const rect = clippedRect(node);
      return style.visibility !== "hidden" && style.display !== "none" && !node.classList.contains("sr-only-input") && rect.width > 4 && rect.height > 4;
    };
    const elements = [...document.querySelectorAll("button,input,select,textarea,[role=button],h1,h2,h3,h4,p,strong")]
      .filter(visible)
      .map((node, index) => {
        const rect = clippedRect(node);
        return { node, index, tag: node.tagName, id: node.id, text: (node.innerText || node.getAttribute("aria-label") || "").trim().slice(0, 48), rect };
      });
    const overlaps = [];
    for (let left = 0; left < elements.length; left += 1) {
      for (let right = left + 1; right < elements.length; right += 1) {
        const a = elements[left];
        const b = elements[right];
        if (a.node.contains(b.node) || b.node.contains(a.node)) continue;
        const width = Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
        const height = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
        if (width > 6 && height > 6) overlaps.push({ a: a.tag + "#" + a.id + ":" + a.text, b: b.tag + "#" + b.id + ":" + b.text, width, height });
      }
    }
    return {
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      overlaps,
      // Document-level width cannot detect a clipped action column inside a nested table scroller.
      // Report operational work surfaces separately so a locally overflowing manager cannot pass
      // merely because the page itself still fits the viewport.
      internalOverflows: [...document.querySelectorAll('.group-table, .detail-workbench')]
        .filter(visible)
        // Browsers may round collapsed table borders by 2-3 CSS pixels at fractional device
        // coordinates. A 4px tolerance ignores that paint artifact while still catching a clipped
        // action column or any genuinely scrollable operational surface.
        .filter((node) => node.scrollWidth > node.clientWidth + 4)
        .map((node) => ({ selector: node.className, scrollWidth: node.scrollWidth, clientWidth: node.clientWidth })),
    };
  })()`;
}

async function auditRoute(session, route, viewport) {
  await session.send("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: Boolean(viewport.mobile),
  });
  await session.send("Page.navigate", { url: frontendUrl });
  // Every route shares the same initial API batch. Waiting for one populated meeting row prevents
  // screenshots of the temporary empty DOM while preserving a deterministic fixture requirement.
  const coreDataReady = await waitForEvaluation(session, `Boolean(document.querySelector('[data-open-meeting]'))`, 80, 150);
  assert.equal(coreDataReady, true, `${route} ${viewport.name}: core API data did not render`);
  await session.evaluate(`document.querySelector('[data-route="${route}"]').click()`);
  await wait(350);
  const layout = await session.evaluate(inspectLayoutExpression());
  const screenshot = await session.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const path = join(outputDir, `${route}-${viewport.name}.png`);
  await writeFile(path, Buffer.from(screenshot.data, "base64"));
  const events = session.drainEvents();
  const errors = events.filter((event) => event.method === "Runtime.exceptionThrown" || event.method === "Log.entryAdded")
    .map((event) => event.params?.exceptionDetails?.text || event.params?.entry?.text || "browser runtime error");
  const failedResponses = events
    .filter((event) => event.method === "Network.responseReceived" && event.params?.response?.status >= 400)
    .map((event) => `${event.params.response.status} ${event.params.response.url}`);
  assert.ok(layout.scrollWidth <= layout.clientWidth, `${route} ${viewport.name}: horizontal overflow ${layout.scrollWidth}/${layout.clientWidth}`);
  assert.deepEqual(layout.internalOverflows, [], `${route} ${viewport.name}: internal overflow ${JSON.stringify(layout.internalOverflows)}`);
  assert.deepEqual(layout.overlaps, [], `${route} ${viewport.name}: visible overlap ${JSON.stringify(layout.overlaps)}`);
  assert.deepEqual(errors, [], `${route} ${viewport.name}: console/page error ${errors.join(" | ")}; failed responses ${failedResponses.join(" | ")}`);
  assert.deepEqual(failedResponses, [], `${route} ${viewport.name}: failed responses ${failedResponses.join(" | ")}`);
  console.log(`PASS ${route} ${viewport.name} ${path}`);
}

async function auditQuickMeetingDialog(session, viewport) {
  // 图四的问题只会在二级弹窗打开后出现，普通页面路由审计看不到。这里直接沿用户入口打开
  // 快速会议，并验证必填标记、紧凑开关、滚动内容区和底部操作栏的真实几何关系。
  await session.send("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: Boolean(viewport.mobile),
  });
  await session.send("Page.navigate", { url: frontendUrl });
  const buttonReady = await waitForEvaluation(session, `Boolean(document.querySelector('#quickMeetingBtn'))`, 80, 150);
  assert.equal(buttonReady, true, `${viewport.name}: quick meeting entry did not render`);
  await session.evaluate(`document.querySelector('#quickMeetingBtn').click()`);
  const dialogReady = await waitForEvaluation(session, `document.querySelector('#createMeetingDialog')?.open === true`);
  assert.equal(dialogReady, true, `${viewport.name}: quick meeting dialog did not open`);

  const layout = await session.evaluate(`(() => {
    const dialog = document.querySelector('#createMeetingDialog');
    const scrollArea = dialog.querySelector('.quick-meeting-scroll-area');
    const footer = dialog.querySelector('.quick-meeting-footer');
    const fieldTitle = dialog.querySelector('#createMeetingTitle')?.previousElementSibling;
    const required = fieldTitle?.querySelector('.required');
    const switches = [...dialog.querySelectorAll('.switch-card input[type="checkbox"]')];
    const rect = dialog.getBoundingClientRect();
    const footerRect = footer.getBoundingClientRect();
    const titleRect = fieldTitle?.getBoundingClientRect();
    const requiredRect = required?.getBoundingClientRect();
    return {
      sectionCount: dialog.querySelectorAll('.quick-meeting-section').length,
      switchSizes: switches.map((node) => { const box = node.getBoundingClientRect(); return { width: box.width, height: box.height }; }),
      requiredAligned: Boolean(titleRect && requiredRect && Math.abs(titleRect.top - requiredRect.top) < 3),
      footerInsideDialog: footerRect.bottom <= rect.bottom + 1 && footerRect.top >= rect.top,
      footerVisible: footerRect.height > 0 && getComputedStyle(footer).display !== 'none',
      scrollAreaContained: scrollArea.scrollWidth <= scrollArea.clientWidth + 1,
      dialogInsideViewport: rect.left >= 0 && rect.right <= innerWidth + 1 && rect.top >= 0 && rect.bottom <= innerHeight + 1,
    };
  })()`);
  assert.equal(layout.sectionCount, 3, `${viewport.name}: quick meeting dialog sections are incomplete`);
  assert.equal(layout.requiredAligned, true, `${viewport.name}: required marker is separated from the meeting title label`);
  assert.ok(layout.switchSizes.every((size) => size.width <= 44 && size.height <= 26), `${viewport.name}: native oversized checkboxes remain ${JSON.stringify(layout.switchSizes)}`);
  assert.equal(layout.footerInsideDialog, true, `${viewport.name}: quick meeting footer escaped the dialog`);
  assert.equal(layout.footerVisible, true, `${viewport.name}: quick meeting footer is not visible`);
  assert.equal(layout.scrollAreaContained, true, `${viewport.name}: quick meeting form has horizontal overflow`);
  assert.equal(layout.dialogInsideViewport, true, `${viewport.name}: quick meeting dialog exceeds the viewport`);

  const screenshot = await session.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const path = join(outputDir, `quick-meeting-${viewport.name}.png`);
  await writeFile(path, Buffer.from(screenshot.data, "base64"));
  console.log(`PASS quick-meeting ${viewport.name} ${path}`);
}

async function auditMeetingDetail(session, viewport) {
  await session.send("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: Boolean(viewport.mobile),
  });
  await session.send("Page.navigate", { url: frontendUrl });
  const meetingReady = await waitForEvaluation(session, `Boolean(document.querySelector('[data-open-meeting]'))`);
  if (!meetingReady) {
    const diagnostics = await session.evaluate(`({
      href: location.href,
      serviceBanner: document.querySelector('#serviceBanner')?.textContent?.trim() || '',
      toasts: [...document.querySelectorAll('.toast')].map((node) => node.textContent.trim()),
      recordsText: document.querySelector('#recordsTableBody')?.textContent?.trim() || '',
    })`);
    assert.fail(`${viewport.name}: no meeting row was available after data loading; ${JSON.stringify(diagnostics)}`);
  }

  // The route audit above proves that each management page opens, but the transcript workbench is
  // reached through a row action and therefore needs its own product-level check. Reuse a durable
  // meeting already returned by the backend: this keeps the audit read-only while exercising the
  // exact navigation path a user takes from the meeting ledger.
  const opened = await session.evaluate(`(async () => {
    const api = new URLSearchParams(location.search).get('api') || 'http://127.0.0.1:8001';
    const response = await fetch(api + '/api/meetings');
    const payload = await response.json();
    // List ordering changes whenever smoke tests create a fresh record. Select by the capability the
    // workbench audit needs, not by row position: a realtime-owned meeting with persisted segments.
    const fixture = payload.items.find((meeting) => meeting.audioSource !== '上传文件' && (meeting.segments || []).length > 0);
    const button = fixture ? document.querySelector('[data-open-meeting="' + CSS.escape(fixture.id) + '"]') : null;
    if (!button) return false;
    button.click();
    return true;
  })()`);
  assert.equal(opened, true, `${viewport.name}: no meeting row was available for detail audit`);
  await wait(500);

  const detail = await session.evaluate(`(() => {
    const workbench = document.querySelector('.detail-workbench');
    const segments = [...document.querySelectorAll('[data-segment-id]')];
    const mergedParagraphs = [...document.querySelectorAll('.transcript-merged-text')];
    const sourceMarkers = [...document.querySelectorAll('[data-source-segment]')];
    const visible = (node) => node && getComputedStyle(node).display !== 'none' && !node.hidden;
    const editorRect = document.querySelector('.editor-panel')?.getBoundingClientRect();
    const toolRect = document.querySelector('.right-tool-dock')?.getBoundingClientRect();
    const toolbar = document.querySelector('.transcript-edit-toolbar');
    const lastStyleButton = toolbar?.querySelector('[data-transcript-style="align"]');
    const lastStyleRect = lastStyleButton?.getBoundingClientRect();
    const toolbarRect = toolbar?.getBoundingClientRect();
    return {
      workbenchVisible: visible(workbench),
      removedConfigSummaryPresent: Boolean(document.querySelector('#meetingConfigSummary')),
      segmentCount: segments.length,
      mergedParagraphCount: mergedParagraphs.length,
      mergedParagraphsWithNestedRows: mergedParagraphs.filter((node) => node.querySelector('.speech-block-paragraph')).length,
      sourceMarkerCount: sourceMarkers.length,
      // 页面刚打开且播放器尚未播放时不应高亮 00:00 的第一段；这正是截图中绿色异形块的根因。
      activeSegmentCount: document.querySelectorAll('.speech-segment.is-active').length,
      editorWidth: editorRect?.width || 0,
      toolWidth: toolRect?.width || 0,
      allStyleButtonsVisible: Boolean(lastStyleRect && toolbarRect && lastStyleRect.right <= toolbarRect.right + 1),
      realtimePlayerVisible: visible(document.querySelector('#bottomAudioPlayer')),
    };
  })()`);
  assert.equal(detail.workbenchVisible, true, `${viewport.name}: transcript workbench is not visible`);
  assert.equal(detail.removedConfigSummaryPresent, false, `${viewport.name}: removed configuration summary still occupies the detail page`);
  assert.ok(detail.segmentCount > 0, `${viewport.name}: fixture meeting did not render transcript segments`);
  assert.ok(detail.mergedParagraphCount > 0, `${viewport.name}: read-only transcript did not render merged speaker paragraphs`);
  assert.equal(detail.mergedParagraphsWithNestedRows, 0, `${viewport.name}: merged speaker paragraph still contains separated internal rows`);
  assert.equal(detail.activeSegmentCount, 0, `${viewport.name}: transcript highlighted 00:00 before any playback/source interaction`);
  if (viewport.name === "desktop-1600x1000") {
    assert.ok(detail.toolWidth >= 490, `AI rail is still too narrow on desktop: ${detail.toolWidth}px`);
    assert.equal(
      detail.allStyleButtonsVisible,
      true,
      `restored transcript style buttons still require horizontal scrolling on the common desktop viewport: ${JSON.stringify(detail)}`,
    );
  }
  assert.equal(detail.realtimePlayerVisible, true, `${viewport.name}: realtime detail lost its media controls`);

  if (!viewport.mobile) {
    // 左右栏都要真实完成“收起 -> 只剩展开入口 -> 再展开”。仅检查 class 存在无法发现
    // 截图中的残留“章节”文字和右侧按钮被工具栏 overflow 裁掉的问题。
    const collapseState = await session.evaluate(`(() => {
      const visible = (node) => {
        if (!node) return false;
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 4 && rect.height > 4;
      };
      const speakerToggle = document.querySelector('[data-detail-collapse="speaker"]');
      const toolsToggle = document.querySelector('[data-detail-collapse="tools"]');
      speakerToggle.click();
      const speakerCollapsed = {
        workbenchClass: document.querySelector('.detail-workbench').classList.contains('speaker-collapsed'),
        tabsVisible: visible(document.querySelector('.speaker-panel .panel-mode-tabs')),
        hintVisible: visible(document.querySelector('.speaker-panel .navigation-mode-hint')),
        toggleVisible: visible(speakerToggle),
      };
      speakerToggle.click();
      toolsToggle.click();
      const toolsCollapsed = {
        workbenchClass: document.querySelector('.detail-workbench').classList.contains('tools-collapsed'),
        tabsVisible: visible(document.querySelector('.right-tool-dock .tool-tab-bar')),
        toggleVisible: visible(toolsToggle),
      };
      toolsToggle.click();
      return {
        speakerCollapsed,
        toolsCollapsed,
        speakerExpanded: !document.querySelector('.detail-workbench').classList.contains('speaker-collapsed'),
        toolsExpanded: !document.querySelector('.detail-workbench').classList.contains('tools-collapsed'),
        toolTabsRestored: visible(document.querySelector('.right-tool-dock .tool-tab-bar')),
      };
    })()`);
    assert.deepEqual(collapseState.speakerCollapsed, { workbenchClass: true, tabsVisible: false, hintVisible: false, toggleVisible: true }, `${viewport.name}: left rail collapse is incomplete`);
    assert.deepEqual(collapseState.toolsCollapsed, { workbenchClass: true, tabsVisible: false, toggleVisible: true }, `${viewport.name}: right rail collapse lost its expand entry`);
    assert.equal(collapseState.speakerExpanded, true, `${viewport.name}: left rail did not expand again`);
    assert.equal(collapseState.toolsExpanded, true, `${viewport.name}: right rail did not expand again`);
    assert.equal(collapseState.toolTabsRestored, true, `${viewport.name}: right tool tabs did not restore after expand`);
  }

  // A populated transcript alone does not prove provenance is usable. Generate one real derived
  // artifact through the visible tool, wait for its canonical source buttons, then follow a source
  // back into the editor. This covers the no-route-change traceability contract end to end.
  const summaryStarted = await session.evaluate(`(() => {
    const button = document.querySelector('[data-detail-tool="summary"]');
    if (!button) return false;
    button.click();
    return true;
  })()`);
  assert.equal(summaryStarted, true, `${viewport.name}: AI summary action is not available`);
  // The configured AI provider may need roughly twenty seconds on a cold request. Keep the audit
  // timeout above that observed boundary so it measures correctness rather than provider warm-up.
  const sourcesReady = await waitForEvaluation(session, `Boolean(document.querySelector('[data-source-segment]'))`, 240, 150);
  if (!sourcesReady) {
    const diagnostics = await session.evaluate(`({
      panelText: document.querySelector('#detailToolPanel')?.textContent?.trim() || '',
      panelHtml: document.querySelector('#detailToolPanel')?.innerHTML?.slice(0, 2000) || '',
      toasts: [...document.querySelectorAll('.toast')].map((node) => node.textContent.trim()),
    })`);
    assert.fail(`${viewport.name}: generated summary did not expose source references; ${JSON.stringify(diagnostics)}`);
  }
  const sourceNavigation = await session.evaluate(`(() => {
    const source = document.querySelector('[data-source-segment]');
    const segmentId = source?.dataset.sourceSegment || '';
    source?.click();
    const segment = segmentId ? document.querySelector('[data-segment-id="' + CSS.escape(segmentId) + '"]') : null;
    return {
      segmentId,
      active: Boolean(segment?.classList.contains('is-active')),
      seekMs: document.querySelector('#bottomAudioPlayer')?.dataset.seekMs || '',
    };
  })()`);
  assert.ok(sourceNavigation.segmentId, `${viewport.name}: source reference has no segment id`);
  assert.equal(sourceNavigation.active, true, `${viewport.name}: source reference did not select its transcript segment`);
  assert.notEqual(sourceNavigation.seekMs, "", `${viewport.name}: source reference did not seek the timeline`);

  if (viewport.name === "desktop-1600x1000") {
    // Minutes are durable artifacts rather than one-shot AI text. Exercise the actual history path,
    // advance the transcript revision with an idempotent text PATCH, then prove the selected old
    // version becomes stale and can be explicitly regenerated without losing its source controls.
    // Source buttons appear at the end of the typing animation, just before refreshMeetings settles.
    // Wait for the summary action to release its busy state so that refresh cannot invalidate the
    // minutes run token immediately after the next click.
    const summarySettled = await waitForEvaluation(
      session,
      `document.querySelector('[data-detail-tool="summary"]')?.getAttribute('aria-busy') !== 'true'`,
      80,
      150,
    );
    assert.equal(summarySettled, true, "summary action did not settle before minutes audit");
    const templateSelected = await session.evaluate(`(() => {
      const templateId = state.templates?.[0]?.id || '';
      if (!templateId || !state.currentMeetingId) return false;
      state.minutesTemplateIds[state.currentMeetingId] = templateId;
      return true;
    })()`);
    assert.equal(templateSelected, true, "minutes audit could not select a durable template");
    await session.evaluate(`document.querySelector('[data-detail-tool="minutes"]').click()`);
    const minutesReady = await waitForEvaluation(session, `Boolean(document.querySelector('[data-minutes-version-controls]'))`, 240, 150);
    if (!minutesReady) {
      const diagnostics = await session.evaluate(`({
        panelText: document.querySelector('#detailToolPanel')?.textContent?.trim() || '',
        panelHtml: document.querySelector('#detailToolPanel')?.innerHTML?.slice(0, 2400) || '',
        busy: document.querySelector('[data-detail-tool="minutes"]')?.getAttribute('aria-busy') || '',
        toasts: [...document.querySelectorAll('.toast')].map((node) => node.textContent.trim()),
      })`);
      assert.fail(`minutes history/version controls did not render; ${JSON.stringify(diagnostics)}`);
    }
    const revisionAdvanced = await session.evaluate(`(async () => {
      const api = new URLSearchParams(location.search).get('api') || 'http://127.0.0.1:8001';
      const response = await fetch(api + '/api/meetings');
      const meetings = await response.json();
      const meeting = meetings.items.find((item) => (item.segments || []).some((segment) => segment.id === ${JSON.stringify(sourceNavigation.segmentId)}));
      const segment = meeting?.segments?.find((item) => item.id === ${JSON.stringify(sourceNavigation.segmentId)});
      if (!meeting || !segment) return false;
      window.__productAuditTranscriptRestore = { meetingId: meeting.id, segmentId: segment.id, text: segment.text };
      const patched = await fetch(api + '/api/meetings/' + encodeURIComponent(meeting.id) + '/segments/' + encodeURIComponent(segment.id), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: segment.text + '\\n[product-audit-revision]' }),
      });
      if (!patched.ok) return false;
      await refreshMeetings();
      openMeetingDetail(meeting.id);
      return true;
    })()`);
    assert.equal(revisionAdvanced, true, "transcript revision could not be advanced for stale audit");
    await session.evaluate(`document.querySelector('[data-detail-tool="minutes"]').click()`);
    const staleReady = await waitForEvaluation(session, `Boolean(document.querySelector('[data-minutes-stale-banner]'))`, 80, 150);
    assert.equal(staleReady, true, "historical minutes did not become stale after transcript revision changed");
    const staleScreenshot = await session.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(join(outputDir, `meeting-detail-stale-${viewport.name}.png`), Buffer.from(staleScreenshot.data, "base64"));
    const sourceRestored = await session.evaluate(`(async () => {
      const restore = window.__productAuditTranscriptRestore;
      const api = new URLSearchParams(location.search).get('api') || 'http://127.0.0.1:8001';
      if (!restore) return false;
      const response = await fetch(api + '/api/meetings/' + encodeURIComponent(restore.meetingId) + '/segments/' + encodeURIComponent(restore.segmentId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: restore.text }),
      });
      if (!response.ok) return false;
      await refreshMeetings();
      openMeetingDetail(restore.meetingId);
      state.minutesTemplateIds[restore.meetingId] = state.templates?.[0]?.id || '';
      document.querySelector('[data-detail-tool="minutes"]').click();
      return true;
    })()`);
    assert.equal(sourceRestored, true, "stale audit did not restore its temporary transcript edit");
    const restoredStaleReady = await waitForEvaluation(session, `Boolean(document.querySelector('[data-minutes-stale-banner]'))`, 80, 150);
    assert.equal(restoredStaleReady, true, "restored transcript did not retain stale history before regeneration");
    await session.evaluate(`document.querySelector('[data-detail-tool-regenerate="minutes"]').click()`);
    const regenerated = await waitForEvaluation(
      session,
      `Boolean(document.querySelector('[data-minutes-version-controls]')) && !document.querySelector('[data-minutes-stale-banner]')`,
      240,
      150,
    );
    assert.equal(regenerated, true, "stale minutes regeneration did not return a current version");
  }

  const layout = await session.evaluate(inspectLayoutExpression());
  const screenshot = await session.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const path = join(outputDir, `meeting-detail-${viewport.name}.png`);
  await writeFile(path, Buffer.from(screenshot.data, "base64"));
  const events = session.drainEvents();
  const errors = events.filter((event) => event.method === "Runtime.exceptionThrown" || event.method === "Log.entryAdded")
    .map((event) => event.params?.exceptionDetails?.text || event.params?.entry?.text || "browser runtime error");
  const failedResponses = events
    .filter((event) => event.method === "Network.responseReceived" && event.params?.response?.status >= 400)
    .map((event) => `${event.params.response.status} ${event.params.response.url}`);
  assert.ok(layout.scrollWidth <= layout.clientWidth, `meeting-detail ${viewport.name}: horizontal overflow ${layout.scrollWidth}/${layout.clientWidth}`);
  assert.deepEqual(layout.internalOverflows, [], `meeting-detail ${viewport.name}: internal overflow ${JSON.stringify(layout.internalOverflows)}`);
  assert.deepEqual(layout.overlaps, [], `meeting-detail ${viewport.name}: visible overlap ${JSON.stringify(layout.overlaps)}`);
  assert.deepEqual(errors, [], `meeting-detail ${viewport.name}: console/page error ${errors.join(" | ")}`);
  assert.deepEqual(failedResponses, [], `meeting-detail ${viewport.name}: failed responses ${failedResponses.join(" | ")}`);
  console.log(`PASS meeting-detail ${viewport.name} ${path}; source ${sourceNavigation.segmentId}`);
}

async function createPlaybackFixture() {
  // The fixture uses the public meeting/upload APIs, not a direct database write.  This exercises
  // the same content type and ownership records that an imported file uses in the product.
  const meetingResponse = await fetch(`${apiBase}/api/meetings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      meetingName: `browser playback audit ${Date.now()}`,
      audioSource: "上传文件",
      keywordLibraryIds: [],
      enableDiarization: false,
    }),
  });
  assert.equal(meetingResponse.ok, true, `playback fixture meeting failed: ${meetingResponse.status}`);
  const meeting = await meetingResponse.json();
  const wavBytes = await readFile(resolve("backend/data/audio_clips/rec-001-realtime-0.wav"));
  const form = new FormData();
  form.append("file", new Blob([wavBytes], { type: "audio/wav" }), "browser-playback.wav");
  const uploadResponse = await fetch(`${apiBase}/api/meetings/${encodeURIComponent(meeting.id)}/files`, {
    method: "POST",
    body: form,
  });
  assert.equal(uploadResponse.ok, true, `playback fixture upload failed: ${uploadResponse.status}`);
  return meeting.id;
}

async function auditImportedPlayback(session, meetingId) {
  await session.send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
  await session.send("Page.navigate", { url: frontendUrl });
  const rowReady = await waitForEvaluation(session, `Boolean(document.querySelector('[data-open-import="${meetingId}"]'))`, 80, 150);
  if (!rowReady) {
    await session.evaluate(`document.querySelector('[data-route="import"]')?.click()`);
  }
  const importReady = await waitForEvaluation(session, `Boolean(document.querySelector('[data-open-import="${meetingId}"]'))`, 40, 150);
  assert.equal(importReady, true, "temporary imported-media row did not render");
  await session.evaluate(`document.querySelector('[data-open-import="${meetingId}"]').click()`);
  const mediaReady = await waitForEvaluation(
    session,
    `(() => { const media = document.querySelector('#detailMediaElement'); return Boolean(media?.src?.startsWith('blob:') && media.readyState >= 1 && Number.isFinite(media.duration) && media.duration > 0 && !media.error); })()`,
    80,
    150,
  );
  assert.equal(mediaReady, true, "imported WAV did not reach loadedmetadata through the app Blob playback path");
  const diagnostics = await session.evaluate(`(() => { const media = document.querySelector('#detailMediaElement'); return { src: media.src, readyState: media.readyState, duration: media.duration, error: media.error?.message || '' }; })()`);
  console.log(`PASS import-playback loadedmetadata duration=${diagnostics.duration.toFixed(3)}s readyState=${diagnostics.readyState}`);
}

await mkdir(outputDir, { recursive: true });
const playbackMeetingId = await createPlaybackFixture();
const browser = spawn(browserPath, [
  "--headless=new",
  `--remote-debugging-port=${debugPort}`,
  "--remote-allow-origins=*",
  `--user-data-dir=${resolve("test-results/product-audit-profile")}`,
  "about:blank",
], { stdio: "ignore", windowsHide: true });

try {
  const session = await openPage();
  for (const viewport of viewports) {
    for (const route of routes) await auditRoute(session, route, viewport);
    await auditQuickMeetingDialog(session, viewport);
    await auditMeetingDetail(session, viewport);
  }
  await auditImportedPlayback(session, playbackMeetingId);
  session.socket.close();
} finally {
  browser.kill();
  // Cleanup uses the product delete contract, which now also removes the exact uploaded WAV.
  await fetch(`${apiBase}/api/meetings/${encodeURIComponent(playbackMeetingId)}`, { method: "DELETE" });
}
