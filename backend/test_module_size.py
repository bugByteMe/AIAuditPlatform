from pathlib import Path
import unittest


MAX_PYTHON_LINES = 500


class ModuleSizeTest(unittest.TestCase):
  def test_backend_python_modules_stay_below_size_limit(self) -> None:
    backend = Path(__file__).resolve().parent
    oversized = {
      path.relative_to(backend).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
      for path in backend.rglob("*.py")
      if "__pycache__" not in path.parts
      and len(path.read_text(encoding="utf-8").splitlines()) > MAX_PYTHON_LINES
    }
    self.assertEqual(oversized, {}, f"Python modules must not exceed {MAX_PYTHON_LINES} lines")
