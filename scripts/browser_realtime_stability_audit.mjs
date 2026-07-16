import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const frontendUrl = process.env.REALTIME_AUDIT_URL || "http://127.0.0.1:5173/?api=http://127.0.0.1:8001";
const apiBase = new URL(frontendUrl).searchParams.get("api") || "http://127.0.0.1:8001";
const browserPath = process.env.REALTIME_AUDIT_BROWSER || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const debugPort = Number(process.env.REALTIME_AUDIT_CDP_PORT || 9231);
const outputDir = resolve("test-results/realtime-stability-audit");

function wait(milliseconds) {
  return new Promise((resolveWait) => setTimeout(resolveWait, milliseconds));
}

async function waitForJson(url, attempts = 80) {
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
  throw new Error(`无法连接浏览器调试端口：${lastError?.message || url}`);
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
        const pending = this.pending.get(payload.id);
        this.pending.delete(payload.id);
        if (payload.error) pending.reject(new Error(payload.error.message));
        else pending.resolve(payload.result);
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
    const payload = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (payload.exceptionDetails) throw new Error(payload.exceptionDetails.text || "浏览器执行失败");
    return payload.result.value;
  }
}

async function waitForEvaluation(session, expression, attempts = 80) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (await session.evaluate(expression)) return true;
    await wait(100);
  }
  return false;
}

async function main() {
  await mkdir(outputDir, { recursive: true });
  const meetingResponse = await fetch(`${apiBase}/api/meetings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ meetingName: `实时稳定性验收 ${Date.now()}`, enableDiarization: true }),
  });
  assert.equal(meetingResponse.ok, true, `创建验收会议失败：HTTP ${meetingResponse.status}`);
  const meeting = await meetingResponse.json();

  // 使用独立用户目录避免影响用户当前 Edge 会话。目录保留为测试证据，不执行任何递归删除。
  const browser = spawn(browserPath, [
    "--headless=new",
    "--disable-gpu",
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${resolve("tmp/realtime-stability-edge-profile")}`,
    "about:blank",
  ], { stdio: "ignore", windowsHide: true });

  let socket;
  try {
    const targets = await waitForJson(`http://127.0.0.1:${debugPort}/json/list`);
    const target = targets.find((item) => item.type === "page");
    assert.ok(target?.webSocketDebuggerUrl, "未找到 Edge 页面调试目标");
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolveOpen, rejectOpen) => {
      socket.addEventListener("open", resolveOpen, { once: true });
      socket.addEventListener("error", () => rejectOpen(new Error("CDP WebSocket 连接失败")), { once: true });
    });
    const session = new CdpSession(socket);
    await session.send("Page.enable");
    await session.send("Runtime.enable");
    await session.send("Log.enable");
    await session.send("Network.enable");
    // 验收必须读取当前工作区资源，不能让持久化测试 profile 的旧 CSS 掩盖或伪造布局结果。
    await session.send("Network.setCacheDisabled", { cacheDisabled: true });
    await session.send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
    await session.send("Page.navigate", { url: frontendUrl });

    const rowReady = await waitForEvaluation(session, `Boolean(document.querySelector('[data-open-meeting="${meeting.id}"]'))`);
    assert.equal(rowReady, true, "新建实时会议没有出现在会议台账");
    await session.evaluate(`document.querySelector('[data-open-meeting="${meeting.id}"]').click()`);
    const detailReady = await waitForEvaluation(session, `Boolean(document.querySelector('.detail-workbench:not([hidden])'))`);
    assert.equal(detailReady, true, "实时会议详情工作台未显示");

    const layout = await session.evaluate(`(() => {
      const editor = document.querySelector('.editor-panel');
      const toolbar = document.querySelector('.rich-toolbar');
      const transcript = document.querySelector('#transcriptEditor');
      const workbench = document.querySelector('#transcriptWorkbenchBar');
      const filter = document.querySelector('#transcriptSpeakerFilter');
      const player = document.querySelector('#bottomAudioPlayer');
      const editorRect = editor.getBoundingClientRect();
      const toolbarRect = toolbar.getBoundingClientRect();
      const transcriptRect = transcript.getBoundingClientRect();
      return {
        workbenchHidden: workbench.hidden || getComputedStyle(workbench).display === 'none',
        filterHidden: filter.hidden || getComputedStyle(filter).display === 'none',
        toolbarBeforeTranscript: toolbarRect.top < transcriptRect.top,
        toolbarNearTop: toolbarRect.top - editorRect.top < 90,
        playerHeight: player.getBoundingClientRect().height,
        transcriptHeight: transcriptRect.height,
        emptyText: transcript.textContent.trim(),
      };
    })()`);
    assert.equal(layout.workbenchHidden, true, "空转写时不应显示搜索/筛选栏");
    assert.equal(layout.filterHidden, true, "不足两名发言人时不应显示发言人筛选");
    assert.equal(layout.toolbarBeforeTranscript, true, "编辑工具栏没有位于正文之前");
    assert.equal(layout.toolbarNearTop, true, "空会议工具栏仍漂浮在编辑器中部");
    assert.ok(layout.playerHeight < 100, `播放器错误占据正文弹性行：${layout.playerHeight}px`);
    assert.ok(layout.transcriptHeight > 300, `空会议正文区域高度异常：${layout.transcriptHeight}px`);

    const aiText = await session.evaluate(`(() => {
      const result = {
        content: '会议纪要 Revision 109 rt-rec-deadbeef-0',
        sourceTranscriptRevision: 109,
        sourceSegmentIds: ['rt-rec-deadbeef-0'],
        sourceRanges: [{ segmentId: 'rt-rec-deadbeef-0', startMs: 0, endMs: 1000 }],
      };
      const host = document.createElement('div');
      host.innerHTML = renderDetailToolResult('minutes', result);
      return host.textContent;
    })()`);
    assert.doesNotMatch(aiText, /Revision\s+109/i, "AI 结果仍显示内部 revision");
    assert.doesNotMatch(aiText, /rt-rec-/i, "AI 结果仍显示内部 segment id");

    const screenshot = await session.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    const screenshotPath = resolve(outputDir, "empty-realtime-detail-1600x1000.png");
    await writeFile(screenshotPath, Buffer.from(screenshot.data, "base64"));

    const runtimeErrors = session.events
      .filter((event) => event.method === "Runtime.exceptionThrown" || event.method === "Log.entryAdded")
      .map((event) => event.params?.exceptionDetails?.text || event.params?.entry?.text || "浏览器运行错误");
    assert.deepEqual(runtimeErrors, [], `浏览器出现异常：${runtimeErrors.join(" | ")}`);
    console.log(`REALTIME BROWSER AUDIT OK ${screenshotPath}`);
  } finally {
    socket?.close();
    browser.kill();
    // 测试会议通过公开 API 精确删除，不触碰用户会议，也不执行文件或目录批量清理。
    await fetch(`${apiBase}/api/meetings/${encodeURIComponent(meeting.id)}`, { method: "DELETE" });
  }
}

await main();
