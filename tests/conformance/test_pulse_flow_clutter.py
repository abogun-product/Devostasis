"""V0 band tables for Pulse, Flow and Clutter, the V0.1/V0.2 degraded-data repairs, and the
calibration repairs adopted by rule versions pulse.bands.v1 (PULSE-CAP-01..05) and
flow.bands.v1 (FLOW-EQ-01..06, FLOW-PREC-03..06, FLOW-PREC-09)."""

import pytest

from devostasis import canonical, normalize
from devostasis.config import single_project
from devostasis.observations import PARTIAL, STALE, UNAVAILABLE, UNKNOWN, ObservationSet
from devostasis.vitals import clutter, flow, pulse
from helpers import MEDIAN_HOURS, MEDIAN_SECONDS, add, clutter_inputs, flow_inputs, obs_set, pulse_inputs

# The coverage by which a PARTIAL count proves it is an observed subset (PV-REV-PR-031-003).
SUBSET = {"complete": False, "value_semantics": "OBSERVED_SUBSET_COUNT"}


def test_pulse_band_table():
    assert pulse.classify(15, 0, 0) == "SURGING"
    assert pulse.classify(2, 40, 2) == "SURGING"
    assert pulse.classify(3, 40, 1) == "STEADY"
    assert pulse.classify(3, 5, 1) == "STEADY"
    assert pulse.classify(2, 5, 1) == "QUIET"
    assert pulse.classify(0, 0, 0) == "DORMANT"


def test_pulse_exact_when_every_channel_is_observed():
    obs = obs_set()
    pulse_inputs(obs, commits=10, active_days=4, cr_updates=3, issue_updates=0)
    result = pulse.evaluate(obs)
    assert (result.band, result.evaluation_status, result.band_semantics) == ("STEADY", "AVAILABLE", "EXACT")
    assert result.derived["activity_events_28d"] == 13 and result.derived["channel_count"] == 2
    assert result.rule_id == "pulse.bands.v1"


def test_pulse_missing_required_input_is_unknown():
    obs = obs_set()
    add(obs, "git.default_branch.commits.count_28d", 10)
    result = pulse.evaluate(obs)
    assert result.band is None and result.evaluation_status == "UNKNOWN"


def test_r3_zero_activity_with_unavailable_channel_is_degraded_dormant_lower_bound():
    obs = obs_set()
    pulse_inputs(obs, commits=0, active_days=0, cr_updates=0, issue_updates=None)
    add(obs, "forge.issues.updated_count_28d", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    result = pulse.evaluate(obs)
    assert result.band == "DORMANT" and result.evaluation_status == "DEGRADED"
    assert result.band_semantics == "CONSERVATIVE_LOWER_BOUND"
    assert result.possible_bands == ["DORMANT", "QUIET", "STEADY", "SURGING"]


def test_v1_10_unavailable_channel_never_becomes_zero_but_lower_bound_still_classifies():
    obs = obs_set()
    pulse_inputs(obs, commits=30, active_days=16, cr_updates=None, issue_updates=2)
    add(obs, "forge.change_requests.updated_count_28d", status=PARTIAL, value=5, reason_code="PAGINATION_CAPPED")
    result = pulse.evaluate(obs)
    assert result.band == "SURGING" and result.evaluation_status == "DEGRADED"
    assert result.derived["activity_events_28d"] == 32
    assert any(code.startswith("UNOBSERVED_CHANNEL:forge.change_requests.updated_count_28d:PARTIAL") for code in result.diagnostics)


def _capped_pulse(commits_days: tuple[int, int], cr_updates: int, issue_updates: int, days_exact: bool = False, coverage=None):
    obs = obs_set()
    commits, days = commits_days
    add(obs, "git.default_branch.commits.count_28d", commits, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage=coverage)
    if days_exact:
        add(obs, "git.default_branch.commit_active_days_28d", days)
    else:
        add(obs, "git.default_branch.commit_active_days_28d", days, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage=coverage)
    add(obs, "forge.change_requests.updated_count_28d", cr_updates)
    add(obs, "forge.issues.updated_count_28d", issue_updates)
    return obs


def test_pulse_cap_01_invariant_lower_bound_forces_one_band():
    result = pulse.evaluate(_capped_pulse((3000, 14), 500, 100))
    assert result.band == "SURGING" and result.evaluation_status == "DEGRADED"
    assert result.band_semantics == "CONSERVATIVE_LOWER_BOUND" and result.possible_bands == ["SURGING"]
    assert result.derived["commits_28d_semantics"] == "LOWER_BOUND" and result.derived["commit_active_days_28d_semantics"] == "LOWER_BOUND"
    assert result.derived["required_lower_bound_rule"] == "PV-PULSE-REQUIRED-LOWER-BOUND-001"
    assert any(code.startswith("REQUIRED_INPUT_PARTIAL:git.default_branch.commits.count_28d") for code in result.diagnostics)


def test_pulse_cap_01_exact_days_with_capped_commits_is_forced_quiet():
    result = pulse.evaluate(_capped_pulse((3000, 2), 0, 0, days_exact=True))
    assert result.band == "QUIET" and result.possible_bands == ["QUIET"] and result.evaluation_status == "DEGRADED"
    assert "commit_active_days_28d_semantics" not in result.derived


def test_pulse_cap_02_boundary_crossing_lists_every_reachable_band_and_no_exact_band():
    result = pulse.evaluate(_capped_pulse((3000, 2), 0, 0))
    assert result.evaluation_status == "DEGRADED" and result.band_semantics == "CONSERVATIVE_LOWER_BOUND"
    assert result.band == "QUIET" and result.possible_bands == ["QUIET", "STEADY", "SURGING"]
    assert "could reach QUIET, STEADY, SURGING" in result.explanation


def test_pulse_cap_03_unconstrained_required_tail_is_unknown():
    obs = obs_set()
    add(obs, "git.default_branch.commits.count_28d", status=PARTIAL, reason_code="PAGINATION_CAPPED")
    add(obs, "git.default_branch.commit_active_days_28d", 2)
    assert pulse.evaluate(obs).evaluation_status == "UNKNOWN"
    stale = obs_set()
    add(stale, "git.default_branch.commits.count_28d", 3000, status=PARTIAL, freshness=STALE, reason_code="PAGINATION_CAPPED")
    add(stale, "git.default_branch.commit_active_days_28d", 2)
    result = pulse.evaluate(stale)
    assert result.evaluation_status == "UNKNOWN" and result.band is None


def test_pulse_cap_04_optional_channel_cannot_make_capped_required_input_exact():
    result = pulse.evaluate(_capped_pulse((3000, 2), 500, 0))
    assert result.band == "SURGING" and result.possible_bands == ["SURGING"]
    assert result.evaluation_status == "DEGRADED" and result.band_semantics == "CONSERVATIVE_LOWER_BOUND"


def test_pulse_cap_05_pagination_metadata_is_non_semantic():
    results = [
        pulse.evaluate(_capped_pulse((3000, 2), 0, 0, coverage={"complete": False, "page_size": page_size})).to_dict()
        for page_size in (100, 30)
    ]
    assert results[0] == results[1]


def test_flow_no_queue_and_moving():
    obs = obs_set()
    flow_inputs(obs, 0, 0)
    result = flow.evaluate(obs)
    assert result.band == "NO_QUEUE" and result.rule_id == "flow.bands.v1"
    obs = obs_set()
    flow_inputs(obs, 2, 5, oldest=3, median=10)
    assert flow.evaluate(obs).band == "MOVING"


def test_flow_congested_by_age_count_or_median():
    for kwargs in ({"open_count": 1, "merged": 3, "oldest": 14, "median": 5}, {"open_count": 10, "merged": 3, "oldest": 1, "median": 5}, {"open_count": 1, "merged": 3, "oldest": 1, "median": 169}):
        obs = obs_set()
        flow_inputs(obs, **kwargs)
        assert flow.evaluate(obs).band == "CONGESTED", kwargs


def test_flow_gridlocked_by_stale_queue_or_slow_large_queue():
    obs = obs_set()
    flow_inputs(obs, 3, 0, oldest=30)
    assert flow.evaluate(obs).band == "GRIDLOCKED"
    obs = obs_set()
    flow_inputs(obs, 10, 2, oldest=2, median=337)
    assert flow.evaluate(obs).band == "GRIDLOCKED"


def test_flow_missing_conditional_input_is_unknown_not_guessed():
    obs = obs_set()
    flow_inputs(obs, 2, 0)
    result = flow.evaluate(obs)
    assert result.band is None and result.evaluation_status == "UNKNOWN"


def test_flow_eq_01_empty_queue_with_slow_historical_median_is_no_queue():
    obs = obs_set()
    flow_inputs(obs, 0, 1, median=240)
    result = flow.evaluate(obs)
    assert (result.band, result.evaluation_status, result.band_semantics) == ("NO_QUEUE", "AVAILABLE", "EXACT")
    assert "FLOW_HISTORICAL_MEDIAN_NOT_APPLICABLE:EMPTY_QUEUE" in result.diagnostics
    assert result.derived["median_time_to_merge_hours_28d"] == 240
    assert result.derived["median_time_to_merge_seconds_28d"] == {"numerator": 864000, "denominator": 1}
    assert "not applicable to an empty queue" in result.explanation


def test_flow_eq_02_extreme_historical_median_cannot_gridlock_an_empty_queue():
    obs = obs_set()
    flow_inputs(obs, 0, 1, median=500)
    assert flow.evaluate(obs).band == "NO_QUEUE"


def test_flow_eq_03_live_queue_keeps_the_congested_median_threshold():
    obs = obs_set()
    flow_inputs(obs, 1, 1, oldest=1, median=240)
    assert flow.evaluate(obs).band == "CONGESTED"


def test_flow_eq_04_live_large_queue_keeps_the_gridlocked_predicate():
    obs = obs_set()
    flow_inputs(obs, 10, 1, oldest=1, median=400)
    assert flow.evaluate(obs).band == "GRIDLOCKED"


def test_flow_eq_05_missing_open_count_never_becomes_an_empty_queue():
    for status, value in ((PARTIAL, 0), (UNKNOWN, None)):
        obs = obs_set()
        add(obs, "forge.change_requests.open_count", value, status=status, reason_code="PAGINATION_CAPPED")
        add(obs, "forge.change_requests.merged_count_28d", 1)
        add(obs, MEDIAN_SECONDS, {"numerator": 864000, "denominator": 1}, "duration")
        result = flow.evaluate(obs)
        assert result.band is None and result.evaluation_status == "UNKNOWN", status


def test_flow_eq_06_provider_and_order_invariance():
    first = obs_set()
    flow_inputs(first, 0, 1, median=240)
    second = obs_set()
    add(second, MEDIAN_SECONDS, {"numerator": 864000, "denominator": 1}, "duration")
    add(second, "forge.change_requests.merged_count_28d", 1)
    add(second, "forge.change_requests.open_count", 0)
    add(second, MEDIAN_HOURS, 240, "duration")
    assert flow.evaluate(first).to_dict() == flow.evaluate(second).to_dict()


def test_flow_prec_03_04_lower_boundary_is_exact_in_seconds():
    obs = obs_set()
    flow_inputs(obs, 1, 1, oldest=1, median_seconds=604800)
    assert flow.evaluate(obs).band == "MOVING"
    obs = obs_set()
    flow_inputs(obs, 1, 1, oldest=1, median_seconds=604801)
    assert flow.evaluate(obs).band == "CONGESTED"


def test_flow_prec_05_upper_boundary_is_exact_in_seconds():
    obs = obs_set()
    flow_inputs(obs, 10, 1, oldest=1, median_seconds=1209600)
    assert flow.evaluate(obs).band == "CONGESTED"
    obs = obs_set()
    flow_inputs(obs, 10, 1, oldest=1, median_seconds=1209601)
    assert flow.evaluate(obs).band == "GRIDLOCKED"


def test_flow_prec_06_empty_queue_takes_precedence_over_any_median():
    obs = obs_set()
    flow_inputs(obs, 0, 1, median_seconds=2000000)
    assert flow.evaluate(obs).band == "NO_QUEUE"


def test_flow_prec_09_partial_evidence_stays_unknown():
    obs = obs_set()
    add(obs, "forge.change_requests.open_count", 1)
    add(obs, "forge.change_requests.merged_count_28d", 1, status=PARTIAL, reason_code="PAGINATION_CAPPED")
    add(obs, "forge.change_requests.oldest_open_age_days", 1, "duration")
    add(obs, MEDIAN_SECONDS, {"numerator": 600, "denominator": 1}, "duration")
    assert flow.evaluate(obs).evaluation_status == "UNKNOWN"


def test_flow_fractional_median_is_classified_exactly_and_projected_to_whole_hours():
    obs = obs_set()
    flow_inputs(obs, 1, 2, oldest=1, median_seconds=(1209599, 2))
    result = flow.evaluate(obs)
    assert result.band == "MOVING"
    assert result.derived["median_time_to_merge_seconds_28d"] == {"numerator": 1209599, "denominator": 2}
    assert result.derived["median_time_to_merge_hours_28d"] == 167
    assert "median time to merge 167h 59m" in result.explanation


def test_flow_rejects_a_non_rational_median_record():
    obs = obs_set()
    add(obs, "forge.change_requests.open_count", 1)
    add(obs, "forge.change_requests.merged_count_28d", 1)
    add(obs, "forge.change_requests.oldest_open_age_days", 1, "duration")
    add(obs, MEDIAN_SECONDS, 600, "duration")
    result = flow.evaluate(obs)
    assert result.evaluation_status == "UNKNOWN" and any(code.startswith("INVALID_INPUT:") for code in result.diagnostics)


def test_clutter_band_table():
    assert clutter.classify(0, 0, 0) == "CLEAN"
    assert clutter.classify(1, 10, 0) == "LIGHT"
    assert clutter.classify(0, 0, 1) == "LIGHT"
    assert clutter.classify(5, 100, 0) == "CLUTTERED"
    assert clutter.classify(1, 4, 0) == "CLUTTERED"
    assert clutter.classify(0, 0, 6) == "CLUTTERED"
    assert clutter.classify(25, 100, 0) == "HEAVY"
    assert clutter.classify(2, 4, 0) == "HEAVY"
    assert clutter.classify(0, 0, 20) == "HEAVY"
    assert clutter.classify(1, 3, 0) == "LIGHT"


def test_clutter_exact_evaluation():
    obs = obs_set()
    clutter_inputs(obs, issues_open=10, issues_stale=3, cr_open=4, cr_stale=2, stale_branches=2)
    result = clutter.evaluate(obs)
    assert result.band == "CLUTTERED" and result.evaluation_status == "AVAILABLE"
    assert result.derived["stale_work_ratio"] == {"num": 5, "den": 14}


def test_clutter_issues_disabled_with_no_observed_residue_is_unknown_not_clean():
    """CLU-INCOMPLETE-01 shape: before clutter.bands.v1 this was DEGRADED CLEAN, a band manufactured from absence."""
    obs = obs_set()
    add(obs, "forge.issues.open_count", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(obs, "forge.issues.stale_open_count_30d", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(obs, "forge.change_requests.open_count", 1)
    add(obs, "forge.change_requests.stale_open_count_14d", 0)
    add(obs, "git.nondefault_branches.stale_count_30d", 0)
    result = clutter.evaluate(obs)
    assert result.band is None and result.evaluation_status == "UNKNOWN" and result.rule_id == "clutter.bands.v1"
    assert "CLUTTER_INCOMPLETE_COMPONENT:issues:UNAVAILABLE" in result.diagnostics
    assert "COMPONENT_UNAVAILABLE:issues:ISSUES_DISABLED" in result.diagnostics
    assert result.derived["confirmed_burden_floor"] == "NONE" and result.derived["ratio_domain_complete"] is False


def test_clutter_issues_disabled_with_observed_residue_is_a_lower_bound():
    obs = obs_set()
    add(obs, "forge.issues.open_count", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(obs, "forge.issues.stale_open_count_30d", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(obs, "forge.change_requests.open_count", 1)
    add(obs, "forge.change_requests.stale_open_count_14d", 0)
    add(obs, "git.nondefault_branches.stale_count_30d", 4)
    result = clutter.evaluate(obs)
    assert (result.band, result.evaluation_status, result.band_semantics) == ("LIGHT", "DEGRADED", "CONSERVATIVE_LOWER_BOUND")
    assert result.possible_bands == ["LIGHT", "CLUTTERED", "HEAVY"]
    assert result.derived["confirmed_stale_branch_lower_bound"] == 4, "an undeclared retention reads a complete count as it always did"
    assert result.derived["stale_branch_count_semantics"] == "EXACT"


def test_v1_11_branch_enumeration_unavailable_cannot_emit_exact_clean():
    obs = obs_set()
    clutter_inputs(obs, issues_open=0, issues_stale=0, cr_open=0, cr_stale=0, stale_branches=0)
    obs.replace(add(obs_set(), "git.nondefault_branches.stale_count_30d", status=UNAVAILABLE, reason_code="NOT_ENUMERABLE"))
    result = clutter.evaluate(obs)
    assert result.band is None and result.evaluation_status == "UNKNOWN", "CLU-INCOMPLETE-05: not CLEAN, and since clutter.bands.v1 no band at all"
    assert "CLUTTER_INCOMPLETE_COMPONENT:branches:UNAVAILABLE" in result.diagnostics


def test_clutter_partial_branch_count_without_classified_retention_is_unknown():
    """Issue #26 as reconciled: a capped head resolution proves a floor only with CLASSIFIED retention semantics."""
    obs = obs_set()
    clutter_inputs(obs, issues_open=0, issues_stale=0, cr_open=0, cr_stale=0, stale_branches=0)
    obs.replace(add(obs_set(), "git.nondefault_branches.stale_count_30d", 3, status=PARTIAL, reason_code="BRANCH_HEADS_UNRESOLVED", coverage=SUBSET))
    result = clutter.evaluate(obs)
    assert result.band is None and result.evaluation_status == "UNKNOWN"
    assert "CLUTTER_BRANCH_FLOOR_NOT_PROVEN:UNDECLARED" in result.diagnostics
    assert "COMPONENT_PARTIAL:branches:BRANCH_HEADS_UNRESOLVED" in result.diagnostics

    add(obs, "git.nondefault_branches.retention_semantics", "CLASSIFIED", "enum")
    result = clutter.evaluate(obs)
    assert (result.band, result.evaluation_status, result.band_semantics) == ("LIGHT", "DEGRADED", "CONSERVATIVE_LOWER_BOUND")
    assert result.derived["stale_branch_count_semantics"] == "LOWER_BOUND"


def test_clutter_partial_branch_count_that_is_unclassified_proves_nothing():
    obs = obs_set()
    clutter_inputs(obs, issues_open=0, issues_stale=0, cr_open=0, cr_stale=0, stale_branches=0)
    obs.replace(add(obs_set(), "git.nondefault_branches.stale_count_30d", 30, status=PARTIAL, reason_code="BRANCH_HEADS_UNRESOLVED", coverage=SUBSET))
    add(obs, "git.nondefault_branches.retention_semantics", "UNCLASSIFIED", "enum")
    result = clutter.evaluate(obs)
    assert result.band is None and result.evaluation_status == "UNKNOWN"
    assert "CLUTTER_BRANCH_PURPOSE_UNCLASSIFIED" in result.diagnostics and "CLUTTER_BRANCH_FLOOR_NOT_PROVEN:UNCLASSIFIED" in result.diagnostics


def test_clutter_partial_count_without_a_value_is_unresolved_not_a_subset():
    obs = obs_set()
    clutter_inputs(obs, issues_open=0, issues_stale=0, cr_open=0, cr_stale=0, stale_branches=0)
    obs.replace(add(obs_set(), "forge.change_requests.stale_open_count_14d", status=PARTIAL, reason_code="PAGINATION_CAPPED"))
    result = clutter.evaluate(obs)
    assert result.band is None and result.evaluation_status == "UNKNOWN"
    assert any(code.startswith("MISSING_REQUIRED:forge.change_requests.stale_open_count_14d") for code in result.diagnostics)


def test_clutter_declared_but_unreadable_retention_semantics_fail_closed():
    obs = obs_set()
    clutter_inputs(obs, issues_open=0, issues_stale=0, cr_open=0, cr_stale=0, stale_branches=3)
    add(obs, "git.nondefault_branches.retention_semantics", "MAYBE", "enum")
    result = clutter.evaluate(obs)
    assert result.band is None and result.evaluation_status == "UNKNOWN"
    assert any(code.startswith("COMPONENT_UNRESOLVED:branch_retention") for code in result.diagnostics)


def test_clutter_unclassified_branches_that_the_work_items_already_reach_are_an_invariant_band():
    """Section 5 of PV-CLUTTER-INCOMPLETE-001: the core band equal-or-more-burdensome than the apparent branch band is DEGRADED / core / EXACT."""
    obs = obs_set()
    clutter_inputs(obs, issues_open=20, issues_stale=5, cr_open=0, cr_stale=0, stale_branches=6)
    add(obs, "git.nondefault_branches.retention_semantics", "UNCLASSIFIED", "enum")
    result = clutter.evaluate(obs)
    assert (result.band, result.evaluation_status, result.band_semantics, result.possible_bands) == ("CLUTTERED", "DEGRADED", "EXACT", None)
    assert "CLUTTER_BRANCH_PURPOSE_UNCLASSIFIED" in result.diagnostics


def test_clu_incomplete_20_provider_and_order_invariance_is_exact_over_the_whole_result():
    first = obs_set()
    add(first, "forge.issues.open_count", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(first, "forge.issues.stale_open_count_30d", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(first, "forge.change_requests.open_count", 5, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage={"complete": False, "page_size": 100, "value_semantics": "OBSERVED_SUBSET_COUNT"})
    add(first, "forge.change_requests.stale_open_count_14d", 5, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage={"complete": False, "page_size": 100, "value_semantics": "OBSERVED_SUBSET_COUNT"})
    add(first, "git.nondefault_branches.stale_count_30d", 2)
    add(first, "git.nondefault_branches.retention_semantics", "CLASSIFIED", "enum")
    second = obs_set()
    add(second, "git.nondefault_branches.retention_semantics", "CLASSIFIED", "enum")
    add(second, "forge.change_requests.stale_open_count_14d", 5, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage={"complete": False, "page_size": 30, "value_semantics": "OBSERVED_SUBSET_COUNT"})
    add(second, "forge.issues.stale_open_count_30d", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(second, "git.nondefault_branches.stale_count_30d", 2)
    add(second, "forge.change_requests.open_count", 5, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage={"complete": False, "page_size": 30, "value_semantics": "OBSERVED_SUBSET_COUNT"})
    add(second, "forge.issues.open_count", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    assert clutter.evaluate(first).to_dict() == clutter.evaluate(second).to_dict()
    assert clutter.evaluate(first).band == "CLUTTERED"


# --------------------------------------------------------------------------- CLU-PARTIAL-TRUST-01..08 (PV-REV-PR-031-003)


def _partial_work(coverage, value=5):
    """Issues disabled, and a partial change-request enumeration that observed ``value`` stale items out of ``value`` open."""
    obs = obs_set()
    add(obs, "forge.issues.open_count", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(obs, "forge.issues.stale_open_count_30d", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(obs, "forge.change_requests.open_count", value, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage=coverage)
    add(obs, "forge.change_requests.stale_open_count_14d", value, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage=coverage)
    add(obs, "git.nondefault_branches.stale_count_30d", 0)
    return obs


def test_clu_partial_trust_01_a_proven_subset_of_stale_work_contributes_its_observed_count():
    result = clutter.evaluate(_partial_work(SUBSET))
    assert (result.band, result.evaluation_status, result.band_semantics) == ("CLUTTERED", "DEGRADED", "CONSERVATIVE_LOWER_BOUND")
    assert result.derived["confirmed_stale_work_lower_bound"] == 5
    assert not any(code.startswith(clutter.PARTIAL_NOT_TRUSTED) for code in result.diagnostics)


def test_clu_partial_trust_02_a_proven_subset_of_branches_with_classified_retention_contributes():
    obs = obs_set()
    clutter_inputs(obs, issues_open=0, issues_stale=0, cr_open=0, cr_stale=0, stale_branches=0)
    obs.replace(add(obs_set(), "git.nondefault_branches.stale_count_30d", 20, status=PARTIAL, reason_code="BRANCH_HEADS_UNRESOLVED", coverage=SUBSET))
    add(obs, "git.nondefault_branches.retention_semantics", "CLASSIFIED", "enum")
    result = clutter.evaluate(obs)
    assert (result.band, result.evaluation_status, result.band_semantics) == ("HEAVY", "DEGRADED", "EXACT")
    assert result.derived["confirmed_stale_branch_lower_bound"] == 20


@pytest.mark.parametrize("coverage", [None, {"complete": False}, {"complete": False, "page_size": 30}], ids=["none", "complete-flag-only", "pagination-only"])
def test_clu_partial_trust_03_a_partial_count_without_subset_proof_cannot_establish_a_floor(coverage):
    result = clutter.evaluate(_partial_work(coverage, value=30))
    assert result.band is None and result.evaluation_status == "UNKNOWN", "30 unproven stale items would have been a HEAVY floor"
    for oid in ("forge.change_requests.open_count", "forge.change_requests.stale_open_count_14d"):
        assert f"CLUTTER_PARTIAL_NOT_TRUSTED:{oid}:PARTIAL_SUBSET_NOT_PROVEN" in result.diagnostics


@pytest.mark.parametrize("semantics", ["ESTIMATE", "AGGREGATE_ONLY", "UNKNOWN", "SAMPLED"])
def test_clu_partial_trust_04_coverage_that_describes_a_non_subset_cannot_establish_a_floor(semantics):
    result = clutter.evaluate(_partial_work({"complete": False, "value_semantics": semantics}, value=30))
    assert result.band is None and result.evaluation_status == "UNKNOWN"
    assert f"CLUTTER_PARTIAL_NOT_TRUSTED:forge.change_requests.stale_open_count_14d:PARTIAL_NOT_AN_OBSERVED_SUBSET:{semantics}" in result.diagnostics


@pytest.mark.parametrize(
    "coverage, value, reason",
    [
        ("OBSERVED_SUBSET_COUNT", 30, "PARTIAL_COVERAGE_MALFORMED"),
        ({"complete": True, "value_semantics": "OBSERVED_SUBSET_COUNT"}, 30, "PARTIAL_COVERAGE_CONTRADICTS_STATUS"),
        ({"complete": "false", "value_semantics": "OBSERVED_SUBSET_COUNT"}, 30, "PARTIAL_COVERAGE_MALFORMED"),
        ({"value_semantics": "OBSERVED_SUBSET_COUNT"}, 30, "PARTIAL_COVERAGE_MALFORMED"),
        ({"complete": False, "value_semantics": ["OBSERVED_SUBSET_COUNT"]}, 30, "PARTIAL_COVERAGE_MALFORMED"),
        (SUBSET, -3, "PARTIAL_VALUE_NOT_A_COUNT"),
        (SUBSET, "30", "PARTIAL_VALUE_NOT_A_COUNT"),
        (SUBSET, True, "PARTIAL_VALUE_NOT_A_COUNT"),
    ],
    ids=["not-an-object", "complete-true", "complete-not-boolean", "complete-absent", "semantics-not-a-string", "negative", "string", "boolean"],
)
def test_clu_partial_trust_05_malformed_or_contradictory_coverage_fails_closed_without_an_exception(coverage, value, reason):
    result = clutter.evaluate(_partial_work(coverage, value=value))
    assert result.band is None and result.evaluation_status == "UNKNOWN"
    assert f"CLUTTER_PARTIAL_NOT_TRUSTED:forge.change_requests.stale_open_count_14d:{reason}" in result.diagnostics


def test_clu_partial_trust_06_an_untrusted_member_is_not_counted_beside_trusted_ones():
    """A proven open count and an unproven stale count of one component: the component is unresolved, not half counted."""
    obs = obs_set()
    add(obs, "forge.issues.open_count", 10, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage=SUBSET)
    add(obs, "forge.issues.stale_open_count_30d", 30, status=PARTIAL, reason_code="PAGINATION_CAPPED")
    add(obs, "forge.change_requests.open_count", 0)
    add(obs, "forge.change_requests.stale_open_count_14d", 0)
    add(obs, "git.nondefault_branches.stale_count_30d", 3)
    result = clutter.evaluate(obs)
    assert result.band is None and result.evaluation_status == "UNKNOWN"
    assert "CLUTTER_PARTIAL_NOT_TRUSTED:forge.issues.stale_open_count_30d:PARTIAL_SUBSET_NOT_PROVEN" in result.diagnostics
    assert not any(code.startswith("CLUTTER_PARTIAL_NOT_TRUSTED:forge.issues.open_count") for code in result.diagnostics), "the proven member is not blamed"

    # The higher-precedence rule is unchanged: an unproven required change-request count is missing required evidence.
    required = _partial_work({"complete": False}, value=30)
    result = clutter.evaluate(required)
    assert any(code.startswith("MISSING_REQUIRED:forge.change_requests.stale_open_count_14d:PARTIAL/FRESH") for code in result.diagnostics)


def test_clu_partial_trust_07_derived_and_saved_evidence_reach_the_same_decision():
    """The proof is carried by the evidence: derived from a capped enumeration, then saved and reloaded, one decision."""
    now = "2026-09-05T12:00:00Z"
    stale_open = [
        {"number": i, "title": "stale", "state": "OPEN", "created_at": "2026-06-01T00:00:00Z", "updated_at": "2026-07-01T00:00:00Z",
         "merged_at": None, "closed_at": None, "target_id": None, "target_state": None, "url": f"u{i}"}
        for i in range(1, 8)
    ]
    live = obs_set(now)
    add(live, normalize.INV_CRS, stale_open, "series", status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage={"open_complete": False, "window_complete": False})
    add(live, "forge.issues.open_count", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(live, "forge.issues.stale_open_count_30d", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(live, "git.nondefault_branches.stale_count_30d", 0)
    normalize.derive(live, single_project("acme/widget"))
    stale = live.get("forge.change_requests.stale_open_count_14d")
    assert stale.status == PARTIAL and stale.coverage["value_semantics"] == "OBSERVED_SUBSET_COUNT"

    saved = ObservationSet.from_dict(canonical.loads(canonical.pretty_json(live.to_dict())))
    decided = clutter.evaluate(live).to_dict()
    assert decided == clutter.evaluate(saved).to_dict()
    assert (decided["band"], decided["evaluation_status"]) == ("CLUTTERED", "DEGRADED")


def test_clu_partial_trust_08_complete_and_optional_unavailable_evidence_are_unchanged():
    exact = obs_set()
    clutter_inputs(exact, issues_open=10, issues_stale=3, cr_open=4, cr_stale=2, stale_branches=2)
    result = clutter.evaluate(exact)
    assert (result.band, result.evaluation_status, result.band_semantics) == ("CLUTTERED", "AVAILABLE", "EXACT")

    optional = obs_set()
    add(optional, "forge.issues.open_count", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(optional, "forge.issues.stale_open_count_30d", status=UNAVAILABLE, reason_code="ISSUES_DISABLED")
    add(optional, "forge.change_requests.open_count", 1)
    add(optional, "forge.change_requests.stale_open_count_14d", 0)
    add(optional, "git.nondefault_branches.stale_count_30d", 4)
    result = clutter.evaluate(optional)
    assert (result.band, result.evaluation_status, result.band_semantics) == ("LIGHT", "DEGRADED", "CONSERVATIVE_LOWER_BOUND")
    assert not any(code.startswith(clutter.PARTIAL_NOT_TRUSTED) for code in result.diagnostics)


def test_a_derived_count_proves_its_subset_only_when_partial_and_only_as_a_count():
    """Complete evidence keeps its coverage bytes (and bundle identity); a median never claims to be a lower bound."""
    now = "2026-09-05T12:00:00Z"
    items = [
        {"number": 1, "title": "m", "state": "MERGED", "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T10:00:00Z",
         "merged_at": "2026-09-01T10:00:00Z", "closed_at": "2026-09-01T10:00:00Z", "target_id": None, "target_state": None, "url": "u1"},
    ]
    complete = obs_set(now)
    add(complete, normalize.INV_CRS, items, "series", coverage={"open_complete": True, "window_complete": True})
    normalize.derive(complete, single_project("acme/widget"))
    assert "value_semantics" not in complete.get("forge.change_requests.merged_count_28d").coverage

    capped = obs_set(now)
    add(capped, normalize.INV_CRS, items, "series", status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage={"open_complete": False, "window_complete": False})
    normalize.derive(capped, single_project("acme/widget"))
    assert capped.get("forge.change_requests.merged_count_28d").coverage["value_semantics"] == "OBSERVED_SUBSET_COUNT"
    median = capped.get(MEDIAN_SECONDS)
    assert median.status == PARTIAL and "value_semantics" not in median.coverage


def test_t5_issue_only_provenance_needs_every_channel_positively_observed():
    """`PULSE_ISSUE_ONLY_ACTIVITY` says where every observed event came from; with a channel
    unobserved that is a claim about evidence nobody has, so it is not made."""
    exact = obs_set()
    pulse_inputs(exact, commits=0, active_days=0, cr_updates=0, issue_updates=1)
    result = pulse.evaluate(exact)
    assert result.band == "QUIET" and "PULSE_ISSUE_ONLY_ACTIVITY" in result.diagnostics

    unobserved = obs_set()
    pulse_inputs(unobserved, commits=0, active_days=0, cr_updates=None, issue_updates=1)
    add(unobserved, "forge.change_requests.updated_count_28d", status=UNAVAILABLE, reason_code="CHANGE_REQUESTS_UNAVAILABLE")
    result = pulse.evaluate(unobserved)
    assert result.band == "QUIET" and result.evaluation_status == "DEGRADED"
    assert "PULSE_ISSUE_ONLY_ACTIVITY" not in result.diagnostics

    capped = obs_set()
    add(capped, "git.default_branch.commits.count_28d", 0, status=PARTIAL, reason_code="PAGINATION_CAPPED")
    add(capped, "git.default_branch.commit_active_days_28d", 0, status=PARTIAL, reason_code="PAGINATION_CAPPED")
    add(capped, "forge.change_requests.updated_count_28d", 0)
    add(capped, "forge.issues.updated_count_28d", 1)
    result = pulse.evaluate(capped)
    assert result.evaluation_status == "DEGRADED" and "PULSE_ISSUE_ONLY_ACTIVITY" not in result.diagnostics


# --------------------------------------------------------------------------- review of 2026-09-30: Pulse bounded inference


def test_a_capped_enumeration_over_all_29_dates_the_window_touches_is_evaluated_not_a_crash():
    """The 28-day window touches 29 UTC dates; the completion grid stopped at 28 and was empty."""
    observed_at = "2026-09-30T12:00:00Z"
    items = [{"sha": f"c{day:02d}", "committed_at": f"2026-09-{day:02d}T13:00:00Z", "title": "x"} for day in range(2, 31)]
    for status in ("AVAILABLE", "PARTIAL"):
        obs = obs_set(observed_at)
        add(obs, normalize.INV_COMMITS, items, "series", status=status, reason_code=None if status == "AVAILABLE" else "PAGINATION_CAPPED", coverage={"complete": status == "AVAILABLE"})
        normalize.derive(obs, single_project("acme/widget"))
        assert obs.value_of("git.default_branch.commit_active_days_28d") == 29
        result = pulse.evaluate(obs)
        assert result.band == "SURGING", status


def test_every_band_a_completion_reaches_is_in_the_possible_set():
    """Brute force over capped inputs: the grid must contain every threshold, the QUIET one included."""
    from devostasis.policy import PULSE

    for commits in range(0, 12):
        for days in range(0, 6):
            for issue_updates in (0, 3):
                obs = obs_set()
                add(obs, "git.default_branch.commits.count_28d", commits, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage=SUBSET)
                add(obs, "git.default_branch.commit_active_days_28d", days, status=PARTIAL, reason_code="PAGINATION_CAPPED", coverage=SUBSET)
                add(obs, "forge.change_requests.updated_count_28d", 0)
                add(obs, "forge.issues.updated_count_28d", issue_updates)
                result = pulse.evaluate(obs)
                reached = {
                    pulse.classify(d, e + issue_updates, (1 if e > 0 else 0) + (1 if issue_updates else 0))
                    for d in range(days, PULSE["window_days"] + 2)
                    for e in range(commits, commits + 60)
                }
                assert reached <= set(result.possible_bands or [result.band]), (commits, days, issue_updates, reached, result.possible_bands)
