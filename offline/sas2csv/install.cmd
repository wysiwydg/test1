@echo off
rem Install the converter from the wheels in this folder. No network needed.
rem Uses the Python already on PATH; the bundle is built for one version and
rem install.py says so loudly if this is not it.
python "%~dp0install.py" %*
