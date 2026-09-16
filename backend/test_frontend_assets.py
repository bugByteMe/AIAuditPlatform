import re
import unittest
from pathlib import Path

from server import static_content_type


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_ROOT = PROJECT_ROOT / "frontend"


class FrontendAssetTests(unittest.TestCase):
    def test_javascript_uses_a_module_compatible_content_type(self) -> None:
        self.assertEqual("text/javascript", static_content_type(Path("app.js")))
        self.assertEqual("text/javascript", static_content_type(Path("module.mjs")))

    def test_app_named_imports_exist_in_nonempty_local_modules(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        imports = re.finditer(
            r'import\s*\{(?P<names>.*?)\}\s*from\s*"(?P<path>\./js/[^"]+)"',
            app_source,
            re.DOTALL,
        )

        checked_modules = 0
        for imported in imports:
            module_path = FRONTEND_ROOT / imported.group("path")
            module_source = module_path.read_text(encoding="utf-8")
            self.assertTrue(module_source.strip(), f"{module_path.name} must not be empty")

            exported_names = set(
                re.findall(
                    r"export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)",
                    module_source,
                )
            )
            imported_names = {
                name.strip().split(" as ", 1)[0]
                for name in imported.group("names").split(",")
                if name.strip()
            }
            self.assertEqual(
                set(),
                imported_names - exported_names,
                f"{module_path.name} is missing imports required by app.js",
            )
            checked_modules += 1

        self.assertGreater(checked_modules, 0)


if __name__ == "__main__":
    unittest.main()
