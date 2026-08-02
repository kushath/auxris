"""Create a deterministic, synthetic demo catalog without distributing third-party audio."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import chirp

from auxris.core.fingerprint import FingerprintConfig
from auxris.core.index import Catalog
from auxris.core.models import Asset


def main() -> None:
    output = Path("demo")
    output.mkdir(exist_ok=True)
    config = FingerprintConfig()
    duration = 12
    time = np.arange(config.sample_rate * duration) / config.sample_rate
    source = (
        0.35 * chirp(time, f0=180, f1=1900, t1=duration, method="quadratic")
        + 0.18 * np.sin(2 * np.pi * 443 * time)
        + 0.12 * np.sin(2 * np.pi * (220 + 12 * time) * time)
    ).astype(np.float32)
    source_path = output / "synthetic-source.wav"
    query_path = output / "synthetic-query.wav"
    sf.write(source_path, source, config.sample_rate, subtype="PCM_16")
    start = 40 * config.hop_length
    sf.write(
        query_path,
        source[start : start + 5 * config.sample_rate] * 0.7,
        config.sample_rate,
        subtype="PCM_16",
    )

    database = output / "auxris-demo.sqlite3"
    catalog = Catalog(database, config=config)
    catalog.index_path(
        source_path,
        Asset(
            asset_id="demo:synthetic:001",
            title="Synthetic provenance fixture",
            creator="Auxris",
            license_id="CC0-1.0",
            allowed_uses=("research", "commercial-release"),
            metadata={"generated": True, "purpose": "deterministic demonstration"},
        ),
    )
    matches = catalog.identify_path(query_path, intended_use="research")
    print(f"Created {database} and identified {len(matches)} matching asset(s).")
    if matches:
        print(matches[0].to_dict())


if __name__ == "__main__":
    main()
