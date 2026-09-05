# FundEval

Quantitative mutual fund analytical pipeline (internal research). Survivorship-bias-free: every fund's full return history (dead, merged, renamed) is derived from AMFI's historical NAV report, not mfapi.in (which only reflects a current-day snapshot).

## Setup on a new machine

1. Clone this repo.
2. Create a venv/conda env and pip install -r requirements.txt.
3. Rebuild the databases locally - they are NOT tracked in git (too large): mf_nav_full.duckdb (foundation NAV database), fundeval_analysis.duckdb (L1 analysis layer). TODO: document exact script order to rebuild from scratch.

## Known issues / active diagnostics
See jump_classification.csv and in-progress work in Diagnostics/.
