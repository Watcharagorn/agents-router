import types
import unittest
from unittest.mock import patch

from agy_pty import clean_terminal_output, run_conpty


class _FakeProcess:
    exitstatus = 0

    def __init__(self):
        self._read = False

    def read(self, _size):
        if self._read:
            raise EOFError
        self._read = True
        return "PTY_OK"

    def isalive(self):
        return not self._read

    def wait(self):
        return self.exitstatus


class _FakePtyProcess:
    @staticmethod
    def spawn(_argv, cwd, dimensions):
        if not cwd or not dimensions:
            raise AssertionError("spawn arguments were not supplied")
        return _FakeProcess()


class AgyPtyTests(unittest.TestCase):
    def test_clean_terminal_output_removes_osc_window_title(self):
        raw = "\x1b[1t\x1b]0;C:\\path\\agy.exe\x1b\\RESULT\r\n"
        self.assertEqual(clean_terminal_output(raw), "RESULT")

    def test_conpty_reader_path(self):
        fake_winpty = types.SimpleNamespace(PtyProcess=_FakePtyProcess)
        with patch.dict("sys.modules", {"winpty": fake_winpty}):
            status, output = run_conpty(["agy", "--version"], ".", 2)
        self.assertEqual(status, 0)
        self.assertEqual(output, "PTY_OK")


if __name__ == "__main__":
    unittest.main()
