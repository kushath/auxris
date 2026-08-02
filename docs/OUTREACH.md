# Design-partner outreach kit

Do not send private audio, customer names, or unmeasured accuracy claims in initial outreach. The goal is a discovery call and a legally approved evaluation—not a promise that Auxris already solves global copyright detection.

## Target roles

- Head of data or trust and safety at an AI-audio company.
- Content operations lead at a music, sample, or stock-media marketplace.
- Audio tools or machine-learning lead at a game/film studio.
- Dataset governance lead at a research organization.
- Digital asset management lead at an archive or media company.

## Initial email

**Subject:** Open-source audio provenance pilot for your private catalog

Hi {{name}},

We are building Auxris, an open-source engine that finds registered source material inside new audio and returns aligned, inspectable evidence. It is designed for catalogs an organization controls—for example licensed samples, training assets, masters, stems, or consented voice recordings.

Auxris does not claim to decide copyright or identify every song. A pilot asks a narrower question: can deterministic fingerprint evidence help your team review dataset inputs or generated outputs against assets whose provenance you already know?

We are looking for a small number of design partners with an approved reference set and known positive/negative examples. The evaluation can run inside your environment; the public project does not require uploading your original media.

Would a 25-minute discovery call be useful? We would like to understand the review decision, transformations that matter, and what evidence your team would need to act.

Repository: https://github.com/satoshi-kris/auxris

Best,  
{{sender}}

## Discovery questions

1. What catalog do you already control, and who owns its provenance metadata?
2. What event triggers review: dataset import, generation, upload, or publication?
3. Which transformations cause present systems to miss reuse?
4. What is the cost of a false positive and a false negative?
5. Must original audio remain in your environment?
6. What output would allow a reviewer or automated workflow to act safely?
7. Can approved fixtures be retained for regression testing, and under what terms?

## Pilot acceptance criteria

Agree on the catalog size, authorized fixture transfer, retention/deletion, transformations, known negatives, evaluation metrics, reviewer workflow, and publication boundary before processing data. Never fold a partner's private media into the open benchmark without a separate explicit license.
