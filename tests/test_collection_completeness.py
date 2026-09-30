"""What the review of 2026-09-30 found at the collection boundary: listings that claimed completeness
they did not have, a "false" nobody observed, and failures that escaped as Python exceptions.

- a filtered ``/actions/runs`` query stops at 1,000 results and answers the next page empty; the
  provider's ``total_count`` says so, and a listing that ends short of it is PARTIAL;
- page-number pagination over a list that grows between pages repeats a row (and may skip one);
  each row counts once and the listing is PARTIAL (``LISTING_SHIFTED``);
- a revision whose check-suite page the request budget refused was counted as examined, which let
  ``ci.configured`` be a positively observed false;
- ``http.client`` failures, a timeout while reading an error body, a body nested past the decoder's
  depth, an out-of-range timestamp and a malformed register escaped the inventory boundary;
- run and suite fields that the parent record carries into the bundle were untyped, so a number there
  failed the canonical encoder and cost the project its bundle.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import urllib.error

import pytest

from devostasis import timeutil
from devostasis.adapters.github import (
    MAX_REGISTER_BYTES,
    GitHubAdapter,
    GitHubClient,
    NetworkFailure,
    ApiFailure,
    RegisterError,
    UrllibTransport,
    parse_targets_register,
)
from devostasis.config import single_project
from devostasis.normalize import CI_CONFIGURED, CI_REVISIONS, INV_COMMITS
from test_github_adapter import BASE, NOW, FakeTransport, _collect, _commit, _paged, _routes, _suite, _suite_routes, _window_commits


def _runs_listing(runs, served=None):
    served = runs if served is None else served

    def handler(params):
        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", 100))
        return 200, {}, {"total_count": len(runs), "workflow_runs": served[(page - 1) * per_page: page * per_page]}

    return handler


def test_a_runs_listing_that_ends_short_of_the_providers_total_is_partial():
    commits = [_commit(f"c{i:02d}", f"2026-09-0{1 + i % 4}T{10 + i:02d}:00:00Z") for i in range(11)]
    runs = [
        {"id": 1000 + n, "run_attempt": 1, "head_sha": commits[n // 100]["sha"], "status": "completed", "conclusion": "success", "name": "ci", "event": "push", "html_url": "u", "workflow_id": 1}
        for n in range(1100)
    ]
    routes = _routes(**{f"{BASE}/commits": _paged(commits), f"{BASE}/actions/runs": _runs_listing(runs, served=runs[:1000])})
    obs, _, _ = _collect(routes)
    item = obs.get(CI_REVISIONS)
    assert item.status == "PARTIAL" and item.coverage["runs_complete"] is False, "the empty eleventh page is the search ceiling, not the end"

    routes[f"{BASE}/actions/runs"] = _runs_listing(runs)
    obs, _, _ = _collect(routes)
    assert obs.get(CI_REVISIONS).status == "AVAILABLE", "the same listing served in full is complete"


def test_a_total_count_that_is_not_a_count_is_a_declared_failure():
    routes = _routes(**{f"{BASE}/actions/runs": (200, {}, {"total_count": "many", "workflow_runs": []})})
    obs, _, _ = _collect(routes)
    item = obs.get(CI_REVISIONS)
    assert item.status == "ERROR" and item.reason_code == "UNEXPECTED_PAYLOAD"


def test_a_commit_listing_that_repeats_a_commit_between_pages_counts_it_once_and_is_partial():
    commits = [_commit(f"s{i:03d}", f"2026-09-0{1 + i % 4}T{i % 24:02d}:{i % 60:02d}:00Z") for i in range(150)]

    def shifted(params):
        page = int(params.get("page", 1))
        if page == 1:
            return 200, {}, commits[:100]
        return 200, {}, commits[97:147]

    obs, _, _ = _collect(_routes(**{f"{BASE}/commits": shifted}))
    item = obs.get(INV_COMMITS)
    shas = [record["sha"] for record in item.value]
    assert len(shas) == len(set(shas)) == 147
    assert item.status == "PARTIAL" and item.reason_code == "LISTING_SHIFTED"


def test_a_runs_listing_that_repeats_a_run_is_partial_and_the_run_counts_once():
    runs = [
        {"id": 500 + n, "run_attempt": 1, "head_sha": "c1", "status": "completed", "conclusion": "success", "name": f"wf{n}", "event": "push", "html_url": "u", "workflow_id": n}
        for n in range(150)
    ]

    def shifted(params):
        page = int(params.get("page", 1))
        batch = runs[:100] if page == 1 else runs[99:150]
        return 200, {}, {"total_count": len(runs), "workflow_runs": batch}

    obs, _, _ = _collect(_routes(**{f"{BASE}/actions/runs": shifted}))
    item = obs.get(CI_REVISIONS)
    c1 = {record["revision"]: record for record in item.value}["c1"]
    ids = [parent["parent_id"] for parent in c1["parents"]]
    assert len(ids) == len(set(ids)) == 150
    assert item.status == "PARTIAL" and item.reason_code == "LISTING_SHIFTED"


def test_a_revision_the_budget_refused_is_not_examined_and_configured_is_not_a_false_nobody_observed():
    commits = _window_commits(2)
    routes = _suite_routes(commits, {commits[0]["sha"]: [_suite(1)], commits[1]["sha"]: [_suite(2, "failure")]})
    # Count the requests a full collection spends, then give one fewer, so the last suite page is refused.
    full = GitHubClient(FakeTransport(routes))
    GitHubAdapter(full, NOW).collect(single_project("acme/widget"))
    budgeted = GitHubClient(FakeTransport(routes), budget=full.billed_count - 1)
    obs = GitHubAdapter(budgeted, NOW).collect(single_project("acme/widget"))
    item = obs.get(CI_REVISIONS)
    assert item.coverage["suite_revisions_planned"] == 2 and item.coverage["suite_revisions_examined"] == 1
    configured = obs.get(CI_CONFIGURED)
    assert configured.status != "AVAILABLE" or configured.value is True, "a revision nobody read cannot make ci.configured false"


class _FailingBody(io.BytesIO):
    def __init__(self, exc):
        super().__init__(b"")
        self._exc = exc
        self.status = 200
        self.headers = {}

    def read(self, *args):
        raise self._exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _transport(opener):
    transport = UrllibTransport(token=None)
    transport._open = opener
    return transport


@pytest.mark.parametrize("exc", [http.client.IncompleteRead(b"[", 40), http.client.BadStatusLine("garbage")])
def test_http_client_failures_are_network_failures(exc):
    def opener(request):
        if isinstance(exc, http.client.BadStatusLine):
            raise exc
        return _FailingBody(exc)

    with pytest.raises(NetworkFailure):
        _transport(opener).get("/repos/acme/widget")


def test_an_error_body_that_cannot_be_read_keeps_the_status():
    class Body(io.BytesIO):
        def read(self, *args):
            raise TimeoutError("read timed out")

    def opener(request):
        raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, Body(b""))

    status, _, body = _transport(opener).get("/repos/acme/widget")
    assert status == 503 and body is None


def test_a_body_nested_past_the_decoder_depth_is_a_malformed_response():
    deep = ("[" * 200_000 + "]" * 200_000).encode()

    class Ok(io.BytesIO):
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with pytest.raises(ApiFailure) as caught:
        _transport(lambda request: Ok(deep)).get("/repos/acme/widget")
    assert caught.value.reason_code == "MALFORMED_RESPONSE"


def test_an_out_of_range_instant_is_a_value_error_not_an_overflow():
    for text in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
        with pytest.raises(ValueError):
            timeutil.parse_ts(text)
    with pytest.raises(RegisterError):
        parse_targets_register({"schema": "devostasis.targets.v1", "targets": [{"id": "A1", "title": "t", "state": "open", "due": "0001-01-01T00:00:00+01:00"}]})


def _register_route(body):
    return {f"{BASE}/contents/.devostasis/targets.json": (200, {}, body)}


def _file_planning():
    return {"planning": {"source": "file", "path": ".devostasis/targets.json"}}


@pytest.mark.parametrize(
    "body",
    [
        {"type": "file", "size": 10, "encoding": "base64", "content": ["e30="]},
        {"type": "file", "encoding": "base64", "content": "e30="},
        {"type": "file", "size": 10, "encoding": "base64", "content": "e30=!!"},
        {"type": "file", "size": 10, "encoding": "base64", "content": base64.b64encode(("[" * 100_000 + "]" * 100_000).encode()).decode()},
        {"type": "file", "size": 10, "encoding": "base64", "content": base64.b64encode(b"{" + b" " * (MAX_REGISTER_BYTES + 1) + b"}").decode()},
    ],
    ids=["content-not-a-string", "size-missing", "not-base64", "nested-past-depth", "larger-than-its-size-says"],
)
def test_a_malformed_register_file_is_a_declared_failure_not_an_exception(body):
    routes = _routes(**_register_route(body))
    obs, _, _ = _collect(routes, **_file_planning())
    item = obs.get("planning.explicit_targets.inventory")
    assert item is not None and item.status == "ERROR", body


@pytest.mark.parametrize(
    "field, value",
    [("name", 1.5), ("event", ["push"]), ("html_url", 2.5), ("workflow_id", "one")],
)
def test_an_untyped_run_field_is_a_declared_payload_failure_not_a_lost_bundle(field, value):
    run = {"id": 1, "run_attempt": 1, "head_sha": "c1", "status": "completed", "conclusion": "success", "name": "ci", "event": "push", "html_url": "u", "workflow_id": 1}
    run[field] = value
    obs, _, _ = _collect(_routes(**{f"{BASE}/actions/runs": (200, {}, {"total_count": 1, "workflow_runs": [run]})}))
    item = obs.get(CI_REVISIONS)
    assert item.status == "ERROR" and item.reason_code == "UNEXPECTED_PAYLOAD"
    json.dumps(obs.to_dict())


def test_an_untyped_suite_url_is_a_declared_payload_failure():
    commits = _window_commits(1)
    suite = _suite(1)
    suite["url"] = 2.5
    obs, _, _ = _collect(_suite_routes(commits, {commits[0]["sha"]: [suite]}))
    item = obs.get(CI_REVISIONS)
    assert item.status in ("ERROR", "PARTIAL")
    assert all(not record["parents"] for record in (item.value or []))
