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

    def test_login_form_does_not_expose_demo_credentials(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        translations = (FRONTEND_ROOT / "js" / "i18n.js").read_text(encoding="utf-8")
        self.assertNotIn('value="chen.audit"', html)
        self.assertNotIn('value="audit123"', html)
        self.assertNotIn("auth.demo", translations)

    def test_chat_realtime_reconciliation_is_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        chat_events_source = (FRONTEND_ROOT / "js" / "chatEvents.js").read_text(encoding="utf-8")
        self.assertIn("scheduleChatPoll(workspaceId, sessionId, generation)", app_source)
        self.assertIn("findSessionById(state.workspaces, workspaceId, sessionId)", app_source)
        self.assertIn("initializeEventCursors(state.workspaces", app_source)
        self.assertIn("sessionEventCursor", chat_events_source)

    def test_concurrent_run_confirmation_and_scroll_preservation_are_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        api_source = (FRONTEND_ROOT / "js" / "api.js").read_text(encoding="utf-8")
        render_source = (FRONTEND_ROOT / "js" / "render.js").read_text(encoding="utf-8")
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="workspace-run-lock-toggle"', html)
        self.assertIn('error.code = payload.error', api_source)
        self.assertIn('error.code !== "concurrent_confirmation_required"', app_source)
        self.assertIn('request.confirmConcurrent = true', app_source)
        self.assertIn("captureEventScroll", render_source)
        self.assertIn("restoreEventScroll", render_source)

    def test_grouped_admin_actions_are_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        render_source = (FRONTEND_ROOT / "js" / "render.js").read_text(encoding="utf-8")
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="create-group-button"', html)
        self.assertIn('api("/api/groups"', app_source)
        self.assertIn("/reset-budget", app_source)
        self.assertIn("data-delete-account", render_source)
        self.assertIn("data-set-group-limit", render_source)
        self.assertIn("data-set-group-live-run-limit", render_source)
        self.assertIn("liveRunLimit", app_source)

    def test_recharge_credentials_and_fixed_products_are_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="recharge"', html)
        self.assertIn('id="recharge-view"', html)
        self.assertIn('id="recharge-qr-modal"', html)
        self.assertIn('data-recharge-amount="50"', html)
        self.assertIn('data-recharge-amount="100"', html)
        self.assertIn('data-recharge-amount="200"', html)
        self.assertIn('api("/api/recharge")', app_source)
        self.assertIn('data-copy-recharge="apiKey"', html)

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

    def test_resumable_upload_uses_chunk_and_processing_phases(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        api_source = (FRONTEND_ROOT / "js" / "api.js").read_text(encoding="utf-8")
        self.assertIn('api("/api/uploads"', app_source)
        self.assertIn("uploadChunkApi", app_source)
        self.assertIn("progress.processingUpload", app_source)
        self.assertIn('xhr.open("PUT"', api_source)
        self.assertNotIn('uploadApi(\n      "/api/workspaces"', app_source)

    def test_user_budget_inline_renaming_and_viewport_chat_layout_are_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        render_source = (FRONTEND_ROOT / "js" / "render.js").read_text(encoding="utf-8")
        chat_css = (FRONTEND_ROOT / "css" / "chat.css").read_text(encoding="utf-8")
        self.assertIn('state.user.budget', render_source)
        self.assertIn('used.toLocaleString()', render_source)
        self.assertIn('session.platformTokenUsage', render_source)
        self.assertIn('used / 1_000_000', render_source)
        self.assertIn('data-edit-name', render_source)
        self.assertIn('/chat/sessions/${encodeURIComponent(editor.id)}', app_source)
        self.assertIn('grid-template-rows: auto minmax(0, 1fr)', chat_css)
        self.assertIn('height: 100dvh', chat_css)

    def test_file_trees_load_deeper_folders_on_demand(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        render_source = (FRONTEND_ROOT / "js" / "render.js").read_text(encoding="utf-8")
        self.assertIn('new URLSearchParams({ path, depth: "1" })', app_source)
        self.assertIn("folder.childrenLoaded", app_source)
        self.assertIn("function sortTreeFiles(items)", app_source)
        self.assertIn('left.type === "folder" ? -1 : 1', app_source)
        self.assertIn("files: sortTreeFiles([...files.values()])", app_source)
        self.assertIn("workspace.files = sortTreeFiles([...files.values()])", app_source)
        self.assertIn("file.hasChildren", render_source)
        self.assertIn("state.loadingFileFolders", render_source)

    def test_chat_fork_and_persistent_delete_are_wired(self) -> None:
        app_source = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
        render_source = (FRONTEND_ROOT / "js" / "render.js").read_text(encoding="utf-8")
        self.assertIn('/chat/sessions/${encodeURIComponent(source.id)}/fork', app_source)
        self.assertIn("state.chatLastEventIds[result.session.id]", app_source)
        self.assertIn("activeSourceRunId", app_source)
        self.assertNotIn("events: source.events.map", app_source)
        self.assertIn('method: "DELETE"', app_source)
        self.assertIn('window.confirm(t("chat.confirmDelete"))', app_source)
        self.assertIn("canDeleteSessions", render_source)


if __name__ == "__main__":
    unittest.main()
