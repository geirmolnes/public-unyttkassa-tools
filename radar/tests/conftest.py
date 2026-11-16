"""Shared test setup.

Route tests run with the login gate off (AUTH_DISABLED=1, local only); test_auth
drives AuthMiddleware on a minimal app and test_prod_startup the real one.
"""

import os

os.environ.setdefault("AUTH_DISABLED", "1")
os.environ["STATS_TOKEN"] = "test-stats-token"
