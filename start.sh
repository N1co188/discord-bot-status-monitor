#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Isolated virtual environment instead of installing into the system Python
if [ ! -d venv ]; then
    python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

pip install --disable-pip-version-check -q -r requirements.txt

# Keep secrets and runtime data private to the current user
umask 077

exec python bot.py
