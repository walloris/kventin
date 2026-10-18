from agent.actions.action_result import ActionStatus, action_failed, classify_action_result


def test_action_result_contract() -> None:
    assert classify_action_result("clicked: ref:1") is ActionStatus.SUCCESS
    assert classify_action_result("modal_already_closed") is ActionStatus.NOOP
    assert classify_action_result("form_fill_paused_by_overlay: 1 fields") is ActionStatus.NOOP
    assert classify_action_result("modal_close_failed: still open") is ActionStatus.FAILURE
    assert classify_action_result("typed_but_value_mismatch: expected x") is ActionStatus.FAILURE
    assert classify_action_result("no_form_fields") is ActionStatus.FAILURE
    assert classify_action_result("preflight_rejected:outside_overlay") is ActionStatus.FAILURE
    assert action_failed("click_error: timeout") is True
    assert action_failed("") is True
