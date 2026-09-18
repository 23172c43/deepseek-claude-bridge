import subprocess
import sys

import launcher

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

    def wait(self, timeout=None):
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


def test_main_starts_only_shared_server(monkeypatch, tmp_path):
    captured = {}
    process = FakeProcess()

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(launcher, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(launcher, "is_port_open", lambda port: False)
    monkeypatch.setattr(
        launcher,
        "get_server_health",
        lambda port, timeout=2.0: {
            "bridge": "deepseek-claude-agent",
            "browser_ready": True,
        },
    )
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        sys,
        "argv",
        ["launcher.py", "--profile", str(tmp_path)],
    )

    assert launcher.main() == 0
    assert captured["command"][1:4] == ["-m", "uvicorn", "app.server:app"]
    assert "claude" not in captured["command"]
    assert captured["kwargs"]["cwd"] == tmp_path
