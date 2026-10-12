# -*- coding: utf-8 -*-
"""Compatibility launcher; the packaged bridge is the canonical implementation."""
import os
import runpy

if __name__ == "__main__":
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "factoryio_agents", "codesys_bridge.py")
    runpy.run_path(path, init_globals=globals(), run_name="__main__")
