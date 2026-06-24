"""Pytest config — put the project root on sys.path so tests can import the
bot modules directly (state_io, order_manager, ...) regardless of cwd."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
