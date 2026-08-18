import argparse

from marketgpr.commands.collect import register_args as register_collect, run as run_collect
from marketgpr.commands.clean import register_args as register_clean, run as run_clean
from marketgpr.commands.enrich import register_args as register_enrich, run as run_enrich
from marketgpr.commands.info import register_args as register_info, run as run_info
from marketgpr.commands.index import register_args as register_index, run as run_index
from marketgpr.commands.embed import register_args as register_embed, run as run_embed
from marketgpr.commands.search import register_args as register_search, run as run_search


def main():
    parser = argparse.ArgumentParser(
        prog="marketgpr",
        description="MarketGPR — Kalshi prediction-market contract tools for geopolitical risk research.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    register_collect(sub.add_parser("collect", help="Build contract catalog from Kalshi API"))
    register_clean(sub.add_parser("clean", help="Filter a contract catalog by keywords and regex"))
    register_enrich(sub.add_parser("enrich", help="Enrich contract names with event titles from the Kalshi API"))
    register_info(sub.add_parser("info", help="Inspect a contract database — schema, rows, enrichment status"))
    register_index(sub.add_parser("index", help="Sync the semantic-search name pool from a contracts DB"))
    register_embed(sub.add_parser("embed", help="Compute embeddings for pending names (GPU-heavy)"))
    register_search(sub.add_parser("search", help="Hybrid semantic + keyword search over contract names"))

    args = parser.parse_args()

    if args.command == "collect":
        run_collect(args)
    elif args.command == "clean":
        run_clean(args)
    elif args.command == "enrich":
        run_enrich(args)
    elif args.command == "info":
        run_info(args)
    elif args.command == "index":
        run_index(args)
    elif args.command == "embed":
        run_embed(args)
    elif args.command == "search":
        run_search(args)
