from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import llm, provider as provider_mod, store
from .machine import Brain


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(prog="python -m warden_brain", description="Warden's incident brain")
    ap.add_argument("--db", default=str(root / "warden.db"), help="the sensor's SQLite file")
    ap.add_argument("--reports", default=str(root / "reports"), help="where incident reports are written")
    ap.add_argument("--live", action="store_true", help="execute restarts; without this Warden runs dry")
    ap.add_argument("--once", action="store_true", help="handle pending events once and exit")
    ap.add_argument("--interval", type=float, default=15.0, help="seconds between passes")
    ap.add_argument("--model", default=os.environ.get("WARDEN_MODEL", "ollama:llama3.2"),
                    help="none, ollama:<model> or anthropic:<model>")
    ap.add_argument("--provider", default=os.environ.get("WARDEN_PROVIDER", ""),
                    help="vercel:<team slug> asks Vercel which projects are paused, once a minute")
    args = ap.parse_args(argv)

    try:
        model = llm.from_spec(args.model)
    except ValueError as e:
        print(f"warden-brain: {e}", file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print(f"warden-brain: {args.db} does not exist yet. Start the sensor first: "
              "warden-sensor run --config warden.toml", file=sys.stderr)
        return 2

    provider = None
    if args.provider:
        kind, _, scope = args.provider.partition(":")
        if kind != "vercel" or not scope:
            print("warden-brain: --provider must look like vercel:<team slug>", file=sys.stderr)
            return 2
        provider = provider_mod.Vercel(scope)
    conn = store.connect(args.db)
    brain = Brain(conn, Path(args.reports), model=model, live=args.live, provider=provider,
                  provider_every=max(1, round(60 / args.interval)))
    print(f"brain: {'LIVE' if args.live else 'dry run'}, model {args.model}, db {args.db}")
    while True:
        try:
            for r in brain.run_once():
                if r.get("incident_id") or r.get("verdict") != "healthy":
                    print(json.dumps(r, default=str))
        except sqlite3.OperationalError as e:
            # A long lock is a busy moment, not a reason to die; events stay unclaimed for the next pass.
            print(f"brain: pass skipped, database busy: {e}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
