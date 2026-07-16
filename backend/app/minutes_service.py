"""Immutable meeting-minutes versions built on Task 2 provenance envelopes.

This module is deliberately store-agnostic.  It turns one already generated minutes payload into
an auditable version record, while :mod:`app.store` owns persistence and legacy-field mirroring.
Keeping those roles separate makes template selection and stale-state rules testable without a
database or an HTTP request.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping
import uuid

from app.artifact_service import artifact_envelope


def resolve_minutes_template(
    meeting: Mapping[str, Any],
    requested_template_id: str | None,
    templates: Mapping[str, Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Resolve an explicit template ID or the meeting's immutable default snapshot.

    ``None`` means the caller omitted ``templateId`` and intentionally wants the template frozen
    when the meeting was created.  An empty string is different: it is an explicit but invalid
    request and must not silently select a global default.  Explicit switching reads the current
    template once, then the returned deep copy is frozen inside the newly created version without
    modifying the meeting's original processing configuration.
    """

    processing_config = meeting.get("processingConfig")
    config = processing_config if isinstance(processing_config, Mapping) else {}
    if requested_template_id is None:
        bound_template_id = str(config.get("templateId") or meeting.get("templateId") or "").strip()
        bound_snapshot = config.get("templateSnapshot")
        if not bound_template_id:
            raise ValueError("Meeting has no bound minutes template")
        if isinstance(bound_snapshot, Mapping) and bound_snapshot:
            return bound_template_id, deepcopy(dict(bound_snapshot))
        # Legacy meetings created before Task 1 may retain only the bound ID.  This compatibility
        # read is performed once and immediately frozen into the Task 6 version, never reused for
        # an already-created historical version.
        template = templates.get(bound_template_id)
        if not isinstance(template, Mapping):
            raise ValueError("Meeting-bound minutes template no longer exists")
        return bound_template_id, deepcopy(dict(template))

    explicit_template_id = str(requested_template_id).strip()
    if not explicit_template_id:
        raise ValueError("templateId must be a nonblank template ID")
    template = templates.get(explicit_template_id)
    if not isinstance(template, Mapping):
        raise ValueError("Unknown minutes template ID")
    return explicit_template_id, deepcopy(dict(template))


def generate_minutes_version(
    meeting: Mapping[str, Any],
    template_id: str,
    transcript_revision: int,
    *,
    template_snapshot: Mapping[str, Any],
    generated_content: Mapping[str, Any],
    sensitive_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Create one append-only minutes generation record with canonical source provenance.

    The generated layer is copied by ``artifact_envelope`` and the template/policy snapshots are
    copied here.  A caller can therefore mutate an API response, a global template, or future
    policy administration without retroactively changing what this version used.  The only
    intentionally mutable fields are ``status`` (for stale invalidation) and ``editedContent``.
    """

    expected_revision = int(transcript_revision)
    actual_revision = int(meeting.get("transcriptRevision", 0))
    if expected_revision != actual_revision:
        raise ValueError("Minutes generation revision does not match the meeting transcript")

    envelope = artifact_envelope(
        dict(meeting),
        dict(generated_content),
        source_segment_ids=[str(segment.get("id") or "") for segment in meeting.get("segments", [])],
        template_id=template_id,
    )
    return {
        "versionId": f"minutes-{uuid.uuid4().hex}",
        "templateId": template_id,
        "templateSnapshot": deepcopy(dict(template_snapshot)),
        "status": envelope["status"],
        "sourceTranscriptRevision": expected_revision,
        "sourceSegmentIds": envelope["sourceSegmentIds"],
        "sourceRanges": envelope["sourceRanges"],
        "generatedAt": envelope["generatedAt"],
        "generatedContent": envelope["generatedContent"],
        "editedContent": None,
        "editedAt": None,
        # This is the frozen Task 4 audit for both transcript and every template string sent to
        # the workflow.  Keeping it on every version avoids relying on mutable meeting metadata.
        "sensitivePolicy": deepcopy(dict(sensitive_policy)),
    }


def refresh_minutes_version_states(meeting: dict[str, Any]) -> bool:
    """Reconcile historical status labels with the transcript revision and current pointer.

    The function never promotes a stale version. Only a newly saved generation at the present
    revision may receive ``current`` status. A fresh pointer target remains the sole current
    version; every other same-revision record that was current becomes ``superseded``. This keeps
    template switching and regeneration auditable without making status disagree with the pointer.
    """

    changed = False
    current_revision = int(meeting.get("transcriptRevision", 0))
    current_version_id = str(meeting.get("minutesCurrentVersionId") or "").strip()
    for version in meeting.get("minutesVersions", []):
        if not isinstance(version, dict):
            continue
        if version.get("sourceTranscriptRevision") != current_revision and version.get("status") != "stale":
            version["status"] = "stale"
            changed = True
        elif version.get("status") == "current" and version.get("versionId") != current_version_id:
            # Same-revision history is still immutable content, but it is no longer the pointer
            # target.  ``superseded`` gives readers an honest history label instead of creating
            # several records that all claim to be the one current result.
            version["status"] = "superseded"
            changed = True
    return changed


def edit_minutes_version(version: dict[str, Any], content: str) -> None:
    """Record a human layer without replacing the immutable generated content snapshot."""

    version["editedContent"] = content
    # Do not import the store's formatting helper here: the store imports this service for
    # persistence methods, and a dependency in the opposite direction would create a cycle.
    version["editedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
