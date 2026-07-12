#!/usr/bin/env bash
set -eu

sudo dnf install -y python3 python3-pip git stress-ng nginx redis sysbench
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

