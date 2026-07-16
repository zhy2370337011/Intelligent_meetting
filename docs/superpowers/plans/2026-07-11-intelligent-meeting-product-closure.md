# Intelligent Meeting Product Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing realtime/import transcription prototype into a coherent meeting product where configuration, transcript revisions, speaker identity, recognition optimization, sensitive-word policy, minutes, todos, exports, and UI all operate on one traceable source of truth.

**Architecture:** Keep the existing FastAPI + persistent JSON/SQLite-style store and vanilla frontend, but add focused policy modules instead of continuing to grow `main.py`. A meeting stores an immutable processing configuration snapshot and a mutable `transcriptRevision`; every derived AI artifact records its source revision and segment references. Realtime and import flows share segment contracts while retaining independent routes, tasks, controls, and audio sources.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, existing persistent store, DashScope Qwen ASR, local VAD/CAM++/alignment HTTP services, vanilla HTML/CSS/JavaScript, Node static contract tests, Python unittest, Edge/Playwright smoke tests.

## Global Constraints

- In-scope product labels are exactly: 实时转写、导入转写（在线转写）、声纹库、识别优化、禁忌词、纪要模板。
- “Online transcription” means uploaded audio/video import transcription; third-party meeting bots are out of scope.
- Preserve the current `/api/meetings`, `/api/imports/transcribe`, realtime WebSocket, voiceprint, optimization, sensitive-rule, and template paths.
- Realtime and import records must never fall back to each other or share AI drafts implicitly.
- Raw transcript text must remain stored; masking and forced replacements are separate policies with audit metadata.
- Missing model services must produce explicit unavailable/failed states, never fabricated success.
- Add detailed comments for non-obvious code, especially state transitions, revision invalidation, policy scope, and failure isolation.
- UI must have no horizontal overflow or overlapping controls at 1600x1000, 1366x768, and 390x844.
- The current `.git` directory is not recognized as a repository; do not run commit commands until repository metadata is repaired externally. Treat every green test checkpoint as the task boundary.

---

### Task 1: Meeting Configuration Snapshot and Transcript Revision

**Files:**
- Create: `backend/app/meeting_domain.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/store.py`
- Test: `backend/tests/test_meeting_product_closure.py`

**Interfaces:**
- Produces: `build_processing_snapshot(request, store) -> dict[str, Any]`
- Produces: `bump_transcript_revision(meeting, reason, segment_ids) -> dict[str, Any]`
- Meeting fields: `processingConfig`, `transcriptRevision`, `transcriptRevisionReason`, `transcriptUpdatedAt`

- [ ] **Step 1: Write failing tests for realtime/import configuration snapshots**

```python
def test_meeting_snapshot_persists_product_configuration():
    meeting = create_meeting(MeetingCreateRequest(
        meetingName="项目复盘",
        participantNames=["王忠", "李敏"],
        voiceprintGroupId="vg-office",
        optimizationProfile={"manual": True, "document": False, "smart": True, "replacement": True},
        keywordLibraryIds=["kw-001"],
        templateId="tpl-001",
        notes="仅供内部复盘",
    ))
    config = meeting["processingConfig"]
    self.assertEqual(config["transcriptionMode"], "realtime")
    self.assertEqual(config["participantNames"], ["王忠", "李敏"])
    self.assertEqual(config["voiceprintGroupId"], "vg-office")
    self.assertEqual(config["templateId"], "tpl-001")
    self.assertEqual(meeting["transcriptRevision"], 0)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.test_meeting_product_closure.MeetingProductClosureTest.test_meeting_snapshot_persists_product_configuration`

Expected: FAIL because `MeetingCreateRequest` and meeting records do not contain the new configuration fields.

- [ ] **Step 3: Add request fields and snapshot builder**

```python
def build_processing_snapshot(req: Any, store: Any, *, mode: str) -> dict[str, Any]:
    library_ids = list(req.keywordLibraryIds or [])
    libraries = store.keyword_libraries
    effective_words = [
        word.strip()
        for library_id in library_ids
        for word in libraries.get(library_id, {}).get("words", [])
        if word.strip()
    ]
    return {
        "transcriptionMode": mode,
        "language": req.language,
        "audioSource": req.audioSource,
        "enableDiarization": bool(req.enableDiarization),
        "participantNames": list(req.participantNames or []),
        "voiceprintGroupId": req.voiceprintGroupId or "vg-all",
        "optimizationProfile": dict(req.optimizationProfile or {}),
        "keywordLibraryIds": library_ids,
        "effectiveVocabulary": list(dict.fromkeys(effective_words)),
        "sensitiveRuleVersion": store.config_revision("sensitive_rules"),
        "templateId": req.templateId,
        "templateSnapshot": store.template_snapshot(req.templateId),
        "notes": req.notes or "",
        "attachments": list(req.attachments or []),
    }
```

- [ ] **Step 4: Add revision bumping to segment create/edit/speaker rename**

```python
def bump_transcript_revision(meeting: dict[str, Any], *, reason: str, segment_ids: list[str]) -> dict[str, Any]:
    meeting["transcriptRevision"] = int(meeting.get("transcriptRevision", 0)) + 1
    meeting["transcriptRevisionReason"] = reason
    meeting["transcriptRevisionSegmentIds"] = list(dict.fromkeys(segment_ids))
    meeting["transcriptUpdatedAt"] = format_datetime()
    return meeting
```

Call it from `add_realtime_segment`, completed import persistence, segment PATCH, and speaker-wide rename. Do not increment for partial text.

- [ ] **Step 5: Run task tests and existing meeting contracts**

Run: `python -m unittest tests.test_meeting_product_closure tests.test_api_contract.ApiContractTest.test_create_meeting_uses_keyword_libraries_and_template`

Expected: PASS.

---

### Task 2: Derived Artifact Provenance and Stale-State Rules

**Files:**
- Create: `backend/app/artifact_service.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/store.py`
- Test: `backend/tests/test_meeting_product_closure.py`

**Interfaces:**
- Consumes: `meeting.transcriptRevision`
- Produces: `save_derived_artifact(meeting_id, artifact_type, payload, source_segment_ids, template_id=None)`
- Produces: `refresh_artifact_states(meeting) -> dict[str, Any]`

- [ ] **Step 1: Write failing provenance and invalidation tests**

```python
def test_editing_transcript_marks_minutes_and_todos_stale():
    meeting = self.completed_meeting()
    summary = generate_summary(meeting["id"])
    minutes = generate_minutes(meeting["id"], MinutesRequest(templateName="默认会议纪要模板"))
    patch_meeting_segment(meeting["id"], meeting["segments"][0]["id"], SegmentPatchRequest(text="修订正文"))
    refreshed = get_meeting(meeting["id"])
    self.assertEqual(refreshed["summaryArtifact"]["status"], "stale")
    self.assertEqual(refreshed["minutesArtifact"]["status"], "stale")
    self.assertLess(refreshed["minutesArtifact"]["sourceTranscriptRevision"], refreshed["transcriptRevision"])
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_meeting_product_closure.MeetingProductClosureTest.test_editing_transcript_marks_minutes_and_todos_stale`

Expected: FAIL because generated artifacts have no revision metadata.

- [ ] **Step 3: Implement artifact envelope and source references**

```python
def artifact_envelope(meeting: dict[str, Any], payload: dict[str, Any], *, source_segment_ids: list[str], template_id: str = "") -> dict[str, Any]:
    return {
        "status": "current",
        "sourceTranscriptRevision": int(meeting.get("transcriptRevision", 0)),
        "sourceSegmentIds": list(dict.fromkeys(source_segment_ids)),
        "sourceRanges": source_ranges(meeting.get("segments", []), source_segment_ids),
        "templateId": template_id,
        "generatedAt": format_datetime(),
        "generatedContent": payload,
        "editedContent": None,
    }
```

Generated summary, minutes, todos, discourse, and highlights must use the envelope. Return backward-compatible top-level fields to the current frontend.

- [ ] **Step 4: Mark old artifacts stale after revision changes**

```python
for key in ("summaryArtifact", "minutesArtifact", "todosArtifact", "discourseArtifact"):
    artifact = meeting.get(key)
    if artifact and artifact.get("sourceTranscriptRevision") != meeting.get("transcriptRevision"):
        artifact["status"] = "stale"
```

- [ ] **Step 5: Run artifact and AI workflow tests**

Run: `python -m unittest tests.test_meeting_product_closure tests.test_api_contract.ApiContractTest.test_five_ai_workflow_buttons_and_docx_export_are_frontend_ready`

Expected: PASS.

---

### Task 3: Unified Effective Vocabulary for Realtime and Import

**Files:**
- Create: `backend/app/recognition_policy.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/realtime_stream.py`
- Modify: `backend/app/asr_gateway.py`
- Test: `backend/tests/test_recognition_policy.py`

**Interfaces:**
- Produces: `build_effective_vocabulary(meeting, store) -> EffectiveVocabulary`
- Produces dataclass fields: `words`, `replacement_rules`, `sources`, `snapshot_hash`
- Consumed by realtime `context_text` and import `hotwords`

- [ ] **Step 1: Write failing tests for all optimization sources**

```python
def test_effective_vocabulary_combines_enabled_sources_and_scope():
    policy = build_effective_vocabulary(meeting, store)
    self.assertIn("全国政协", policy.words)
    self.assertIn("KingbaseES", policy.words)
    self.assertEqual(policy.replacement_rules["声文"], "声纹")
    self.assertEqual(policy.sources, {"library", "manual", "document", "smart", "replacement"})
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_recognition_policy -v`

Expected: FAIL because no unified policy exists and document/smart endpoints return fixed examples.

- [ ] **Step 3: Implement policy assembly and document extraction**

Use stored keyword libraries selected by the meeting snapshot, enabled manual word sets matching the meeting language/scope, document extraction records explicitly attached to the meeting, user-confirmed smart terms, and enabled replacement rules. Extract document candidates from parsed text with deterministic frequency + existing domain-token rules; do not return hardcoded demonstration words.

```python
@dataclass(frozen=True)
class EffectiveVocabulary:
    words: tuple[str, ...]
    replacement_rules: dict[str, str]
    sources: frozenset[str]
    snapshot_hash: str
```

- [ ] **Step 4: Wire policy into both ASR paths**

Realtime: build bounded `corpus.text` from meeting title, participants, and `policy.words`.

Import: pass `list(policy.words)` to `asr_gateway.transcribe(...)`.

Apply replacements only to final text and record `normalizationEdits=[{"from", "to", "ruleId"}]`; preserve `rawText`.

- [ ] **Step 5: Run recognition tests**

Run: `python -m unittest tests.test_recognition_policy tests.test_realtime_stream tests.test_core_services.CoreServicesTest.test_dashscope_local_long_audio_is_transcribed_as_timestamped_chunks`

Expected: PASS.

---

### Task 4: Sensitive-Word Policy by Display, AI, and Export Scope

**Files:**
- Create: `backend/app/sensitive_policy.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/export_service.py`
- Modify: `backend/app/llm_workflow.py`
- Modify: `frontend/app.js`
- Test: `backend/tests/test_sensitive_policy.py`

**Interfaces:**
- Produces: `apply_sensitive_policy(text, rules, target) -> PolicyResult`
- `target` is exactly `display`, `ai`, or `export`
- `PolicyResult` fields: `text`, `hits`, `rule_version`

- [ ] **Step 1: Write failing scope tests**

```python
def test_display_only_rule_preserves_ai_and_export_text():
    rule = {"word": "机密", "replacement": "mask", "applyScope": "display", "enabled": True, "caseSensitive": False}
    self.assertEqual(apply_sensitive_policy("机密方案", [rule], "display").text, "**方案")
    self.assertEqual(apply_sensitive_policy("机密方案", [rule], "ai").text, "机密方案")
    self.assertEqual(apply_sensitive_policy("机密方案", [rule], "export").text, "机密方案")
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_sensitive_policy -v`

Expected: FAIL because current replacement does not implement independent targets.

- [ ] **Step 3: Implement target-aware policy and hit audit**

```python
@dataclass(frozen=True)
class PolicyResult:
    text: str
    hits: tuple[dict[str, Any], ...]
    rule_version: str
```

Do not mutate `segment.rawText` or `segment.text` for display-only rules. Add `/api/meetings/{id}/transcript-view?target=display` if the current meeting response cannot safely expose masked text separately.

- [ ] **Step 4: Apply the same rule version to AI and exports**

AI workflows receive target `ai`; DOCX/text exports receive target `export`. Persist the applied rule version in artifact/export metadata.

- [ ] **Step 5: Run sensitive and export tests**

Run: `python -m unittest tests.test_sensitive_policy tests.test_core_services.CoreServicesTest.test_sensitive_words_are_replaced_by_stars tests.test_api_contract.ApiContractTest.test_forbidden_words_support_display_modes_and_case_sensitive`

Expected: PASS.

---

### Task 5: Real Voiceprint Registration, Runtime Health, and Speaker Correction

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/model_clients.py`
- Modify: `backend/app/voiceprint_service.py`
- Modify: `scripts/start_model_services.ps1`
- Modify: `frontend/app.js`
- Test: `backend/tests/test_voiceprint_product_flow.py`

**Interfaces:**
- Produces: `/api/model-services/status` with VAD/voiceprint/alignment readiness
- Produces: speaker correction mode `meeting_only` or `sync_voiceprint`
- Consumes registered embedding IDs only when status is `registered`

- [ ] **Step 1: Write failing service-health and registration tests**

```python
def test_voiceprint_without_runtime_is_unavailable_not_registered():
    result = upload_voiceprint_sample("vp-1", upload)
    self.assertEqual(result["voiceprint"]["registerStatus"], "waiting_model_config")
    self.assertNotIn("mock_registered", result["voiceprint"]["modelStatus"])

def test_speaker_correction_can_remain_meeting_scoped():
    result = rename_speaker(meeting_id, SpeakerRenameRequest(oldName="发言人1", name="王忠", syncMode="meeting_only"))
    self.assertTrue(all(segment["speakerName"] == "王忠" for segment in result["segments"]))
    self.assertFalse(any(item["name"] == "王忠" for item in list_voiceprints()["items"]))
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_voiceprint_product_flow -v`

Expected: FAIL because runtime status is absent and speaker correction always attempts library sync.

- [ ] **Step 3: Implement readiness aggregation and truthful states**

Probe `/v1/health` and expose per-capability `ready`, `mode`, and `message`. Do not silently start mock mode in production configuration. The frontend must disable “upload sample and register” when the runtime is unavailable while still allowing personnel metadata to be saved as `pending_sample`.

- [ ] **Step 4: Implement scoped speaker correction and revision bump**

Meeting-only correction patches matching segments and increments `transcriptRevision`. Sync mode first saves transcript changes, then creates/selects department group, upserts the voiceprint person, and returns a warning if sample registration fails.

- [ ] **Step 5: Verify genuine model service when dependencies are available**

Run: `powershell -ExecutionPolicy Bypass -File scripts/start_model_services.ps1 -Port 8100 -MockMode false`

Then: `python scripts/check_model_services.py --base-url http://127.0.0.1:8100 --deep --audio-path "backend/data/audio_clips/rec-09f9edd868-realtime-0.wav"`

Expected: health, VAD, voiceprint register/match, and alignment checks pass. If model weights or runtime dependencies are absent, the product remains in explicit unavailable state and this acceptance item remains open.

---

### Task 6: Meeting-Bound Minutes Templates and Version History

**Files:**
- Create: `backend/app/minutes_service.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/store.py`
- Modify: `frontend/app.js`
- Test: `backend/tests/test_minutes_product_flow.py`

**Interfaces:**
- Produces: `generate_minutes_version(meeting, template_id, transcript_revision) -> dict`
- Produces: `GET /api/meetings/{id}/minutes/versions`
- Produces: `POST /api/meetings/{id}/minutes/generate` with `templateId`

- [ ] **Step 1: Write failing template-binding/history tests**

```python
def test_meeting_uses_bound_template_and_preserves_regeneration_history():
    first = generate_minutes(meeting_id, MinutesRequest(templateId="tpl-project"))
    second = generate_minutes(meeting_id, MinutesRequest(templateId="tpl-qa"))
    versions = list_minutes_versions(meeting_id)["items"]
    self.assertEqual([item["templateId"] for item in versions], ["tpl-project", "tpl-qa"])
    self.assertNotEqual(first["versionId"], second["versionId"])
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_minutes_product_flow -v`

Expected: FAIL because generation selects by name/default and overwrites the current result.

- [ ] **Step 3: Implement template snapshot resolution and versions**

Resolve the meeting-bound template by ID, use its stored tag bindings, and create a new immutable generated version. Store edited content separately. Every section includes `sourceSegmentIds` and `sourceRanges` from the artifact service.

- [ ] **Step 4: Wire the right-side minutes UI to meeting template and stale state**

The frontend sends `templateId: meeting.processingConfig.templateId`; template switching opens a menu, generates a new version, and does not overwrite the previously edited version. Display a stale banner when source revision is old.

- [ ] **Step 5: Run template and AI tests**

Run: `python -m unittest tests.test_minutes_product_flow tests.test_iflytek_style_contract.IflytekStyleContractTest.test_templates_support_source_tabs_copy_import_and_system_delete_guard`

Expected: PASS.

---

### Task 7: Compact Product UI and Traceable Transcript Workbench

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/styles.css`
- Modify: `frontend/app.js`
- Modify: `frontend/prototype_spec_test.mjs`
- Test: create `scripts/browser_product_audit.mjs`

**Interfaces:**
- Consumes artifact status/source ranges from Tasks 2 and 6
- Produces UI markers: `data-artifact-status`, `data-source-segment`, `data-processing-mode`

- [ ] **Step 1: Add failing frontend contract assertions**

```javascript
assert.ok(!html.includes('class="records-hero"'), "会议列表不得保留重复装饰横幅");
assert.ok(html.includes('id="meetingConfigSummary"'), "详情页必须展示会议配置快照");
assert.ok(js.includes('scrollToSourceSegment'), "AI 结果必须能跳回来源逐字稿");
assert.ok(js.includes('renderArtifactStaleBanner'), "逐字稿更新后必须提示重新生成");
```

- [ ] **Step 2: Verify RED**

Run: `node frontend/prototype_spec_test.mjs`

Expected: FAIL on missing compact layout and provenance UI.

- [ ] **Step 3: Refactor list and configuration pages**

Remove the repeated meeting hero. Use a compact page header with title, metrics, and primary action. Convert voiceprint, optimization, sensitive words, templates, and integration pages to a consistent two-column management layout with bounded content height, useful empty states, disabled batch controls, and confirmation dialogs.

- [ ] **Step 4: Upgrade the transcript workbench**

Add transcript search, speaker filtering, active playback highlight, autosave state, configuration summary, stale banners, and source-reference buttons. `scrollToSourceSegment(id, startMs)` selects the segment and seeks the audio player without changing routes.

- [ ] **Step 5: Add browser audit script**

The script visits every route at 1600x1000, 1366x768, and 390x844, asserts `scrollWidth <= clientWidth`, checks visible control overlap via bounding rectangles, collects `pageerror`/console errors, and saves screenshots under `test-results/product-audit/`.

- [ ] **Step 6: Run frontend verification**

Run: `node frontend/prototype_spec_test.mjs`

Run: `node scripts/browser_product_audit.mjs`

Expected: all assertions pass and screenshots show no overlap, clipped text, or large unexplained blank regions.

---

### Task 8: End-to-End Product Closure Verification

**Files:**
- Modify: `scripts/smoke_verify_system.py`
- Modify: `scripts/run_tests.ps1`
- Modify: `README.md`
- Test: all test suites

**Interfaces:**
- Consumes all prior tasks
- Produces a capability report with `ready`, `degraded`, or `failed` per subsystem

- [ ] **Step 1: Extend smoke verification with the complete product chain**

Create a meeting with explicit snapshot settings, transcribe known audio, register/match a voiceprint when the service is ready, apply recognition and sensitive policies, edit a speaker, verify artifact stale state, regenerate minutes with another template, export the result, and assert every artifact references source segments.

- [ ] **Step 2: Add a machine-readable capability report**

```json
{
  "realtime": {"status": "ready"},
  "import": {"status": "ready", "vadMode": "model"},
  "voiceprint": {"status": "ready", "model": "CAM++"},
  "recognitionPolicy": {"status": "ready", "sources": 5},
  "sensitivePolicy": {"status": "ready", "targets": 3},
  "minutes": {"status": "ready", "versioned": true},
  "frontend": {"status": "ready", "viewports": 3}
}
```

Any unavailable real model keeps the corresponding subsystem `degraded` or `failed`; the report must not convert it to ready because mock tests pass.

- [ ] **Step 3: Run the full regression suite**

Run: `powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1`

Expected: all backend, frontend, model-service contract, and smoke tests pass.

- [ ] **Step 4: Run real-service and browser verification**

Run: `python scripts/smoke_verify_system.py --include-asr`

Run: `node scripts/browser_product_audit.mjs`

Expected: capability report contains `ready` for every in-scope subsystem, no JS errors, and no viewport layout failures.

- [ ] **Step 5: Update operational documentation**

Document the meaning of realtime versus import transcription, required model services, truthful degraded states, configuration snapshot behavior, transcript revision invalidation, template versioning, and exact startup/verification commands.
