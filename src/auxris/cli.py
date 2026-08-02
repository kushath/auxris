"""Command-line interface for local catalogs and the optional API server."""

from __future__ import annotations

import json
import os
import secrets
import uuid
from pathlib import Path
from typing import Annotated

import typer
from platformdirs import user_data_path
from rich.console import Console
from rich.table import Table

from auxris import __version__
from auxris.core.index import Catalog
from auxris.core.models import Asset

app = typer.Typer(
    name="auxris",
    help="Auditable audio provenance search for catalogs you control.",
    no_args_is_help=True,
)
console = Console()
DEFAULT_DB = user_data_path("auxris") / "catalog.sqlite3"


def _catalog(path: Path) -> Catalog:
    return Catalog(path.expanduser().resolve())


@app.command("init")
def initialize(
    database: Annotated[Path, typer.Option("--db", help="Catalog database path")] = DEFAULT_DB,
) -> None:
    """Initialize a durable local catalog."""

    catalog = _catalog(database)
    console.print(f"[green]Catalog ready[/green] at {catalog.path}")


@app.command("index")
def index_audio(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    asset_id: Annotated[str | None, typer.Option("--asset-id")] = None,
    title: Annotated[str | None, typer.Option()] = None,
    creator: Annotated[str | None, typer.Option()] = None,
    license_id: Annotated[str | None, typer.Option("--license")] = None,
    source_uri: Annotated[str | None, typer.Option()] = None,
    c2pa_manifest_uri: Annotated[str | None, typer.Option()] = None,
    allowed_use: Annotated[list[str] | None, typer.Option("--allow")] = None,
    database: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    """Index one reference asset and its asserted provenance."""

    identifier = asset_id or str(uuid.uuid5(uuid.NAMESPACE_URL, str(audio.resolve())))
    result = _catalog(database).index_path(
        audio,
        Asset(
            asset_id=identifier,
            title=title or audio.stem,
            creator=creator,
            license_id=license_id,
            source_uri=source_uri,
            c2pa_manifest_uri=c2pa_manifest_uri,
            allowed_uses=tuple(allowed_use or []),
        ),
    )
    console.print_json(json.dumps(result))


@app.command("scan")
def scan_audio(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    database: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    intended_use: Annotated[str | None, typer.Option("--use")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Find registered material within an audio file."""

    matches = _catalog(database).identify_path(audio, intended_use=intended_use)
    payload = {"matched": bool(matches), "matches": [match.to_dict() for match in matches]}
    if json_output:
        console.print_json(json.dumps(payload))
        return
    if not matches:
        console.print("[yellow]No registered material found.[/yellow]")
        raise typer.Exit(code=2)
    table = Table("Asset", "Title", "Score", "Votes", "Policy")
    for match in matches:
        table.add_row(
            match.asset.asset_id,
            match.asset.title or "—",
            f"{match.score:.3f}",
            str(match.evidence.aligned_votes),
            match.policy.decision,
        )
    console.print(table)


@app.command("catalog")
def catalog_stats(
    database: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    """Show catalog statistics."""

    console.print_json(json.dumps(_catalog(database).stats()))


@app.command("doctor")
def doctor(
    database: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    """Check runtime and catalog readiness without changing data."""

    stats = _catalog(database).stats()
    console.print(f"[green]Auxris {__version__}[/green]")
    console.print(f"Catalog: {database.expanduser().resolve()}")
    console.print(f"Assets: {stats['assets']}; fingerprints: {stats['fingerprints']}")
    if stats["assets"] == 0:
        console.print("[yellow]Catalog is empty; index an asset before scanning.[/yellow]")


@app.command("serve")
def serve(
    database: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8000,
    api_key: Annotated[str | None, typer.Option(envvar="AUXRIS_API_KEY")] = None,
) -> None:
    """Run the optional authenticated API and local web console."""

    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter("Install the server extra: uv sync --extra server") from exc

    key = api_key or secrets.token_urlsafe(32)
    if api_key is None:
        console.print(f"Generated development API key: [bold]{key}[/bold]")
    os.environ["AUXRIS_API_KEY"] = key
    os.environ["AUXRIS_DB_PATH"] = str(database.expanduser().resolve())
    uvicorn.run("auxris.server.app:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    app()
