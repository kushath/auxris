"""Small, explicit policy evaluator; it does not make legal determinations."""

from auxris.core.models import Asset, PolicyResult


def evaluate_policy(asset: Asset, intended_use: str | None) -> PolicyResult:
    if intended_use is None:
        return PolicyResult("not_evaluated", "No intended use was supplied")
    if not asset.allowed_uses:
        return PolicyResult("review_required", "The catalog has no machine-readable usage policy")
    if intended_use in asset.allowed_uses:
        return PolicyResult("allowed", f"The catalog permits the '{intended_use}' use")
    return PolicyResult(
        "not_allowed",
        f"The catalog does not list '{intended_use}' as an allowed use",
    )
