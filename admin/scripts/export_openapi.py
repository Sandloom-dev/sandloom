"""Export the admin's public contracts without starting services or exposing docs."""
import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / '.venv/bin/python'
if Path(sys.prefix).resolve() != (ROOT / '.venv').resolve():
    raise SystemExit(subprocess.call([str(PYTHON), __file__, *sys.argv[1:]], cwd=ROOT))

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
sys.path.insert(0, str(ROOT / 'src'))
os.environ['SANDLOOM_MODULES'] = '["sandloom_identity", "sandloom_tenancy", "sandloom_billing"]'
os.environ['ENVIRONMENT'] = 'dev'
os.environ['BILLING_PROVIDER'] = 'fake'
# Settings require connection URLs, but export never connects (no lifespan):
# inert defaults keep a fresh checkout / CI runner without a .env working.
for _name, _value in (
    ('DATABASE_URL', 'postgresql+asyncpg://export:export@127.0.0.1:9/export'),
    ('CACHE_URL', 'redis://127.0.0.1:9/0'),
    ('BROKER_URL', 'redis://127.0.0.1:9/0'),
    # Export mints and verifies nothing; a throwaway HS256 secret satisfies Settings.
    ('JWT_SECRET', 'admin-contract-export-only-' + 'x' * 32),
):
    os.environ.setdefault(_name, _value)
# Import only constructs the app. No lifespan, DB connection or migrations run.
from sandloom.core.composition import load_composition_app
from sandloom.core.admin import compile_app
app = load_composition_app('sandloom.main')
manifest = compile_app(app)
schema = app.openapi()
schema['paths'] = {p: v for p, v in schema['paths'].items()
                   if p.startswith(('/auth/', '/api/v1/'))}
assert '/api/v1/orgs/{org_id}/membership' in schema['paths'], 'Required tenancy API is missing'
assert '/auth/login' in schema['paths'] and '/api/v1/users' in schema['paths']
# Keep only DTOs reachable from the exported public routes.
def references(value):
    if isinstance(value, dict):
        ref = value.get('$ref', '')
        if ref.startswith('#/components/schemas/'):
            yield ref.rsplit('/', 1)[-1]
        for child in value.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


schemas = schema.get('components', {}).get('schemas', {})
pending = set(references(schema['paths']))
used = set()
while pending:
    name = pending.pop()
    if name not in used:
        used.add(name)
        pending.update(set(references(schemas[name])) - used)
schema['components']['schemas'] = {name: schemas[name] for name in sorted(used)}
schema.pop('servers', None)
payload = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
target = ROOT / 'admin/contracts/openapi.json'
if args.check:
    if not target.exists() or target.read_text() != payload:
        raise SystemExit('Admin API snapshot is stale: run npm run contracts in admin/')
else:
    target.parent.mkdir(exist_ok=True)
    target.write_text(payload)

manifest_target = ROOT / 'admin/public/admin-manifest.json'
manifest_payload = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
if args.check:
    if not manifest_target.exists() or manifest_target.read_text() != manifest_payload:
        raise SystemExit('Admin manifest is stale: run npm run contracts in admin/')
else:
    manifest_target.write_text(manifest_payload)
