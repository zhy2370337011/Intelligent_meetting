"""Traceable envelopes for content derived from persisted meeting transcripts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


# One generated result occupies each singular field. Highlights remain a list because creating a
# second marker must not overwrite the source reference for a previous marker.
SINGLE_ARTIFACT_FIELDS = (
    "summaryArtifact",
    "minutesArtifact",
    "todosArtifact",
    "discourseArtifact",
)
MULTIPLE_ARTIFACT_FIELDS = ("highlightArtifacts",)
ARTIFACT_FIELD_BY_TYPE = {
    "summary": "summaryArtifact",
    "minutes": "minutesArtifact",
    "todos": "todosArtifact",
    "discourse": "discourseArtifact",
    "highlight": "highlightArtifacts",
}


def artifact_field_for_type(artifact_type: str) -> str:
    """Return the durable meeting field for a supported derived-artifact type."""

    try:
        return ARTIFACT_FIELD_BY_TYPE[artifact_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported derived artifact type: {artifact_type}") from exc


def _canonical_source_segment_ids(meeting: dict[str, Any], source_segment_ids: list[str]) -> list[str]:
    """Retain only first-occurrence IDs that identify persisted segments in this meeting.

    Provider-local, blank, duplicate, or cross-meeting IDs must never become provenance. Filtering
    against persisted segments means every reference is durable and safe for later source seeking.
    """

    persisted_ids = {
        str(segment.get("id") or "").strip()
        for segment in meeting.get("segments", [])
        if str(segment.get("id") or "").strip()
    }
    canonical_ids: list[str] = []
    seen: set[str] = set()
    for source_id in source_segment_ids:
        normalized_id = str(source_id or "").strip()
        if normalized_id and normalized_id in persisted_ids and normalized_id not in seen:
            canonical_ids.append(normalized_id)
            seen.add(normalized_id)
    return canonical_ids


def _source_ranges(meeting: dict[str, Any], source_segment_ids: list[str]) -> list[dict[str, Any]]:
    """Build persisted timestamp ranges in transcript order for audit and UI source seeking."""

    selected_ids = set(source_segment_ids)
    return [
        {
            "segmentId": str(segment["id"]),
            "startMs": segment.get("startMs", 0),
            "endMs": segment.get("endMs", 0),
        }
        for segment in meeting.get("segments", [])
        if str(segment.get("id") or "") in selected_ids
    ]


def artifact_envelope(
    meeting: dict[str, Any],
    payload: dict[str, Any],
    *,
    source_segment_ids: list[str],
    source_transcript_revision: int | None = None,
    template_id: str = "",
) -> dict[str, Any]:
    """Capture generated content together with the immutable transcript revision that produced it.

    ``generatedContent`` is copied so response/UI changes cannot silently alter the captured model
    result. User modifications belong only in ``editedContent``; separating the layers lets stale
    results remain reviewable after transcript corrections instead of losing user work.
    """

    canonical_ids = _canonical_source_segment_ids(meeting, source_segment_ids)
    # Long-running model callers pass the revision captured before generation.  This explicit
    # provenance value prevents a later store reload from labeling old output with a newer revision.
    provenance_revision = (
        int(meeting.get("transcriptRevision", 0))
        if source_transcript_revision is None
        else int(source_transcript_revision)
    )
    return {
        "status": "current",
        "sourceTranscriptRevision": provenance_revision,
        "sourceSegmentIds": canonical_ids,
        "sourceRanges": _source_ranges(meeting, canonical_ids),
        "templateId": template_id or "",
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generatedContent": deepcopy(payload),
        "editedContent": None,
    }


def draft_artifact_envelope(
    meeting: dict[str, Any],
    *,
    source_segment_ids: list[str],
    template_id: str = "",
) -> dict[str, Any]:
    """Capture a human draft without inventing a model-generated content layer.

    A draft may have been written while looking at the current transcript, so it can retain
    canonical source context for later invalidation. Its ``draft`` status and null generation
    fields are deliberate: neither the meeting's demo defaults nor a user's text are evidence
    that a model generated an artifact at this revision.
    """

    canonical_ids = _canonical_source_segment_ids(meeting, source_segment_ids)
    return {
        "status": "draft",
        "sourceTranscriptRevision": int(meeting.get("transcriptRevision", 0)),
        "sourceSegmentIds": canonical_ids,
        "sourceRanges": _source_ranges(meeting, canonical_ids),
        "templateId": template_id or "",
        "generatedAt": None,
        "generatedContent": None,
        "editedContent": None,
    }


def unlinked_artifact_envelope(meeting: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Represent a manually placed marker that intentionally has no transcript source.

    Unlinked markers are useful for general notes, but must never look like a current derived
    artifact. They therefore have no source revision or ranges and do not participate in stale
    transitions; attaching a source later requires creating a new, explicitly linked marker.
    """

    return {
        "status": "unlinked",
        "sourceTranscriptRevision": None,
        "sourceSegmentIds": [],
        "sourceRanges": [],
        "templateId": "",
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generatedContent": deepcopy(payload),
        "editedContent": None,
    }


def _refresh_one_artifact(meeting: dict[str, Any], artifact: dict[str, Any]) -> None:
    """Mark an old artifact stale without changing generated or manually edited content."""

    if artifact.get("status") == "unlinked":
        # An unlinked marker deliberately has no canonical transcript provenance. A later
        # transcript revision cannot make it stale because it never claimed to be current.
        return
    if artifact.get("sourceTranscriptRevision") != int(meeting.get("transcriptRevision", 0)):
        # The stale transition intentionally changes only status. Deleting either content layer
        # would remove evidence of the old generation or discard a person's edited meeting notes.
        artifact["status"] = "stale"


def refresh_artifact_states(meeting: dict[str, Any]) -> dict[str, Any]:
    """Apply transcript-revision invalidation to every persisted artifact in place.

    A stale artifact never becomes current through this refresh. Only a new generation may claim
    the current revision, preserving honest provenance when a transcript is edited repeatedly.
    """

    for field in SINGLE_ARTIFACT_FIELDS:
        artifact = meeting.get(field)
        if isinstance(artifact, dict):
            _refresh_one_artifact(meeting, artifact)
    for field in MULTIPLE_ARTIFACT_FIELDS:
        for artifact in meeting.get(field, []):
            if isinstance(artifact, dict):
                _refresh_one_artifact(meeting, artifact)
    return meeting
