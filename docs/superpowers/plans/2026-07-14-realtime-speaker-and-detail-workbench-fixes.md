# Realtime Speaker and Detail Workbench Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop realtime meeting-title hallucinations, assign stable anonymous or voiceprint-backed speaker identities, remove the debug strip, localize transcript controls, and keep all five AI tools on one stable row.

**Architecture:** Keep DashScope native streaming as the low-latency text path. Add a bounded PCM timeline and a meeting-scoped speaker tracker that extracts each final utterance, matches registered voiceprints first, then clusters unknown CAM++ embeddings into stable `发言人N` identities. Keep the frontend responsive by appending final text immediately and applying a later `speaker_update` event without crossing meeting/session boundaries.

**Tech Stack:** Python 3.12, FastAPI/WebSocket, DashScope realtime, CAM++ local model service, vanilla JavaScript, HTML/CSS, Node static contract tests, Python unittest.

## Global Constraints

- Realtime meetings and imported transcripts remain independent records, state, AI drafts, and write paths.
- Realtime context must not contain the meeting title or imported filename.
- Only a final segment whose normalized whole text equals the meeting title is filtered; a normal sentence mentioning the title remains intact.
- Voiceprint-library identity has priority over anonymous clustering.
- Unknown speakers receive stable first-seen labels `发言人1`, `发言人2`, and never all collapse to `实时发言人`.
- Speaker embedding values never leave backend/model-service boundaries and are not persisted in browser-visible meeting payloads.
- The debug configuration strip is removed from the user interface.
- Transcript controls use `搜索转写内容`, `全部发言人`, `已保存 / 保存中 / 保存失败`.
- AI tool labels are `规整 / 摘要 / 纪要 / 待办 / 标记`, one row on desktop and horizontal overflow on narrow screens.
- Add detailed comments around context filtering, PCM timeline math, clustering thresholds, asynchronous update guards, and CSS fixed-dimension decisions.
- Never use recursive or batch deletion commands. A temporary audio file may only be removed by its exact full path.
- The workspace is not a valid Git repository; do not run destructive Git commands and record progress through tests and the plan checklist instead of commits.

---

### Task 1: Realtime context and title-echo filtering

**Files:**
- Modify: `backend/app/recognition_policy.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_recognition_policy.py`

**Interfaces:**
- Produces: `build_realtime_context(meeting, policy, *, maximum_characters=1200, include_title=False) -> str`
- Produces: `_is_realtime_context_echo(text: str, meeting: Mapping[str, Any]) -> bool`
- Updates: `_finalize_realtime_transcript_event(...)` returns a `status` event with `code="context_echo_filtered"` instead of persisting a title-only segment.

- [ ] **Step 1: Write failing context tests**

Add tests proving the realtime corpus contains participant names and policy words but excludes `快速会议 7-14 16:00`, and that `include_title=True` preserves the legacy import/general behavior where needed.

- [ ] **Step 2: Write failing persistence tests**

Create a realtime meeting named `快速会议 7-14 16:00`; finalize an event containing only that title and assert no segment/revision is added. Add a second case `我们开始快速会议 7-14 16:00 的议程。` and assert it remains.

- [ ] **Step 3: Run RED tests**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_recognition_policy -v
```

Expected: the new title-exclusion and context-echo assertions fail against current production code.

- [ ] **Step 4: Implement minimal filtering**

Add the explicit `include_title` switch, compare normalized title/text using whitespace and terminal-punctuation removal, and convert title-only transcript events to a non-persisted status event before final normalization/revision allocation.

- [ ] **Step 5: Run GREEN tests**

Run the same unittest module and require zero failures.

---

### Task 2: CAM++ embedding API and meeting-scoped speaker tracker

**Files:**
- Create: `backend/app/realtime_speaker.py`
- Modify: `backend/app/model_clients.py`
- Modify: `backend/model_services/local_models_api.py`
- Create: `backend/tests/test_realtime_speaker.py`
- Modify: `backend/tests/test_local_model_integration.py`

**Interfaces:**
- Produces model route: `POST /v1/speakers/embedding` with `{audio_path}` and `{model, embedding, realModel, fallbackReason}`.
- Produces client method: `LocalVoiceprintClient.embedding(audio_path: str) -> dict[str, Any]`.
- Produces: `RealtimeSpeakerTracker.identify(embedding, voiceprint_match=None) -> SpeakerIdentity`.
- `SpeakerIdentity` exposes `speaker_name`, `speaker_cluster_id`, `speaker_source`, `voiceprint_id`, `confidence`, and optional `speaker_title`.

- [ ] **Step 1: Write failing tracker tests**

Cover: close vectors reuse `发言人1`; a distant vector creates `发言人2`; a voiceprint match returns its registered name; a later voiceprint hit upgrades an existing anonymous cluster; empty vectors use a stable fallback label without crashing.

- [ ] **Step 2: Write failing model-client contract test**

Assert `LocalVoiceprintClient.embedding()` posts to `/v1/speakers/embedding` and returns the vector without exposing it anywhere else.

- [ ] **Step 3: Run RED tests**

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_realtime_speaker tests.test_local_model_integration -v
```

Expected: imports/methods/routes are missing.

- [ ] **Step 4: Implement embedding endpoint and tracker**

Reuse `_speaker_embedding()` in the local model service. Implement cosine similarity and incremental centroid updates in the new focused module. Use a named threshold constant with a detailed comment; do not store raw embeddings in meeting dictionaries or API responses.

- [ ] **Step 5: Run GREEN tests**

Run both modules and require zero failures.

---

### Task 3: Integrate PCM utterance extraction and asynchronous speaker updates

**Files:**
- Modify: `backend/app/realtime_stream.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/store.py` only if no existing exact segment update method is suitable
- Test: `backend/tests/test_realtime_stream.py`
- Test: `backend/tests/test_api_contract.py`

**Interfaces:**
- Produces: bounded PCM timeline helper that appends PCM16 bytes and extracts a 16k mono WAV for `startMs/endMs`.
- Produces websocket event: `{type:"speaker_update", meetingId, sessionToken, segmentId, speakerName, speakerTitle, speakerClusterId, speakerSource, voiceprintId, voiceprintConfidence}`.
- Consumes `RealtimeSpeakerTracker` from Task 2.

- [ ] **Step 1: Write failing PCM timeline tests**

Append deterministic PCM frames, extract a known millisecond range, and verify WAV sample count/time alignment. Append beyond the retention limit and prove old bytes are discarded while current ranges remain extractable.

- [ ] **Step 2: Write failing WebSocket speaker tests**

Use a fake stream final event and fake embedding/match clients. Assert final text is stored once, then its stable segment is updated to `发言人1`; a different embedding becomes `发言人2`; a registered match displays the voiceprint name. Assert stale session tokens cannot update another meeting/session.

- [ ] **Step 3: Run RED tests**

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_realtime_stream tests.test_api_contract -v
```

- [ ] **Step 4: Implement bounded timeline and update path**

Append every streaming PCM frame before forwarding it upstream. Persist final text immediately with an anonymous stable identity, run speaker analysis via `asyncio.to_thread`, patch the exact stored segment, then send `speaker_update`. Delete only the exact temporary WAV path in `finally`.

- [ ] **Step 5: Run GREEN tests**

Run the same two modules and require zero failures.

---

### Task 4: Frontend controls, speaker updates, and AI tool layout

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/app.js`
- Modify: `frontend/styles.css`
- Modify: `frontend/prototype_spec_test.mjs`

**Interfaces:**
- Consumes websocket `speaker_update` event from Task 3.
- Removes `meetingConfigSummary` DOM/rendering.
- Preserves `transcriptSearch`, `transcriptSpeakerFilter`, and `transcriptAutosaveState` IDs with localized text.

- [ ] **Step 1: Write failing static contracts**

Assert the debug strip/container and `renderMeetingConfigSummary` call are absent; localized strings are present; the five exact short labels are present; CSS has a five-column non-wrapping tool grid and narrow-screen horizontal overflow.

- [ ] **Step 2: Write failing speaker-update contract**

Assert `app.js` handles `speaker_update`, checks current meeting and realtime session token, updates by exact segment ID, and re-renders speaker panel/editor without appending a duplicate segment.

- [ ] **Step 3: Run RED tests**

```powershell
node frontend\prototype_spec_test.mjs
```

Expected: old debug-strip and English-label assertions fail.

- [ ] **Step 4: Implement markup, rendering, and CSS**

Remove the debug container and render call. Localize the transcript toolbar. Change tool labels to short names. Use an isolated final CSS override with five equal tracks, fixed button height, no wrapping, and a separately positioned collapse control. Add guarded speaker update handling with detailed comments.

- [ ] **Step 5: Run GREEN checks**

```powershell
node frontend\prototype_spec_test.mjs
node --check frontend\app.js
```

Require both commands to exit 0.

---

### Task 5: Full regression and browser acceptance

**Files:**
- Modify tests only if verification reveals a genuine uncovered regression; do not weaken assertions.

**Interfaces:**
- Verifies all prior task outputs together.

- [ ] **Step 1: Run full automated regression**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_tests.ps1
```

Require frontend contracts, model-service checks, smoke checks, and every backend unittest to pass.

- [ ] **Step 2: Restart latest services and verify health**

Use the currently selected backend port, verify `/api/health`, and confirm the browser API query parameter points to that same process.

- [ ] **Step 3: Run browser smoke at desktop and mobile widths**

Verify: debug strip absent; Chinese search/filter/save controls visible; five tools remain one row or horizontally scroll without overlap; speaker panel and transcript editor fit; no JavaScript console exceptions.

- [ ] **Step 4: Exercise realtime behavior**

Use a real microphone or deterministic WebSocket fixture to prove title-only text is filtered, same speaker is stable, two speakers receive distinct labels, and a voiceprint match displays its saved name.

- [ ] **Step 5: Completion audit**

Map each of the user's five numbered requirements to an automated assertion plus runtime/visual evidence. Do not mark complete if speaker distinction is supported only by unit tests without an integration event path.
