"""SQLAlchemy-specific models used by core facilities (temporary location).

Note: These models are infrastructure-only (e.g., outbox) and must not leak
into business modules directly. Repositories or services should be injected.
"""

from sandloom.core.sql_alchemy.models.consumer_inbox import (
    ConsumerInboxEntry as ConsumerInboxEntry,
)
from sandloom.core.sql_alchemy.models.contract_command import (
    ContractCommandRecord as ContractCommandRecord,
)
from sandloom.core.sql_alchemy.models.http_idempotency import (
    HttpIdempotencyRecord as HttpIdempotencyRecord,
)
from sandloom.core.sql_alchemy.models.outbox import (
    OutboxMessage as OutboxMessage,
)
from sandloom.core.sql_alchemy.models.projection_watermark import (
    ProjectionWatermark as ProjectionWatermark,
)
from sandloom.core.sql_alchemy.models.saga import (
    SagaInstance as SagaInstance,
)
from sandloom.core.sql_alchemy.models.saga import (
    SagaStepRecord as SagaStepRecord,
)

__all__ = [
    "OutboxMessage",
    "ConsumerInboxEntry",
    "ContractCommandRecord",
    "HttpIdempotencyRecord",
    "SagaInstance",
    "SagaStepRecord",
    "ProjectionWatermark",
]
