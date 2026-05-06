"""
pytest configuration for agentns tests.

Sets AGENTNS_AUTH=off so the test client can call authenticated endpoints
(POST /register, POST /resolve, DELETE /register, POST /cache/clear)
without needing real API keys.  Never set this in production.
"""
import os

# Disable auth before any test module imports the server
os.environ["AGENTNS_AUTH"] = "off"
