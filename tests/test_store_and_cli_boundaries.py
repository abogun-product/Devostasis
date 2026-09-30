"""Store and command-line boundaries the review of 2026-09-30 found open.

- without an index, the scan for the newest bundle skipped one whose manifest it could not read, so
  the next run compared against an older bundle (or called a store with history BASELINE);
- a repository named ``index.json`` (a valid name) was read as a project index and broke every
  identity lookup of the store;
- problem texts carried absolute store paths and exception messages into the delta of a HISTORY_GAP
  bundle, so the same evidence produced a different bundle id per checkout;
- the fleet surfaces were truncated before they were written;
- a fleet-surface failure hid every per-project result of ``run`` and ``build``, a cache that could not
  be saved ended the run, and a missing or unreadable input was a Python traceback.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from devostasis import runner
from devostasis.cli import main
from devostasis.history import FilesystemHistoryStore, HistoryStoreError
from devostasis.runner import RunOutcome, build_from_observations
from helpers import full_inputs, obs_set
from test_store_hardening import _index_path, _later_obs, _observe

KEY = "github.com/acme/widget"


def _compact(store):
    """What the contract allows a store to lose: the mutable index and the convenience copy."""
    _index_path(store, KEY).unlink()
    shutil.rmtree(store.project_dir(KEY) / "latest")


def test_without_an_index_an_unreadable_newest_bundle_is_a_gap_not_a_comparison_with_an_older_one(tmp_path):
    store = FilesystemHistoryStore(tmp_path)
    _, _, project = _observe(store, "acme/widget", "42")
    second = build_from_observations(project, _later_obs("acme/widget", "42"), store)
    second_path = store.commit(second)
    _compact(store)
    (second_path / "manifest.json").write_text("{ torn", "utf-8")

    state = store.latest(KEY, "42")
    assert state.exists and not state.verified
    assert any(problem.startswith("INDEXLESS_CANDIDATE_UNREADABLE") for problem in state.problems)
    third = build_from_observations(project, _later_obs("acme/widget", "42", "2026-09-07T12:00:00Z"), store)
    assert third.manifest["comparison_status"] == "HISTORY_GAP", "never COMPARABLE against the older, readable bundle"


def test_without_an_index_a_single_unreadable_bundle_is_a_gap_not_a_baseline(tmp_path):
    store = FilesystemHistoryStore(tmp_path)
    _, first_path, project = _observe(store, "acme/widget", "42")
    _compact(store)
    (first_path / "manifest.json").unlink()
    later = build_from_observations(project, _later_obs("acme/widget", "42"), store)
    assert later.manifest["comparison_status"] == "HISTORY_GAP", "history exists on disk; BASELINE would deny it"


def test_an_interrupted_write_is_not_a_candidate(tmp_path):
    store = FilesystemHistoryStore(tmp_path)
    first, first_path, project = _observe(store, "acme/widget", "42")
    _compact(store)
    (first_path.parent / ("f" * 64 + ".staging")).mkdir()
    later = build_from_observations(project, _later_obs("acme/widget", "42"), store)
    assert later.manifest["comparison_status"] == "COMPARABLE" and later.manifest["previous_bundle_id"] == first.bundle_id


def test_a_repository_named_index_json_does_not_break_the_store(tmp_path):
    store = FilesystemHistoryStore(tmp_path)
    _observe(store, "acme/index.json", "7")
    _observe(store, "acme/widget", "42")
    assert len(store.all_projects()) == 2
    assert runner.write_fleet_index(store) is not None
    _, _, gadget = _observe(store, "acme/gadget", "99")
    assert store.latest("github.com/acme/gadget", "99").verified, "identity lookups over the whole store still work"


def test_history_gap_reasons_do_not_depend_on_where_the_store_is_checked_out(tmp_path):
    ids = []
    for location in ("one", os.path.join("somewhere", "else", "two")):
        store = FilesystemHistoryStore(tmp_path / location)
        _, first_path, project = _observe(store, "acme/widget", "42")
        for member in first_path.iterdir():
            member.unlink()
        later = build_from_observations(project, _later_obs("acme/widget", "42"), store)
        assert later.manifest["comparison_status"] == "HISTORY_GAP"
        delta = json.loads(later.members["delta.json"])
        assert str(tmp_path) not in json.dumps(delta), "no absolute path in a canonical member"
        ids.append(later.bundle_id)
    assert ids[0] == ids[1], "the same evidence and the same damage are one bundle wherever the store lives"


def test_the_fleet_surfaces_are_replaced_never_truncated(tmp_path, monkeypatch):
    store = FilesystemHistoryStore(tmp_path)
    _observe(store, "acme/widget", "42")
    runner.write_fleet_index(store)
    index = store.projects_root / "index.json"
    readme = store.projects_root / "README.md"
    before_index, before_readme = index.read_bytes(), readme.read_bytes()
    _observe(store, "acme/gadget", "99")

    def refuse(source, destination):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        runner.write_fleet_index(store)
    monkeypatch.undo()
    assert index.read_bytes() == before_index and readme.read_bytes() == before_readme
    json.loads(index.read_text("utf-8"))


def _fleet_config(tmp_path):
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps({"config_version": "1", "projects": [{"repo": "acme/one"}, {"repo": "acme/two"}]}), "utf-8")
    return path


def _outcome(project, store, client, now, **kwargs):
    if project.repo == "one":
        return RunOutcome(project.locator, True, bundle_id="b" * 64, comparison_status="BASELINE")
    return RunOutcome(project.locator, False, error="HTTP 403 FORBIDDEN")


def test_run_reports_every_project_before_a_fleet_surface_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "run_project", _outcome)

    def broken(store):
        raise HistoryStoreError("index unreadable at projects/github.com/acme/other: JSONDecodeError")

    monkeypatch.setattr(runner, "write_fleet_index", broken)
    code = main(["run", "--config", str(_fleet_config(tmp_path)), "--store", str(tmp_path / "store"), "--token", "x"])
    captured = capsys.readouterr()
    assert code == 1
    assert "[ok] acme/one" in captured.out and "[failed] acme/two" in captured.out
    assert "fleet surfaces not written" in captured.err


def test_a_cache_that_cannot_be_saved_is_a_warning_not_the_end_of_the_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "run_project", lambda project, store, client, now, **kwargs: RunOutcome(project.locator, True, bundle_id="b" * 64, comparison_status="BASELINE"))
    not_a_directory = tmp_path / "cache"
    not_a_directory.write_text("a file where the cache directory should be", "utf-8")
    code = main(["run", "--config", str(_fleet_config(tmp_path)), "--store", str(tmp_path / "store"), "--token", "x", "--cache", str(not_a_directory)])
    captured = capsys.readouterr()
    assert code == 0 and "[ok] acme/one" in captured.out and "[ok] acme/two" in captured.out
    assert "conditional cache not saved" in captured.err


def test_build_reports_the_committed_bundle_before_a_fleet_surface_error(tmp_path, monkeypatch, capsys):
    import devostasis.cli as cli

    observations = tmp_path / "observations.json"
    full_inputs(obs_set()).save(observations)

    def broken(store):
        raise HistoryStoreError("index unreadable at projects/github.com/acme/other: JSONDecodeError")

    monkeypatch.setattr(cli, "write_fleet_index", broken)
    code = main(["build", "--observations", str(observations), "--store", str(tmp_path / "store")])
    captured = capsys.readouterr()
    assert code == 1
    assert "written to" in captured.out, "the committed bundle is reported"
    assert "the bundle is committed, the fleet surfaces are not" in captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["evaluate", "--observations", "{missing}", "--out", "{out}"],
        ["evaluate", "--observations", "{garbage}", "--out", "{out}"],
        ["build", "--observations", "{missing}", "--store", "{store}"],
        ["render", "--bundle", "{empty}"],
        ["gauges", "--bundle", "{empty}"],
        ["demand", "--bundle", "{empty}"],
        ["actions-summary", "--bundle", "{empty}"],
    ],
    ids=["evaluate-missing", "evaluate-not-json", "build-missing", "render", "gauges", "demand", "actions-summary"],
)
def test_a_missing_or_unreadable_input_is_an_input_error_not_a_traceback(tmp_path, capsys, argv):
    (tmp_path / "empty").mkdir()
    (tmp_path / "garbage.json").write_text("{ not json", "utf-8")
    paths = {
        "missing": str(tmp_path / "nowhere.json"),
        "garbage": str(tmp_path / "garbage.json"),
        "out": str(tmp_path / "out.json"),
        "store": str(tmp_path / "store"),
        "empty": str(tmp_path / "empty"),
    }
    code = main([part.format(**paths) for part in argv])
    assert code == 2
    assert "input error" in capsys.readouterr().err
