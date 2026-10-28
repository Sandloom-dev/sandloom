"""The notes.board projection (spec §6.4).

Full-state events of one aggregate, versioned tombstones, one owning group
per column.
"""

from uuid import UUID

from sandloom.sdk import Event
from sandloom.sdk.projections import (  # sandloom: advanced projections
    Projection,
    SourceGroup,
    full_state,
    tombstone,
)

from .models.board_row import BoardRow


class BoardSaved(Event, channel="notes.board.saved.v1"):
    board_id: UUID
    tenant_id: UUID
    title: str
    version: int


class BoardDeleted(Event, channel="notes.board.deleted.v1"):
    board_id: UUID
    tenant_id: UUID
    version: int


BOARD = Projection(
    "notes.board",
    key=("tenant_id", "board_id"),
    model=BoardRow,
    groups=[
        SourceGroup(
            events={
                BoardSaved: full_state(lambda e: {"title": e.title}),
                BoardDeleted: tombstone(lambda e: {}),
            },
            aggregate_key="board_id",
            version_field="version",
            version_column="version",
            owns=("title",),
        )
    ],
)
