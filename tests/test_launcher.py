import subprocess

from launcher import stop_process


class FakeProcess:
    def __init__(self, timeout_once=False):
        self.running = True
        self.timeout_once = timeout_once
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("fake", timeout)
        self.running = False
        return 0

    def kill(self):
        self.killed = True


def test_stop_process_terminates_and_waits():
    process = FakeProcess()
    stop_process(process)
    assert process.terminated
    assert not process.killed
    assert process.poll() == 0


def test_stop_process_kills_after_timeout():
    process = FakeProcess(timeout_once=True)
    stop_process(process)
    assert process.terminated
    assert process.killed
    assert process.poll() == 0
