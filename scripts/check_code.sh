#!/bin/sh
set -eu
python -m ruff check can_tcp_gateway.py can_tcp_client.py tests
