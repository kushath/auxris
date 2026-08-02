"""Domain models kept independent from the API framework."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Asset:
    """A catalog asset and its externally asserted provenance metadata."""

    asset_id: str
    title: str | None = None
    creator: str | None = None
    license_id: str | None = None
    source_uri: str | None = None
    c2pa_manifest_uri: str | None = None
    allowed_uses: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A stable landmark pair at an anchor frame."""

    hash_bytes: bytes
    frame: int


@dataclass(frozen=True, slots=True)
class MatchEvidence:
    """Auditable evidence supporting a catalog match."""

    aligned_votes: int
    query_fingerprints: int
    coverage: float
    offset_frames: int
    query_start_seconds: float
    query_end_seconds: float
    source_start_seconds: float
    source_end_seconds: float


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: Literal["allowed", "review_required", "not_allowed", "not_evaluated"]
    reason: str


@dataclass(frozen=True, slots=True)
class Match:
    asset: Asset
    score: float
    evidence: MatchEvidence
    policy: PolicyResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset.to_dict(),
            "score": self.score,
            "evidence": asdict(self.evidence),
            "policy": asdict(self.policy),
        }
