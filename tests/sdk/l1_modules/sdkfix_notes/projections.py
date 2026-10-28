"""A projection over the module's own note events (spec §6.4)."""

from __future__ import annotations

from sandloom.sdk.projections import (
    Projection,
    SourceGroup,
    delta,
    full_state,
    tombstone,
)

from .events import NoteDeleted, NoteLiked, NoteSaved
from .models import NoteIndex

NOTE_INDEX = Projection(
    "sdkfix_notes.index",
    key=("tenant_id", "note_id"),
    model=NoteIndex,
    groups=[
        SourceGroup(
            events={
                NoteSaved: full_state(lambda e: {"title": e.title}),
                NoteDeleted: tombstone(lambda e: {}),
            },
            aggregate_key="note_id",
            version_field="version",
            version_column="note_version",
            owns=("title",),
        ),
        SourceGroup(
            events={NoteLiked: delta(lambda e: {"likes": 1})},
            aggregate_key="note_id",
            version_field="version",
            version_column="like_version",
            owns=("likes",),
        ),
    ],
)
