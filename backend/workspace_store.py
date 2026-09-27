from __future__ import annotations

from workspace_common import (
  StorageError, UploadedFile, ensure_under_root, generated_id, human_size, normalize_relative_path, now_string,
  parse_multipart, parse_query, parse_urlencoded_paths,
)
from workspace_core import WorkspaceCoreMixin
from workspace_files import WorkspaceFilesMixin
from workspace_lifecycle import WorkspaceLifecycleMixin
from workspace_snapshots import WorkspaceSnapshotMixin
from postgres_workspace_database import PostgresWorkspaceDatabase


class WorkspaceStore(WorkspaceCoreMixin, WorkspaceLifecycleMixin, WorkspaceSnapshotMixin, WorkspaceFilesMixin):
  """Facade preserving the workspace store API across focused services."""

  pass
