"""Run six synthetic API workflows. No model keys, external HTTP or persistent service."""
import argparse
import json
from pathlib import Path
import tempfile
import sys

from .harness import cases, run_case


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory(prefix='juli-m08-synthetic-') as folder:
        for case in cases():
            for missing in (False, True):
                rows.append(run_case(Path(folder)/f'{case["id"]}-{missing}.sqlite3', case, missing=missing))
    print(json.dumps({'mode': 'offline_synthetic_scenario_acceptance', 'real_model_calls': 0,
        'analysis_quality_evaluated': False, 'transport': 'in_process_http_testclient',
        'results': rows}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
