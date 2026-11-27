#!/usr/bin/env python
"""观澜 · 启动器停止逻辑回归集。

对应真实踩过的坑：用官方包装器 start-guanlan-clean.py 起的服务，
`launch.py --stop` 会对着自己人喊「端口被别的程序占着」——
因为 is_guanlan_server 只认命令行里的 server.py 完整路径。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "vendor"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import launch  # noqa: E402


class IsGuanlanServerTests(unittest.TestCase):

    def _cmdline(self, text: str):
        return mock.Mock(stdout=text.encode("ascii"))

    def test_direct_server_py_path_is_recognized(self):
        cmd = f'"pythonw.exe" -u "{ROOT / "server.py"}" --lan'
        with mock.patch.object(launch.subprocess, "run", return_value=self._cmdline(cmd)):
            self.assertTrue(launch.is_guanlan_server(100))

    def test_wrapper_script_is_recognized(self):
        """runpy 把 server.py 跑在包装器里，命令行没有 server.py。"""
        cmd = r'"pythonw.exe" -u "E:\WORKbuddy\2026-11-10-10-35-21\start-guanlan-clean.py"'
        with mock.patch.object(launch.subprocess, "run", return_value=self._cmdline(cmd)):
            self.assertTrue(launch.is_guanlan_server(100))

    def test_unrelated_python_is_rejected(self):
        cmd = r'"python.exe" "C:\work\jupyter-lab.py"'
        with mock.patch.object(launch.subprocess, "run", return_value=self._cmdline(cmd)):
            self.assertFalse(launch.is_guanlan_server(100))

    def test_empty_cmdline_is_rejected(self):
        with mock.patch.object(launch.subprocess, "run", return_value=self._cmdline("")):
            self.assertFalse(launch.is_guanlan_server(100))


class StopTests(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.pidfile = Path(self.td.name) / "server.pid"
        for name, value in (("PIDFILE", self.pidfile),
                            ("CREATE_NO_WINDOW", 0)):
            p = mock.patch.object(launch, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _patch(self, name, value):
        p = mock.patch.object(launch, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _mock_run(self):
        calls: list[list[str]] = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(stdout=b"")

        self._patch("subprocess", mock.Mock(run=run))
        return calls

    def test_stops_own_wrapper_process(self):
        self.pidfile.write_text("4321")
        self._patch("is_guanlan_server", lambda pid: pid == 4321)
        self._patch("pid_on_port", lambda: None)
        self._patch("port_open", lambda: False)
        calls = self._mock_run()
        self.assertEqual(launch.stop(), 0)
        self.assertTrue(any(c[:1] == ["taskkill"] and "4321" in c for c in calls))
        self.assertFalse(self.pidfile.exists())      # 停完清掉 pid 文件

    def test_refuses_unknown_program_on_port(self):
        self.pidfile.write_text("")                  # 没有 pid 文件内容
        self._patch("is_guanlan_server", lambda pid: False)
        self._patch("pid_on_port", lambda: 9999)     # 端口上是别人的程序
        self._patch("port_open", lambda: True)
        calls = self._mock_run()
        self.assertEqual(launch.stop(), 1)
        self.assertFalse(any(c[:1] == ["taskkill"] for c in calls))

    def test_reports_not_running(self):
        self.pidfile.write_text("")
        self._patch("is_guanlan_server", lambda pid: False)
        self._patch("pid_on_port", lambda: None)
        self._patch("port_open", lambda: False)
        self._mock_run()
        self.assertEqual(launch.stop(), 0)


if __name__ == "__main__":
    unittest.main()
