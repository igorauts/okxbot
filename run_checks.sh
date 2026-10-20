#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -B tests/test_offline.py
