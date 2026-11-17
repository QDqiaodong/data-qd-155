"""queue-planner command line: init, apply and plan subcommands."""
import argparse
import json
import sqlite3
import sys

from queue_replay.common import read, save

from . import service, store

DESCRIPTION = 'Persistent rolling dispatch workbench'
EXIT_ERROR = 2


def _fail(error):
    print(str(error), file=sys.stderr)
    return EXIT_ERROR


def _cmd_init(args):
    data, _digest = read(args.source)
    servers, now = service.validate_init(data)
    store.ensure_distinct_path(args.db, args.source)
    store.reject_symlink(args.db)
    store.initialize(args.db, servers, now)
    print(json.dumps({'servers': servers, 'now': now, 'revision': 0},
                     sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


def _cmd_apply(args):
    data, digest = read(args.source)
    store.ensure_distinct_path(args.db, args.source)
    conn = store.connect(args.db)
    try:
        receipt = service.apply_request(conn, data, digest)
    finally:
        conn.close()
    print(json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def _cmd_plan(args):
    store.ensure_distinct_path(args.db, args.output)
    conn = store.connect_readonly(args.db)
    try:
        report = service.build_report(conn)
    finally:
        conn.close()
    save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command', required=True)

    p_init = sub.add_parser('init', help='create a new workbench database')
    p_init.add_argument('--db', required=True)
    p_init.add_argument('--source', required=True)
    p_init.set_defaults(func=_cmd_init)

    p_apply = sub.add_parser('apply', help='apply one load/observe/start request')
    p_apply.add_argument('--db', required=True)
    p_apply.add_argument('--source', required=True)
    p_apply.set_defaults(func=_cmd_apply)

    p_plan = sub.add_parser('plan', help='publish the optimal plan report')
    p_plan.add_argument('--db', required=True)
    p_plan.add_argument('--output', required=True)
    p_plan.set_defaults(func=_cmd_plan)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, sqlite3.Error) as error:
        return _fail(error)


if __name__ == '__main__':
    raise SystemExit(main())
