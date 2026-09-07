@echo off
REM fundeval conda env lives under Downloads\Coding, not directly under Users\vanshkumar
cd /d "C:\Users\vanshkumar\Downloads\FundEval"
"C:\Users\vanshkumar\Downloads\Coding\envs\fundeval\python.exe" "C:\Users\vanshkumar\Downloads\FundEval\foundation\daily_update.py" --db mf_nav_full.duckdb
