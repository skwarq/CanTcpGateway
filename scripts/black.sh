#!/bin/sh
set -eu

if [ "${1:-}" = "" ]; then
    python -m black can_tcp_gateway.py can_tcp_client.py tests
    exit 0
fi

if [ "$1" = "--check" ]; then
    python -m black --check can_tcp_gateway.py can_tcp_client.py tests
    exit 0
fi

echo "Usage: sh scripts/black.sh [--check]" >&2
exit 2
