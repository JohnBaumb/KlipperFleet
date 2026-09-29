"""Pytest configuration - adds project root to Python path."""
import asyncio
import sys
from pathlib import Path

import pytest

# Add project root to Python path so 'backend' module can be imported
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# One event loop for the whole session, created before any test module imports
# backend. The managers build asyncio.Lock() objects when they are constructed,
# and on Python 3.9 a Lock binds to whatever loop is current at that moment, so
# every test has to keep using this same loop.
_session_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_session_loop)


@pytest.fixture(autouse=True)
def _keep_event_loop_current():
    """Keep asyncio.get_event_loop() usable in the sync tests.

    pytest-asyncio leaves the current loop unset after each async test, and on
    Python 3.12+ get_event_loop() raises instead of creating a new one. The sync
    tests call get_event_loop().run_until_complete(...), so restore the session
    loop before each test rather than rewriting every call site.
    """
    asyncio.set_event_loop(_session_loop)
    yield
