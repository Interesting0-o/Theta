import os
import sys

if os.environ.get("THETA_LANGGRAPH_DEV_PNA") == "1" and sys.platform == "win32":
    from starlette.middleware.cors import CORSMiddleware

    _original_init = CORSMiddleware.__init__

    def _init_with_private_network(self, *args, **kwargs):
        kwargs.setdefault("allow_private_network", True)
        _original_init(self, *args, **kwargs)

    CORSMiddleware.__init__ = _init_with_private_network
