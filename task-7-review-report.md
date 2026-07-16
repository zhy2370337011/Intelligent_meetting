# Task 7 Review Report

## Verdict

**Not clean. Changes required before acceptance.** The configuration/provenance/version backend chain is substantially implemented, but the review found important workflow gaps and the browser audit does not exercise the core populated detail workbench. The 21/21 automated route checks are therefore insufficient evidence for the Task 7 brief.

## Review Evidence

- Brief: `.superpowers/sdd/task-7-brief.md`
- Implementation report: `.superpowers/sdd/task-7-implementation-report.md`
- Review brief: `.superpowers/sdd/task-7-review-brief.md`
- Audit script: `scripts/browser_product_audit.mjs`
- Screenshots: `test-results/product-audit/`
- Additional status evidence: `test-results/.last-run.json` reports `status: failed` with no failure detail.

## Critical

None confirmed from the read-only review.

## Important

### I1. Source links do not actually seek playback

`scrollToSourceSegment()` selects and scrolls the transcript, but only writes a `data-seek-ms` attribute and replaces the clock text. There is no audio element, `currentTime` assignment, or playback seek API. The supposed player in `frontend/index.html:155-158` is a `div` with inert buttons, while the implementation is `frontend/app.js:509-521`.

This misses the brief's required behavior: `scrollToSourceSegment(id, startMs)` must select/scroll the source segment **and seek playback**. It also means source references can look successful while leaving playback at its previous position.

### I2. Destructive actions bypass confirmation, and batch actions are enabled with no selection

The voiceprint toolbar renders `删除` and `下载` without `disabled` state in `frontend/index.html:183-184`. `renderVoiceprintManager()` never synchronizes those buttons to `state.selectedVoiceprintIds` (`frontend/app.js:1211-1231`); the handlers only reject an empty selection after click (`frontend/app.js:1884-1895`). This is visible in `test-results/product-audit/voiceprints-desktop-1366x768.png`, where the empty table still presents active batch controls.

Separately, template and voiceprint row deletes call `DELETE` directly from the delegated click handler (`frontend/app.js:2702-2704`), and record rows also render delete actions (`frontend/app.js:306`). No confirmation flow is present in those paths. This violates the brief's explicit confirmation requirement and makes an accidental destructive click possible.

### I3. The voiceprint management table is not usable at 1366px

`test-results/product-audit/voiceprints-desktop-1366x768.png` shows a horizontal scrollbar inside the table and the right-side `操作` column cut off. The cause is structural: the base table has `min-width: 980px` (`frontend/styles.css:349-359`), the later override still requires `min-width: 760px` (`frontend/styles.css:2583-2589`), and the group table intentionally uses `overflow: auto` (`frontend/styles.css:2653-2655`). Document-level `scrollWidth <= clientWidth` therefore passes while a required management column remains outside the initial usable viewport.

This is a functional discoverability problem, not merely a cosmetic scrollbar: users cannot see row actions without horizontal scrolling, and the screenshot gives no clear table-specific affordance or priority treatment for the action column.

### I4. The audit never reaches a populated detail workbench

All records screenshots show an empty meeting list, for example `test-results/product-audit/records-desktop-1600x1000.png` and `test-results/product-audit/records-mobile-390x844.png`. No screenshot covers a selected meeting with transcript segments, configuration summary, stale artifact banner, source links, playback-active state, speaker filter, autosave state, or minutes version controls.

The script only clicks the seven top-level route buttons (`scripts/browser_product_audit.mjs:14-19`, `scripts/browser_product_audit.mjs:131-145`). It does not seed or select a meeting, open import/realtime detail, generate or load an artifact, edit a segment, switch a version, click a source reference, or exercise a stale state. The implementation report's `21/21` result proves only empty-route rendering and the narrow layout checks, not the core Task 7 workbench.

### I5. Audit coverage misses internal overflow and HTTP failures

The audit measures only `document.documentElement.scrollWidth` and a selected set of controls/headings (`scripts/browser_product_audit.mjs:96-128`). It does not inspect descendant scroll containers such as `.group-table`, which is why the voiceprint failure is compatible with a passing audit. It also collects `failedResponses` but never asserts that the list is empty (`scripts/browser_product_audit.mjs:149-154`); a 4xx/5xx response with no console error can pass.

The overlap selector also excludes table cells, labels, links, and most layout containers, so this is not a general usability or accessibility audit.

### I6. Mobile navigation evidence is not acceptance-grade

Every reviewed mobile screenshot in `test-results/product-audit/` shows only the first three destinations beside the brand, with no visible overflow affordance or menu for the remaining four. The problem is clearest in `records-mobile-390x844.png` and `integration-mobile-390x844.png`.

The current stylesheet contains a later two-row grid attempt (`frontend/styles.css:2592-2600`), but the screenshots were written at 11:02 while `frontend/styles.css` was last modified at 11:19. The evidence is therefore out of sync with the source under review. The implementation report cannot claim the mobile navigation is clean until fresh screenshots demonstrate that all seven destinations are visible or an explicit, discoverable menu/overflow control exists.

## Minor

### M1. Management headings remain oversized for a compact operational UI

The shared heading rule keeps desktop page headings at up to 44px (`frontend/styles.css:1521-1530`). This is visible across `voiceprints-desktop-1600x1000.png`, `hotwords-desktop-1600x1000.png`, `sensitive-desktop-1600x1000.png`, `templates-desktop-1600x1000.png`, and `integration-desktop-1600x1000.png`. The treatment reads editorial rather than dense enterprise management and consumes vertical space before the primary controls.

### M2. Mobile template import surface is disproportionately tall

`test-results/product-audit/templates-mobile-390x844.png` shows a large import card occupying most of the first content viewport while the actual template list is empty. The base card height is 320px (`frontend/styles.css:958-970`); although a later mobile override attempts to reduce it (`frontend/styles.css:2630-2632`), the screenshot predates the current stylesheet. This is another evidence mismatch and should be re-captured before treating the mobile layout as resolved.

### M3. Empty management pages leave large unexplained blank regions

The desktop screenshots for integration and templates leave most of the viewport blank after a small amount of content, especially `integration-desktop-1600x1000.png` and `templates-desktop-1600x1000.png`. Empty states are present, but the fixed workspace treatment does not provide useful density or an intentional empty-state explanation. This is lower risk than the clipped table because it does not block an action.

## Positive Findings

- Backend meeting creation freezes `processingConfig`, including recognition inputs, sensitive-rule revision, template ID, and template snapshot (`backend/app/meeting_domain.py:60-85`).
- The detached display transcript endpoint keeps masking out of the stored source (`backend/app/main.py:984-1021`), and the frontend deliberately does not fall back to raw source text when the safe display view fails (`frontend/app.js:435-449`).
- Artifact envelopes preserve generated and edited layers, source revision, canonical source ranges, and stale status (`backend/app/artifact_service.py:74-176`).
- Minutes versions snapshot the template/policy and enforce revision matching (`backend/app/minutes_service.py:59-129`); the API exposes ordered history and a current pointer (`backend/app/main.py:2850-2861`), while the frontend keeps template/version selection read-only until an explicit new generation (`frontend/app.js:2031-2066`, `frontend/app.js:2764-2777`).
- Transcript search and speaker filtering are wired to rerender the workbench (`frontend/app.js:551-571`, `frontend/app.js:2801-2808`). However, search currently matches raw `segment.text` while the editor renders the policy-safe display text, so this behavior still needs a populated-data check for privacy and user expectation.

## Final Assessment

The implementation has a credible provenance foundation, but Task 7 is not acceptance-ready. Fix the destructive-control semantics and real playback seek first, then rerun an interaction-based audit with a seeded meeting containing transcript segments, at least two minutes versions, a stale artifact, source ranges, and a masked display case. Refresh all screenshots after the final CSS changes and extend the audit to descendant scroll containers and failed network responses before issuing a clean verdict.
