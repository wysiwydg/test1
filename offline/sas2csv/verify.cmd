@echo off
rem Check the converter against sas7bdat files generated here, on this machine,
rem before trusting it with an extract. Needs no network and no SAS.
cd /d "%~dp0"
.venv\Scripts\python.exe -m pytest tests -q
