#!/bin/sh
set -eu
python -m compileall -q can_tcp_gateway.py can_tcp_client.py tests
