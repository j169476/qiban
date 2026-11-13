"""Compatibility entry for the original bounded file probe."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from companion_core.file_reader import Limits as Limits
from companion_core.file_reader import extract

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    started = time.perf_counter()
    result = extract(args.input)
    result['elapsed_s'] = time.perf_counter() - started
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
