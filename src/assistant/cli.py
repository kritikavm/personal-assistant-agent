"""Command-line entry point. M0: --help only; commands arrive in later milestones."""

import typer

from assistant import __version__

app = typer.Typer(
    help="Local personal assistant: morning brief, reminders, task capture and Q&A.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Personal assistant agent (Phase 1)."""


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)
