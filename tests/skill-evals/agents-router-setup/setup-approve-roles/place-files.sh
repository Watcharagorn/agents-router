#!/usr/bin/env bash
set -eu
cp -R "$(dirname "$0")/files/." .
python3 "$(dirname "$0")/../../make_sandbox.py" host
