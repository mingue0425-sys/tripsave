"""V0.8 source-to-canonical place integration layer.

This package is deliberately downstream of the V0.6/V0.7 source models.  It
does not replace raw source rows and it never turns an ambiguous candidate
into an automatic merge.
"""

from backend.entities.models import (
    CanonicalPlace,
    EntityMatchCandidate,
    EntityResolveRequest,
    EntityResolveResponse,
    EntityResolverStats,
    EntitySourceRecord,
    MatchDecision,
    MatchTrace,
    QuarantinedRecord,
    SourceMembership,
)
from backend.entities.repository import EntityResolutionRepository
from backend.entities.resolver import EntityResolver

__all__ = [
    "CanonicalPlace",
    "EntityMatchCandidate",
    "EntityResolutionRepository",
    "EntityResolveRequest",
    "EntityResolveResponse",
    "EntityResolver",
    "EntityResolverStats",
    "EntitySourceRecord",
    "MatchDecision",
    "MatchTrace",
    "QuarantinedRecord",
    "SourceMembership",
]
