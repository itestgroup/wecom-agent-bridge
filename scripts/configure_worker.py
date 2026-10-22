"""Create an ignored local Worker configuration; never deploy anything."""
import argparse
import json
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    destination = ROOT / 'wrangler.json'
    if args.check:
        if not destination.exists():
            raise ValueError('Run python scripts/configure_worker.py, then configure your own test resources')
        config = json.loads(destination.read_text())
        databases = config.get('d1_databases', [])
        if len(databases) != 1 or databases[0].get('binding') != 'DB':
            raise ValueError('Configure one D1 database bound as DB')
        identifier = databases[0].get('database_id', '')
        if uuid.UUID(identifier).int == 0:
            raise ValueError('Replace the example D1 database ID')
        if not config.get('name') or not databases[0].get('database_name'):
            raise ValueError('Set your Worker and database names')
        print('Local deployment configuration is complete. Verify the Cloudflare account and targets before deploying.')
    else:
        if destination.exists():
            raise ValueError('wrangler.json already exists; refusing to overwrite it')
        destination.write_bytes((ROOT / 'wrangler.example.json').read_bytes())
        print('Created ignored wrangler.json; replace its D1 ID with a NEW test database ID.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError) as error:
        raise SystemExit(str(error))
