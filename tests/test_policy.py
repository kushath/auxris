from auxris.core.models import Asset
from auxris.core.policy import evaluate_policy


def test_policy_is_explicit_about_unknown_rules() -> None:
    asset = Asset(asset_id="asset")
    assert evaluate_policy(asset, None).decision == "not_evaluated"
    assert evaluate_policy(asset, "research").decision == "review_required"


def test_policy_uses_registered_allowed_uses() -> None:
    asset = Asset(asset_id="asset", allowed_uses=("research",))
    assert evaluate_policy(asset, "research").decision == "allowed"
    assert evaluate_policy(asset, "commercial-release").decision == "not_allowed"
