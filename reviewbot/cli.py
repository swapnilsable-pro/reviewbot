"""ReviewBot CLI — `reviewbot review`, `reviewbot test-connection`, `reviewbot version`."""

from __future__ import annotations

import os
import sys

import typer

from reviewbot import __version__
from reviewbot.config import ConfigError, load_config

app = typer.Typer(
    name="reviewbot",
    help="Automated PR code review with a free LLM — inline comments via GitHub Actions.",
    no_args_is_help=True,
)


def _require_env(name: str, hint: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        typer.secho(f"Error: {name} is not set. {hint}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    return value


@app.command()
def review(
    repo: str = typer.Option(
        None,
        "--repo",
        help="Repository as owner/name. Defaults to $GITHUB_REPOSITORY (set in Actions).",
    ),
    pr: int = typer.Option(
        None,
        "--pr",
        help="Pull request number. Defaults to the number in the Actions event payload.",
    ),
    config_path: str = typer.Option(
        None, "--config", help="Path to reviewbot.yml (default: ./reviewbot.yml)."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run the review but print findings instead of posting to GitHub.",
    ),
) -> None:
    """Review a pull request and post inline comments."""
    github_token = _require_env(
        "GITHUB_TOKEN",
        "In GitHub Actions, pass `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` in the step env.",
    )
    openrouter_key = _require_env(
        "OPENROUTER_API_KEY",
        "Get a free key at https://openrouter.ai/keys and add it to your repo secrets.",
    )

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    from reviewbot.runner import ReviewRunner

    runner = ReviewRunner(
        config=config,
        github_token=github_token,
        openrouter_api_key=openrouter_key,
    )
    exit_code = runner.run(repo=repo, pr_number=pr, dry_run=dry_run)
    raise typer.Exit(code=exit_code)


@app.command("test-connection")
def test_connection(
    config_path: str = typer.Option(
        None, "--config", help="Path to reviewbot.yml (default: ./reviewbot.yml)."
    ),
) -> None:
    """Verify the OpenRouter API key and model are working."""
    openrouter_key = _require_env(
        "OPENROUTER_API_KEY",
        "Get a free key at https://openrouter.ai/keys.",
    )
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    from reviewbot.reviewer import LLMReviewer

    reviewer = LLMReviewer(api_key=openrouter_key, model=config.model)
    typer.echo(f"Pinging OpenRouter with model {config.model} ...")
    try:
        reply = reviewer.ping()
    except Exception as exc:  # noqa: BLE001 — report any failure cleanly
        typer.secho(f"Connection failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"OK — model replied: {reply!r}", fg=typer.colors.GREEN)


@app.command()
def version() -> None:
    """Print the ReviewBot version."""
    typer.echo(f"reviewbot {__version__} (python {sys.version.split()[0]})")


if __name__ == "__main__":
    app()
