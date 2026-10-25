"""Pins how engine-reported states are read: a state taas doesn't know keeps the job polled."""

from app.vocabulary import EngineState, PollAction, PollStatus
from app.workers.poller import classify_poll_result


def test_known_engine_state_is_parsed() -> None:
    assert EngineState.parse("done") is EngineState.DONE


def test_unknown_engine_state_is_unknown() -> None:
    assert EngineState.parse("paused") is EngineState.UNKNOWN  # a newer engine's state
    assert EngineState.parse(None) is EngineState.UNKNOWN  # no status field at all


def test_unknown_engine_state_keeps_polling() -> None:
    status = PollStatus(EngineState.parse("paused"))

    action = classify_poll_result(status, retryable=False, requeues=0, max_requeues=3)

    assert action is PollAction.RUNNING  # not failed over a state taas can't read
