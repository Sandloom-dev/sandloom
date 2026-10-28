"""Retrieve a bounded origin trace for observed Cloudflare 502 Ray IDs."""

from __future__ import annotations

import json
import re

from external import remote


def collect(alias, sample):
    rays = {
        e["headers"].get("cf-ray", "").split("-", 1)[0]
        for e in sample.get("error_samples", [])
        if e["status"] == 502
    }
    rays = sorted(r for r in rays if re.fullmatch(r"[a-fA-F0-9]{16,32}", r))
    if not rays:
        return
    code = """import json,os,urllib.request,sys
out=[]
for ray in json.loads(sys.argv[1]):
 for _ in range(10):
  request=urllib.request.Request('http://127.0.0.1:2222/_qualification/edge-trace?ray='+ray,
   headers={'X-Internal-Token':os.environ['INTERNAL_API_TOKEN'],'Connection':'close'})
  with urllib.request.urlopen(request,timeout=2) as response:
   out.append({'ray':ray,**json.load(response)})
print(json.dumps(out))
"""
    try:
        sample["origin_trace"] = json.loads(
            remote(
                alias,
                [
                    "sudo",
                    "-n",
                    "docker",
                    "exec",
                    "sandloom-release-qualification-app-1",
                    "python",
                    "-c",
                    code,
                    json.dumps(rays),
                ],
            )
        )
    except Exception as error:
        # The app can itself be deliberately down during a fault window.
        sample["origin_trace_error"] = type(error).__name__
