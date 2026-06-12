from app.workers.poller import classify_poll_result


def test_done_harvests():
    assert classify_poll_result("done", requeues=0, max_requeues=3) == "harvest"


def test_engine_failure_fails_immediately():
    assert classify_poll_result("failed", 0, 3) == "fail"
    assert classify_poll_result("error", 0, 3) == "fail"


def test_unreachable_requeues_within_budget():
    assert classify_poll_result("unreachable", 0, 3) == "requeue"
    assert classify_poll_result("unreachable", 2, 3) == "requeue"


def test_unreachable_fails_when_budget_exhausted():
    assert classify_poll_result("unreachable", 3, 3) == "fail"


def test_running_keeps_polling():
    assert classify_poll_result("running", 0, 3) == "running"
    assert classify_poll_result("unknown", 0, 3) == "running"
