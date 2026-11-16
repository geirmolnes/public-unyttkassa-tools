"""CLI entrypoint and orchestration."""

import argparse
import asyncio
import sys
from pathlib import Path

from .drafter import draft_article
from .env import ensure_google_api_key as _ensure_google_api_key
from .maalform import detect_nynorsk
from .sources import load_and_format
from .storage import save_draft


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kladd",
        description="Lag utkast til nyhetsartikkel fra kildemateriale",
    )
    parser.add_argument(
        "sources",
        nargs="+",
        help="Kildefiler (PDF, markdown, tekst) eller URLer",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Skriv artikkelen til fil i stedet for stdout",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    loaded = load_and_format(args.sources)
    for source, error in loaded.dropped:
        print(f"Advarsel: Kunne ikke lese '{source}': {error}", file=sys.stderr)
    print(f"Genererer artikkel fra {loaded.count} kilde(r)...", file=sys.stderr)

    nynorsk = detect_nynorsk([s.markdown or "" for s in loaded.parsed])
    if nynorsk:
        print("Kildene er på nynorsk — skriver artikkelen på nynorsk.", file=sys.stderr)
    article, model_used = await draft_article(
        loaded.formatted, loaded.count, all_urls=loaded.all_urls, nynorsk=nynorsk
    )
    save_draft(article, model_used, loaded.parsed, user_email="cli")

    if args.output:
        args.output.write_text(article, encoding="utf-8")
        print(f"Artikkel skrevet til {args.output}", file=sys.stderr)
    else:
        print(article)


def main() -> None:
    try:
        _ensure_google_api_key()
        args = _parse_args()
        asyncio.run(_run(args))
    except (RuntimeError, ValueError) as e:
        print(f"Feil: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
