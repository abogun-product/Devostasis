"""Every conformance vector in the corpus, executed as an ordinary test (target B1).

The corpus under ``tests/vectors`` is the executable half of
``docs/spec/conformance.md``: a case written there as a vector is proved here,
by the same runner that will execute the vectors of ``PV-TEST-001`` when they
arrive. The documented examples under ``examples/vectors`` run too, so the
format shown in the specification cannot drift from the format the runner
accepts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devostasis import vectors

ROOT = Path(__file__).resolve().parent.parent.parent
CORPUS = ROOT / "tests" / "vectors"
EXAMPLES = ROOT / "examples" / "vectors"

ALL = vectors.load([CORPUS, EXAMPLES])


def test_the_corpus_is_not_empty():
    """An empty corpus would make every case below vacuously true."""
    assert ALL, "no conformance vectors were loaded"
    assert any(vector.path.replace("\\", "/").startswith(str(CORPUS).replace("\\", "/")) for vector in ALL)


@pytest.mark.parametrize("vector", ALL, ids=lambda vector: vector.case)
def test_conformance_vector(vector):
    result = vectors.run(vector)
    assert result.ok, "\n".join([f"{vector.case}: {vector.title}"] + result.failures)


def test_every_kind_the_format_declares_is_exercised_by_the_corpus():
    """A kind nothing exercises is a promise, not a proof."""
    assert {vector.kind for vector in ALL} == set(vectors.KINDS)


def test_every_envelope_in_the_corpus_is_one_the_published_schema_accepts():
    """The runner and the published schema must accept the same evidence.

    Only the key sets are checked here: the runtime carries no JSON Schema
    validator, so this is the guard that a vector the runner runs is not a
    vector the schema beside it declares invalid.
    """
    from devostasis import canonical

    envelope = canonical.load_file(ROOT / "schemas" / "conformance-vector.schema.json")["$defs"]["envelope"]
    required, allowed = set(envelope["required"]), set(envelope["properties"])
    for vector in ALL:
        if vector.kind != "vital":
            continue
        shapes = vector.given["variants"] if "variants" in vector.given else [vector.given]
        for shape in shapes:
            for stated in shape["observations"]:
                keys = set(stated)
                assert required <= keys, f"{vector.case}: envelope is missing {sorted(required - keys)}"
                assert keys <= allowed, f"{vector.case}: envelope states {sorted(keys - allowed)}, which the schema does not declare"


def _envelopes():
    for vector in ALL:
        if vector.kind == "vital":
            shapes = vector.given["variants"] if "variants" in vector.given else [vector.given]
            for shape in shapes:
                yield vector.case, shape["observations"]
        elif vector.kind == "activity":
            yield vector.case, vector.given["observations"]


def test_every_envelope_satisfies_the_schemas_value_conditional():
    """The conditional of the envelope, evaluated as JSON Schema 2020-12 evaluates it.

    Without ``required: [status]`` in its ``if``, an envelope that omits the
    status (the normal form: the runner defaults it to AVAILABLE) satisfied
    the ``if`` vacuously and had its value forbidden, so the published schema
    rejected 39 of 64 vectors the runner accepts.
    """
    from devostasis import canonical

    envelope = canonical.load_file(ROOT / "schemas" / "conformance-vector.schema.json")["$defs"]["envelope"]
    condition = envelope["allOf"][0]["if"]
    forbidden_statuses = set(condition["properties"]["status"]["enum"])
    for case, observations in _envelopes():
        for stated in observations:
            applies = all(key in stated for key in condition.get("required", [])) and (
                "status" not in stated or stated["status"] in forbidden_statuses
            )
            if applies:
                assert stated.get("value") is None, f"{case}: the schema forbids the value of {stated['observation_id']}"
