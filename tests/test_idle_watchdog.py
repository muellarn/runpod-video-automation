from runpod_video_automation.idle_watchdog import wait_for_idle_and_stop


def test_idle_watchdog_resets_timer_while_queue_is_active() -> None:
    class FakeRunPod:
        stopped: list[str] = []

        def stop_pod(self, pod_id: str) -> None:
            self.stopped.append(pod_id)

    class FakeComfy:
        states = iter([True, False, True, True, True])

        def queue_is_idle(self) -> bool:
            return next(self.states)

    ticks = iter([0.0, 5.0, 10.0, 20.0])
    runpod = FakeRunPod()

    wait_for_idle_and_stop(
        runpod,
        FakeComfy(),
        "pod-1",
        idle_seconds=10,
        poll_interval=0,
        monotonic=lambda: next(ticks),
        sleep=lambda _: None,
    )

    assert runpod.stopped == ["pod-1"]
