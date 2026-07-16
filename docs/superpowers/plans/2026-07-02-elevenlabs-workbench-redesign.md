# ElevenLabs Workbench Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework the static intelligent meeting frontend into the approved ElevenLabs-inspired editorial workbench without changing backend APIs or core JavaScript behavior.

**Architecture:** Keep the existing no-build static frontend. Use `prototype_spec_test.mjs` as a structural guard, then add lightweight HTML hooks and a CSS token system that restyles all existing pages while preserving `id`, `data-route`, and JS selectors.

**Tech Stack:** Static HTML, CSS, vanilla JavaScript, Node.js built-in `assert` test.

---

## File Structure

- Modify: `frontend/prototype_spec_test.mjs`
  - Adds checks for the new design-system markers and layout hooks.
- Modify: `frontend/index.html`
  - Adds lightweight wrapper classes and clearer workbench copy while preserving existing IDs and routes.
- Modify: `frontend/styles.css`
  - Adds ElevenLabs-inspired CSS variables and rewrites key surfaces, navigation, buttons, tables, detail layout, and responsive behavior.
- Modify: `.gitignore`
  - Ignores `.superpowers/` local visual-companion artifacts.

## Task 1: Add Structural Design Checks

**Files:**
- Modify: `frontend/prototype_spec_test.mjs`

- [ ] **Step 1: Write the failing test**

Add these expected markers near the existing `requiredHtmlMarkers` and `requiredCssMarkers` arrays:

```javascript
const requiredElevenLabsHtmlMarkers = [
  "workbench-hero",
  "hero-copy",
  "hero-metrics",
  "surface-panel",
  "editor-surface",
];

const requiredElevenLabsCssMarkers = [
  "--canvas:",
  "--ink:",
  "--gradient-mint:",
  ".workbench-hero",
  ".hero-metrics",
  ".surface-panel",
  ".editor-surface",
];
```

Then loop over both arrays with `assert.ok(...)`, matching the existing style.

- [ ] **Step 2: Run test to verify it fails**

Run: `node frontend\prototype_spec_test.mjs`

Expected: failure mentioning `workbench-hero` or another new marker missing.

- [ ] **Step 3: Keep this failure as the guard**

Do not weaken existing assertions. The failure must be caused by missing new design hooks, not syntax errors.

## Task 2: Add Lightweight HTML Hooks

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: Add meeting-list workbench classes**

Change the `page-heading` inside `meetingListView` to include the new hero classes while keeping existing IDs:

```html
<div class="page-heading workbench-hero">
  <div class="hero-copy">
    <h2>会议列表</h2>
    <p>创建快速会议并开启实时转写；导入音视频记录保留在“导入转写”台账。</p>
  </div>
  <div class="page-actions">
    <button class="primary-button" id="quickMeetingBtn">快速会议</button>
    <button class="secondary-button" id="batchExportBtn">批量导出</button>
  </div>
</div>
```

- [ ] **Step 2: Add metric and panel classes**

Change existing containers without changing IDs:

```html
<div id="recordsOverview" class="overview-strip hero-metrics"></div>
<div class="iflytek-import-table meeting-list-table surface-panel">
```

- [ ] **Step 3: Add import and detail surface classes**

Add `surface-panel` to import tables and `editor-surface` to the central editor panel:

```html
<div class="iflytek-import-table surface-panel">
<section class="editor-panel editor-surface">
```

## Task 3: Apply CSS Design System

**Files:**
- Modify: `frontend/styles.css`

- [ ] **Step 1: Add CSS token block**

Add a commented `:root` block at the top:

```css
:root {
  /* ElevenLabs-inspired workbench tokens. Pastel gradients are decorative only. */
  --canvas: #f5f5f3;
  --canvas-soft: #faf9f7;
  --surface: #ffffff;
  --surface-strong: #f0ece7;
  --ink: #0c0a09;
  --ink-soft: #292524;
  --body: #57534e;
  --muted: #8d867f;
  --hairline: #e7e2dc;
  --hairline-strong: #d8d0c8;
  --gradient-mint: #a7e5d3;
  --gradient-peach: #f4c5a8;
  --gradient-lavender: #c8b8e0;
  --gradient-sky: #a8c8e8;
  --success: #16a34a;
  --danger: #dc2626;
}
```

- [ ] **Step 2: Rewrite global surfaces**

Update `body`, `.app-shell`, `.side-nav`, `.top-actions`, `.workspace-card`, and form controls to use the token palette, warm background, and hairline borders.

- [ ] **Step 3: Rewrite buttons and badges**

Use near-black pill primary buttons, outline secondary buttons, and warm-gray badges. Preserve `danger-button` as red.

- [ ] **Step 4: Style workbench hero and metrics**

Add `.workbench-hero`, `.hero-copy`, `.hero-metrics`, `.surface-panel`, and metric child styles. Use pastel radial gradients only in background layers.

- [ ] **Step 5: Style tables, import panels, and detail editor**

Update `.records-table`, `.iflytek-import-table`, `.detail-workbench`, `.speaker-panel`, `.editor-surface`, `.right-tool-dock`, `.tool-tab-bar`, `.tool-result-panel`, and `.bottom-audio-player`.

- [ ] **Step 6: Check responsive rules**

Update existing `@media` blocks so the layout stacks cleanly below `1180px` and `760px`.

## Task 4: Verify

**Files:**
- Test: `frontend/prototype_spec_test.mjs`

- [ ] **Step 1: Run structural test**

Run: `node frontend\prototype_spec_test.mjs`

Expected: `prototype spec ok`

- [ ] **Step 2: Start static frontend server**

Run: `scripts\start_frontend.ps1`

Expected: frontend served at `http://127.0.0.1:5173`.

- [ ] **Step 3: Inspect main pages**

Open `http://127.0.0.1:5173` and check:

- Meeting list hero, metrics, filters, and table are visible.
- Import ledger controls are visible and not overlapping.
- Opening/import detail keeps speaker panel, transcript editor, AI tools, and audio bar aligned.
- Narrow viewport stacks without text escaping buttons or panels.

## Self-Review

- Spec coverage: tasks cover test guard, HTML hooks, CSS visual system, responsive checks, and local verification.
- Placeholder scan: no `TBD`, `TODO`, or unresolved implementation placeholders remain.
- Type consistency: all markers in tests match planned HTML/CSS class names exactly.
