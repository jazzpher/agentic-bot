"""TaskForge command line interface.

Usage:
    python -m taskforge serve --db PATH --port N
    python -m taskforge add --db PATH --title TEXT [--due RFC3339]
    python -m taskforge list --db PATH [--done true|false] [--due-before RFC3339] [--archived true|false]
"""

import argparse
import sys

from .db import connect, create_task, init_db, list_tasks, parse_due
from .server import serve


def cmd_add(args):
    conn = connect(args.db)
    try:
        init_db(conn)
        task_id = create_task(conn, args.title, due=args.due)
    finally:
        conn.close()
    print(task_id)


def cmd_list(args):
    conn = connect(args.db)
    try:
        init_db(conn)
        done = None
        if args.done is not None:
            done = args.done == "true"
        due_before = None
        if args.due_before is not None:
            due_before = parse_due(args.due_before)
        archived = None
        if args.archived is not None:
            archived = args.archived == "true"
        tasks = list_tasks(conn, done=done, due_before=due_before, archived=archived)
    finally:
        conn.close()
    for task in tasks:
        print(task["title"])


def do_serve(args):
    serve(args.db, args.port)


def _bool_tri(value):
    """Parse a --archived/--done style 'true'|'false' string."""
    if value is None:
        return None
    if value == "true":
        return "true"
    if value == "false":
        return "false"
    raise argparse.ArgumentTypeError("must be 'true' or 'false'")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="taskforge", description="TaskForge task service"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="serve the HTTP API on 127.0.0.1")
    p_serve.add_argument("--db", required=True, help="path to SQLite database file")
    p_serve.add_argument("--port", required=True, type=int, help="TCP port to bind")
    p_serve.set_defaults(func=do_serve)

    p_add = sub.add_parser("add", help="add a task")
    p_add.add_argument("--db", required=True, help="path to SQLite database file")
    p_add.add_argument("--title", required=True, help="task title")
    p_add.add_argument(
        "--due", help="optional RFC3339 due timestamp (e.g. 2026-12-31T23:59:59Z)"
    )
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="list task titles")
    p_list.add_argument("--db", required=True, help="path to SQLite database file")
    p_list.add_argument("--done", help="filter by done: 'true' or 'false'")
    p_list.add_argument(
        "--due-before", help="filter tasks due strictly before this RFC3339 timestamp"
    )
    p_list.add_argument(
        "--archived", help="filter by archived: 'true' or 'false'"
    )
    p_list.set_defaults(func=cmd_list)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
