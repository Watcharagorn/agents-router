#!/usr/bin/env bash
set -eu
cp -R "$(dirname "$0")/files/." .
python3 "$(dirname "$0")/../../make_sandbox.py" jobs
git init -q
printf '.agt-eval/\n.claude/\n' >> .git/info/exclude
git add greet.py
git -c user.name=eval -c user.email=eval@example.invalid commit -q -m "greet"
cp greet.new greet.py && rm greet.new
