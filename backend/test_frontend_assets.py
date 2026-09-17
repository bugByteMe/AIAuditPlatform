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

    def test_invite_and_chat_activity_ui_is_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        render_source = (FRONTEND_ROOT / "js" / "render.js").read_text(encoding="utf-8")
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="register-form"', html)
        self.assertIn('id="batch-account-form"', html)
        self.assertIn('api("/api/register"', app_source)
        self.assertIn('api("/api/accounts/batch"', app_source)
        self.assertIn("event-running-dots", render_source)
        self.assertIn("event-fold", render_source)

    def test_chat_realtime_reconciliation_is_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        chat_events_source = (FRONTEND_ROOT / "js" / "chatEvents.js").read_text(encoding="utf-8")
        self.assertIn("scheduleChatPoll(workspaceId, sessionId, generation)", app_source)
        self.assertIn("findSessionById(state.workspaces, workspaceId, sessionId)", app_source)
        self.assertIn("initializeEventCursors(state.workspaces", app_source)
        self.assertIn("sessionEventCursor", chat_events_source)

    def test_grouped_admin_actions_are_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        render_source = (FRONTEND_ROOT / "js" / "render.js").read_text(encoding="utf-8")
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="create-group-button"', html)
        self.assertIn('api("/api/groups"', app_source)
        self.assertIn("/reset-budget", app_source)
        self.assertIn("data-delete-account", render_source)
        self.assertIn("data-set-group-limit", render_source)

    def test_compute_resource_status_and_slim_header_are_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        render_source = (FRONTEND_ROOT / "js" / "render.js").read_text(encoding="utf-8")
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="chat-cpu-meter"', html)
        self.assertIn('id="chat-memory-meter"', html)
        self.assertIn('id="worker-grid"', html)
        self.assertIn('api("/api/workers")', app_source)
        self.assertIn("export function renderWorkers", render_source)
        self.assertNotRegex(html, r'<header class="topbar">\s*<div[^>]+>\s*<h1')


if __name__ == "__main__":
    unittest.main()
