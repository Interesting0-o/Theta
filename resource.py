"""Prevent the runtime data directory from masquerading as Unix's resource module."""

import sys

if sys.platform == "win32":
    raise ImportError("the resource module is only available on Unix")
