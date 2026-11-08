import io
import unittest
from contextlib import redirect_stdout

from mlb_manager import __version__
from mlb_manager.main import main


class EntryPointTests(unittest.TestCase):
    def test_version_flag_exits_cleanly(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(__version__, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
