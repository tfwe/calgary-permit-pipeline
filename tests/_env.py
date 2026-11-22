"""Give the optional external-tool settings harmless fake values for the test run.

The tests stub every subprocess call, so these paths are never executed. Setting
them keeps the code paths that build argv exercised. Imported before any
module that reads config.py.
"""
import os
import socket

# The suite must never touch the network. Any real connection attempt fails
# loudly here instead of silently depending on DNS or a live host.
def _no_network(*_a, **_k):
    raise RuntimeError("network access is disabled in the test suite; stub the call")

socket.socket.connect = _no_network
socket.getaddrinfo = _no_network

_FAKE = {
    "CALGARY_LEADS_GWS_BIN": "/opt/test-fakes/bin/gws",
    "CALGARY_LEADS_TOKEN_SCRIPT": "/opt/test-fakes/bin/token.py",
    "CALGARY_LEADS_JOB_SCRIPT": "/opt/test-fakes/bin/gemini-job.py",
    "CALGARY_LEADS_TRIGGER_SCRIPT": "/opt/test-fakes/bin/gemini-task-trigger.py",
    "CALGARY_LEADS_QUOTA_SCRIPT": "/opt/test-fakes/bin/gemini-quota.py",
    "CALGARY_LEADS_BRIDGE_SCRIPT": "/opt/test-fakes/bin/the bridge script",
}
for _k, _v in _FAKE.items():
    os.environ.setdefault(_k, _v)
