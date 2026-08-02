# RFC-0003: Provenance, consent, and policy model

- Status: Proposed
- Target: v0.2

## Principle

Auxris separates facts supplied by an authority, detector observations, and organization-specific decisions. A provenance statement is attributable; a detector result is reproducible; a policy decision is explainable.

## Asset record

An `auxris.asset.v1` record includes a stable asset ID, media digest, title and creators, source URI, rights assertions, allowed uses, prohibited uses, territorial or temporal constraints, and references to credentials. Every assertion includes an issuer and may include issuance, expiry, evidence, and revocation information.

Auxris does not treat uploaded metadata as verified. `verification_state` is one of `unverified`, `self_asserted`, `verified`, `revoked`, or `unknown`.

## Consent vocabulary

Initial actions are `training`, `fine_tuning`, `evaluation`, `retrieval`, `voice_cloning`, `generation`, `distribution`, and `commercial_use`. Policies can extend the vocabulary using namespaced actions.

Consent for a work, recording, performer, or voice persona are separate scopes. Permission for distribution does not imply permission for model training or voice cloning.

## Policy evaluation

The evaluator accepts the requested action, subject, actor, jurisdiction/time context, evidence envelope, and a versioned policy bundle. It returns one decision, machine-readable reason codes, human-readable explanations, and obligations such as attribution, deletion, or manual review.

Policy evaluation is pure and deterministic for the same normalized inputs. External revocation and identity lookups happen before evaluation and are included as inputs.

## Standards mapping

The open model provides mappings rather than hard dependencies:

- C2PA assertions and ingredient relationships for content credentials;
- W3C PROV-O entities, activities, and agents for lineage;
- ODRL permissions, prohibitions, duties, and constraints for usage policy;
- Croissant dataset records for dataset-level discovery and metadata.

Lossless round trips are not promised until a mapping profile has its own conformance fixtures.

## Revocation and deletion

Registries record superseding statements instead of mutating signed history. Serving indexes must support prompt removal of revoked or deleted assets. Evidence that referenced a later-revoked assertion remains auditable and reports the current revocation state when revalidated.
