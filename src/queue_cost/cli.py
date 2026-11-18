"""queue-cost command line: exact work-order and invoice cost accounting."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from queue_replay.common import parse_bytes, save

from .common import BYTE_LIMIT, SCHEMA_VERSION
from .model import analyze

DESCRIPTION = ('Split completed service intervals on contract rate periods '
               'and publish exact work-order costs with per-invoice totals')
EXIT_ERROR = 2


def _read(path, label='source'):
    with Path(path).open('rb') as stream:
        raw = stream.read(BYTE_LIMIT + 1)
    if len(raw) > BYTE_LIMIT:
        raise ValueError('%s JSON exceeds 8 MB' % label)
    return parse_bytes(raw), hashlib.sha256(raw).hexdigest()


def run(source_path):
    """Read a source document and return the full report dictionary.

    The document is parsed with the repository's strict duplicate-key reader,
    validated against the exact field contract, and priced exactly.  The
    digest covers the actual source bytes, so identical bytes always produce
    an identical report.
    """
    data, digest = _read(source_path)
    result = analyze(data)
    return {'schema_version': SCHEMA_VERSION,
            'source_sha256': digest,
            'work_orders': result['work_orders'],
            'invoices': result['invoices']}


def _same_file(left, right, what):
    if Path(left).resolve() == Path(right).resolve():
        raise ValueError('%s paths must differ' % what)


def _device(path):
    """Inode identity used to reject hard links and other aliases."""
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino)


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source', type=Path, required=True,
                        help='cost source JSON')
    parser.add_argument('--output', type=Path, required=True,
                        help='destination report path')
    args = parser.parse_args(argv)
    try:
        _same_file(args.source, args.output, 'input and output')
        if args.output.is_symlink():
            raise ValueError('output cannot be a symlink')
        if args.output.exists() and (_device(args.output)
                                     == _device(args.source)):
            raise ValueError('output cannot alias the input file')
        report = run(args.source)
        if args.output.exists() and (_device(args.output)
                                     == _device(args.source)):
            raise ValueError('output cannot alias the input file')
        save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
