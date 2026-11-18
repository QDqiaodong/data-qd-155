"""queue-observe command line: cross-device clock log reconstruction."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from queue_replay.common import parse_bytes, save

from .report import observe

DESCRIPTION = ('Reconstruct provable cross-device wait/service/sojourn '
               'intervals from arrival, start and finish logs')
EXIT_ERROR = 2
BYTE_LIMIT = 8_000_000


def _read(path, label):
    with Path(path).open('rb') as stream:
        raw = stream.read(BYTE_LIMIT + 1)
    if len(raw) > BYTE_LIMIT:
        raise ValueError('%s JSON exceeds 8 MB' % label)
    return parse_bytes(raw), hashlib.sha256(raw).hexdigest()


def run(source_path, baseline_path):
    """Read both inputs, verify the baseline, and return a report.

    Both inputs share the 8 MB byte limit and the strict duplicate-key JSON
    reader.  The report digests are computed over the actual input bytes.
    """
    source_data, log_digest = _read(source_path, 'source')
    baseline_data, baseline_digest = _read(baseline_path, 'baseline')
    return observe(source_data, log_digest, baseline_data, baseline_digest)


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
                        help='observation LOG.json')
    parser.add_argument('--baseline', type=Path, required=True,
                        help='queue-replay REPORT.json')
    parser.add_argument('--output', type=Path, required=True,
                        help='destination report path')
    args = parser.parse_args(argv)
    try:
        _same_file(args.source, args.output, 'input and output')
        _same_file(args.baseline, args.output, 'input and output')
        _same_file(args.source, args.baseline, 'source and baseline')
        if args.output.is_symlink():
            raise ValueError('output cannot be a symlink')
        if args.output.exists():
            output_id = _device(args.output)
            if output_id in (_device(args.source), _device(args.baseline)):
                raise ValueError('output cannot alias an input file')
        report = run(args.source, args.baseline)
        # Re-check after reading so a swap during the read is still caught.
        if args.output.exists():
            output_id = _device(args.output)
            if output_id in (_device(args.source), _device(args.baseline)):
                raise ValueError('output cannot alias an input file')
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
