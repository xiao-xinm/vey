"""Intentional failures for isolated containers only; no business dependencies."""

import os
import socket
import sys

mode = os.environ["FIXTURE_MODE"]
if mode == "exit":
    print("application stopped with exit 7", flush=True)
    sys.exit(7)
if mode == "config":
    print("configuration error: required SETTING is missing", flush=True)
    sys.exit(2)
if mode == "dependency":
    try:
        socket.create_connection(("127.0.0.1", 65432), timeout=1)
    except OSError:
        print("dependency connection refused at 127.0.0.1:65432", flush=True)
        sys.exit(3)
    sys.exit(99)
raise RuntimeError("Unknown fixture mode")
