"""Isolated smoke: the exact admin image targets two different API upstreams."""
import json
import re
import subprocess
import time
import uuid

IMAGE = 'sandloom-admin:local'
NODE = 'node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402'
run_id = 'sandloom-admin-smoke-' + uuid.uuid4().hex[:8]
containers = []


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def get(base, path, method='GET'):
    # Test traffic stays inside the private network; no published host ports.
    code = """const http=require('http');const req=http.request(process.argv[1],{
      method:process.argv[2],headers:{'X-Internal-Token':'synthetic-must-be-stripped',
      Authorization:'Bearer synthetic'}},res=>{let body='';res.on('data',chunk=>body+=chunk);
      res.on('end',()=>console.log(JSON.stringify({status:res.statusCode,headers:res.headers,body})))
      });req.setTimeout(1500,()=>req.destroy(new Error('timeout')));
      req.on('error',()=>process.exit(2));req.end()"""
    payload = json.loads(run('docker', 'exec', active_client, 'node', '-e', code, base + path, method))
    return payload['status'], {k.title(): v for k, v in payload['headers'].items()}, payload['body']


try:
    run('docker', 'network', 'create', '--internal', run_id)
    for topology in ('monolith', 'split'):
        upstream = run_id + '-' + topology
        containers.append(upstream)
        script = '''let count=0;require('http').createServer((req,res)=>{
          res.setHeader('Content-Type','application/json');
          if(req.url==='/api/v1/fail'){res.statusCode=502;count++}
          res.end(JSON.stringify({topology:process.env.TOPOLOGY,path:req.url,
           internal:!!req.headers['x-internal-token'],auth:req.headers.authorization,proto:req.headers['x-forwarded-proto'],count}))
        }).listen(9000,'0.0.0.0')'''
        run('docker', 'run', '-d', '--name', upstream, '--network', run_id,
            '-e', 'TOPOLOGY=' + topology, NODE, 'node', '-e', script)
        active_client = upstream
        name = run_id + '-ui-' + topology
        containers.append(name)
        run('docker', 'run', '-d', '--name', name, '--network', run_id,
            '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,mode=1777',
            '--tmpfs', '/etc/nginx/conf.d:rw,noexec,nosuid,uid=101,gid=101,mode=0755',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '-e', 'SANDLOOM_ADMIN_API_UPSTREAM=http://' + upstream + ':9000',
            '-e', 'SANDLOOM_ADMIN_PUBLIC_SCHEME=' + ('https' if topology == 'split' else 'http'),
            IMAGE)
        base = 'http://' + name + ':8080'
        for _ in range(50):
            try:
                if get(base, '/config.json')[0] == 200:
                    break
            except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
                pass
            time.sleep(.2)
        else:
            raise RuntimeError('Container failed to start: ' + run('docker', 'logs', name))
        info = json.loads(run('docker', 'inspect', name))[0]
        assert info['Config']['User'] not in ('', '0', 'root')
        status, headers, html = get(base, '/')
        assert status == 200 and 'Sandloom' in html
        assert 'frame-ancestors' in headers['Content-Security-Policy']
        assert headers['Cache-Control'] == 'no-store'
        status, manifest_headers, manifest_body = get(base, '/admin-manifest.json')
        manifest = json.loads(manifest_body)
        assert status == 200 and manifest['version'] == 1
        assert manifest_headers['Cache-Control'] == 'no-store'
        assert {'users', 'roles', 'permissions', 'organizations', 'billing_customers', 'billing_subscriptions', 'audit_identity', 'services_billing'} <= {r['name'] for r in manifest['resources']}
        asset = re.search(r'src="([^"]+\.js)"', html).group(1)
        status, headers, _ = get(base, asset)
        assert status == 200 and 'immutable' in headers['Cache-Control']
        assert headers['X-Content-Type-Options'] == 'nosniff'
        for path in ('/auth/me', '/api/v1/who'):
            status, headers, body = get(base, path)
            payload = json.loads(body)
            assert status == 200 and payload['topology'] == topology
            assert payload['path'] == path and not payload['internal']
            assert payload['auth'] == 'Bearer synthetic'
            assert payload['proto'] == ('https' if topology == 'split' else 'http')
        for path in ('/_sandloom/rpc', '/metrics', '/diagnostics/core', '/contracts'):
            assert get(base, path)[0] == 404
        status, _, body = get(base, '/api/v1/fail', 'POST')
        assert status == 502 and json.loads(body)['count'] == 1
        print(json.dumps({'topology': topology, 'static_assets': 'pass', 'admin_manifest': 'pass',
                          'public_proxy': 'pass', 'internal_denied': 'pass',
                          'nonroot_readonly': 'pass', 'no_mutation_retry': 'pass'}))
finally:
    for name in reversed(containers):
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['docker', 'network', 'rm', run_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
