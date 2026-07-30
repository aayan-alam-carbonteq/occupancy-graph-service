"""The CLI entry point. Single process by design -- see service/app.py."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distribution

import pytest

from occupancy_graph.service.serve import build_parser


def test_defaults_bind_locally_on_8000():
    args = build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8000


def test_host_and_port_are_overridable_for_the_container():
    args = build_parser().parse_args(["--host", "0.0.0.0", "--port", "9001"])
    assert args.host == "0.0.0.0"
    assert args.port == 9001


def test_there_is_no_db_or_workers_flag_left():
    """--db pointed at a SQLite file that no longer exists; --workers forked
    processes that would each hold their own bundle cache."""
    flags = {action.dest for action in build_parser()._actions}
    assert "db" not in flags
    assert "workers" not in flags


def test_the_installed_console_script_points_at_this_module():
    """The declared entry point and the INSTALLED one are different facts.

    pyproject has named `occupancy_graph.service.serve:main` for a while, but
    the script in the venv was still the one setuptools generated in the
    GraphQL era -- it imported `occupancy_graph.graphql.serve`, a package that
    no longer exists, so `occupancy-graph-serve` was a stack trace. Reading the
    metadata is what tells us the install was refreshed; reading pyproject
    would only re-assert the intent.

    Skipped rather than failed on a checkout that was never installed: the
    suite itself runs off `pythonpath = ["src"]` and does not need the dist.
    """
    try:
        dist = distribution("occupancy-graph-service")
    except PackageNotFoundError:
        pytest.skip("occupancy-graph-service is not installed in this environment")

    scripts = {
        entry.name: entry.value
        for entry in dist.entry_points
        if entry.group == "console_scripts"
    }
    assert scripts == {"occupancy-graph-serve": "occupancy_graph.service.serve:main"}
