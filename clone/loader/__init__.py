# `clone` is not an installed package, so `clone/load.py` (Task 11) must be run
# as a module from the repo root -- `.venv/bin/python -m clone.load --csv-dir
# ...` -- never as a script path. `python clone/load.py` puts only `clone/` on
# sys.path, not the repo root, so every `from clone...` import in it will raise
# `ModuleNotFoundError: No module named 'clone'`. See clone/README.md, "Running
# the loader".
