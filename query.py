#!/usr/bin/env python3
"""Query the cost-telemetry server's /summary endpoint.

Run:
    python3 query.py http://127.0.0.1:8000 --at 2026-09
    python3 query.py http://127.0.0.1:8000 --from 2026-09 --to 2026-09 -s session-a
    python3 query.py http://127.0.0.1:8000 --at 2026-09-07 --tz Asia/Shanghai --json
"""

# Stdlib-only client: argparse for flags, urllib for GET, json for output.
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


class PrettyPrinter:
    """Static helpers to pretty-print summary rows nested by session."""

    @staticmethod
    def print_stats(d, indent=0):
        """Print the turns/in/out/etc. lines for one bucket.

        Reads a dict with keys: turns, inTokens, outTokens, cacheRead,
        cacheWrite, totalTokens, reasoningTokens, costIn, costOut,
        costCacheRead, costCacheWrite, costTotal. Each line
        is prefixed with `indent` spaces.
        """
        pad = " " * indent
        specs = [
            ("turns", d['turns'], PrettyPrinter.fmt_tok),
            ("tokens", d['totalTokens'], PrettyPrinter.fmt_tok),
            ("  in", d['inTokens'], PrettyPrinter.fmt_tok),
            ("  out", d['outTokens'], PrettyPrinter.fmt_tok),
            ("    reasoning", d['reasoningTokens'], PrettyPrinter.fmt_tok),
            ("  cache read", d['cacheRead'], PrettyPrinter.fmt_tok),
            ("  cache write", d['cacheWrite'], PrettyPrinter.fmt_tok),
            ("cost", d['costTotal'], PrettyPrinter.fmt_cost),
            ("  input", d['costIn'], PrettyPrinter.fmt_cost),
            ("  output", d['costOut'], PrettyPrinter.fmt_cost),
            ("  cache read", d['costCacheRead'], PrettyPrinter.fmt_cost),
            ("  cache write", d['costCacheWrite'], PrettyPrinter.fmt_cost),
        ]
        width = max(len(label) for label, _, _ in specs)
        shown = False
        for label, value, fmt in specs:
            # hide zero values
            if value == 0:
                continue
            print(f"{pad}{label:<{width}} : {fmt(value)}")
            shown = True
        if not shown:
            print(f"{pad}(no data)")

    @staticmethod
    def fmt_tok(n):
        """Format an integer with thousands separators."""
        return f"{n:,}"

    @staticmethod
    def fmt_cost(v):
        """Format a cost as $X.XX with thousands separators."""
        return f"${v:,.2f}"

    @staticmethod
    def print_summary(rows):
        """Pretty-print rows nested by session, then a grand TOTAL block."""
        # Bucket rows by session, preserving first-seen order.
        groups = {}
        for r in rows:
            groups.setdefault(r["session"], []).append(r)
        # Accumulators for the final TOTAL block.
        total_turns = total_in = total_out = total_cr = total_cw = total_tok = total_reas = 0
        total_cost_in = total_cost_out = total_cost = total_cost_cr = total_cost_cw = 0.0
        # Print one nested block per session: model rows indented under it.
        for session, srows in groups.items():
            print("session:", session)
            for r in srows:
                print(f"  model: {r['model']}")
                PrettyPrinter.print_stats(r, indent=4)
            total_turns += sum(r["turns"] for r in srows)
            total_in += sum(r["inTokens"] for r in srows)
            total_out += sum(r["outTokens"] for r in srows)
            total_cr += sum(r["cacheRead"] for r in srows)
            total_cw += sum(r["cacheWrite"] for r in srows)
            total_tok += sum(r["totalTokens"] for r in srows)
            total_reas += sum(r.get("reasoningTokens", 0) for r in srows)
            total_cost_in += sum(r["costIn"] for r in srows)
            total_cost_out += sum(r["costOut"] for r in srows)
            total_cost_cr += sum(r.get("costCacheRead", 0) for r in srows)
            total_cost_cw += sum(r.get("costCacheWrite", 0) for r in srows)
            total_cost += sum(r["costTotal"] for r in srows)
        # Print grand totals across all sessions, in the same nested style.
        print("\n---")
        print("GRAND TOTAL")
        PrettyPrinter.print_stats({
            "turns": total_turns,
            "inTokens": total_in,
            "outTokens": total_out,
            "cacheRead": total_cr,
            "cacheWrite": total_cw,
            "totalTokens": total_tok,
            "reasoningTokens": total_reas,
            "costIn": total_cost_in,
            "costOut": total_cost_out,
            "costCacheRead": total_cost_cr,
            "costCacheWrite": total_cost_cw,
            "costTotal": total_cost,
        }, indent=2)


def main():
    """Fetch /summary rows and print them as a table (or raw JSON).

    Parses CLI flags, builds the /summary URL, GETs it with an
    optional Bearer key, then renders rows with totals.

    Returns:
        int: Process exit code (0 on success, 1 on HTTP error).
    """
    # Define CLI flags mirroring /summary query params.
    parser = argparse.ArgumentParser(description="Query cost-telemetry summary.")
    parser.add_argument("server", help="server base URL")
    parser.add_argument("-s", "--session", default=None, help="filter by session")
    parser.add_argument("-m", "--model", default=None, help="filter by model")
    parser.add_argument("--at", default=None, help="YYYY, YYYY-MM, or YYYY-MM-DD")
    parser.add_argument("--from", dest="from_", default=None, help="start period (YYYY, YYYY-MM, YYYY-MM-DD)")
    parser.add_argument("--to", default=None, help="last period to include")
    parser.add_argument("--tz", default=None, help="IANA name, e.g. Asia/Shanghai")
    parser.add_argument("-k", "--key", default=None, help="API key (or TELEMETRY_KEY env)")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    args = parser.parse_args()

    # Collect only set filters so unset flags are omitted from the URL.
    # CLI uses from/to; server expects since/until as query params.
    params = {}
    if args.session is not None:
        params["session"] = args.session
    if args.model is not None:
        params["model"] = args.model
    if args.at is not None:
        params["at"] = args.at
    if args.from_ is not None:
        params["since"] = args.from_
    if args.to is not None:
        params["until"] = args.to
    if args.tz is not None:
        params["tz"] = args.tz
    # Build the GET URL, normalizing any trailing slash on the server arg.
    url = args.server.rstrip("/") + "/summary?" + urllib.parse.urlencode(params)
    # Prefer --key, fall back to TELEMETRY_KEY env var for convenience.
    key = args.key or os.environ.get("TELEMETRY_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    # Fetch and decode the summary payload.
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Server returned 4xx/5xx: show its JSON error body when possible.
        try:
            print(f"error {e.code}: {json.loads(e.read())}", file=sys.stderr)
        except ValueError:
            print(f"error {e.code}", file=sys.stderr)
        return 1

    # Raw mode: dump the response untouched and exit early.
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    # Empty result: nothing to tabulate.
    rows = data.get("rows", [])
    if not rows:
        print("no data")
        return 0
    # Nested pretty-print by session/model.
    PrettyPrinter.print_summary(rows)
    return 0


if __name__ == "__main__":
    # Entry point: forward main()'s exit code to the shell.
    sys.exit(main())
