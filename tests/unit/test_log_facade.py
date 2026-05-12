"""Test log facade: bind/unbind, level dispatch, never-raise contract."""
from __future__ import annotations

from boxagent.log import Category, LogFacade, NullLogger, log


class RecordingSink:
    def __init__(self):
        self.calls: list[tuple] = []

    def publish(self, level, category, message, **meta):
        self.calls.append((level, category, message, meta))


class RaisingSink:
    def publish(self, level, category, message, **meta):
        raise RuntimeError("sink boom")


def test_log_is_singleton_facade():
    assert isinstance(log, LogFacade)


def test_unbound_facade_is_noop_and_does_not_raise():
    facade = LogFacade()
    facade.info("scheduler.run", "msg")
    facade.warning("x", "msg", foo=1)
    facade.error("x", "msg")
    facade.notify("x", "msg")
    facade.debug("x", "msg")


def test_bind_routes_calls_to_sink():
    facade = LogFacade()
    sink = RecordingSink()
    facade.bind(sink)

    facade.info(Category.SCHEDULER_RUN, "fired", task_id="t1", bot="b1")

    assert sink.calls == [
        ("info", "scheduler.run", "fired", {"task_id": "t1", "bot": "b1"})
    ]


def test_all_five_levels_dispatch_with_correct_level_string():
    facade = LogFacade()
    sink = RecordingSink()
    facade.bind(sink)

    facade.debug("c", "m")
    facade.info("c", "m")
    facade.warning("c", "m")
    facade.error("c", "m")
    facade.notify("c", "m")

    levels = [call[0] for call in sink.calls]
    assert levels == ["debug", "info", "warning", "error", "notify"]


def test_sink_exception_is_swallowed(capsys):
    facade = LogFacade()
    facade.bind(RaisingSink())

    facade.info("c", "m")  # must not raise

    captured = capsys.readouterr()
    assert "sink boom" in captured.err or "sink failed" in captured.err


def test_unbind_restores_noop():
    facade = LogFacade()
    sink = RecordingSink()
    facade.bind(sink)
    facade.info("c", "m")
    assert len(sink.calls) == 1

    facade.unbind()
    facade.info("c", "m2")
    assert len(sink.calls) == 1  # no new call


def test_rebind_replaces_previous_sink():
    facade = LogFacade()
    sink_a = RecordingSink()
    sink_b = RecordingSink()
    facade.bind(sink_a)
    facade.bind(sink_b)

    facade.info("c", "m")

    assert sink_a.calls == []
    assert len(sink_b.calls) == 1


def test_null_logger_accepts_anything_without_raising():
    null = NullLogger()
    null.publish("info", "c", "m")
    null.publish("notify", "c", "m", a=1, b="x", nested={"k": "v"})


def test_meta_with_no_kwargs_yields_empty_dict():
    facade = LogFacade()
    sink = RecordingSink()
    facade.bind(sink)

    facade.info("c", "m")

    assert sink.calls[0][3] == {}
