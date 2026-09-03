@echo off
rem Convert zipped sas7bdat extracts to CSV. Everything after the script name
rem is passed straight through, so --help lists every option.
rem
rem   convert.cmd C:\extracts\POLICY.zip --out C:\staging
rem
"%~dp0.venv\Scripts\python.exe" -m scripts.sas2csv %*
