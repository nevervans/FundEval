#!/usr/bin/env bash
set -e

pip install --upgrade pip
pip install duckdb pandas==2.3.3 casparser pyxirr matplotlib ipykernel magic_duckdb

echo "FundEval environment ready."
