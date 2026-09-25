# Workspace and Artifacts

## Workspace Model

A workspace is a managed directory on the shared filesystem. It contains the files used by Codex for one audit task or related set of tasks.

## File Tree Loading

Workspace list and detail responses include only the first three levels of the file tree. Folders on the third level report whether they contain children but do not include those children. The chat file explorer and workspace-management tree render these three levels by default and request only the direct children of a deeper folder when the user expands it.

File-tree nodes expose their workspace-relative path, absolute display level, type, and whether folder children exist and are already present in the response. The authenticated file-tree endpoint accepts a normalized folder path and a bounded depth; current browser expansion requests use a depth of one. Backend traversal stops at the requested depth and applies the same workspace permission and path-containment checks as file access.

Both frontend trees share loaded node data while retaining independent expanded/collapsed state. A workspace snapshot change invalidates lazily loaded descendants so deleted or replaced paths cannot remain visible. Folder selections are represented as subtree selections and are expanded by the backend for downloads, so selecting or operating on a folder does not require sending its full tree to the browser.

Users create workspaces by uploading files or folders through the web UI. The backend writes uploaded content into a generated workspace path and stores metadata in the database.

Folder uploads use persisted, resumable upload sessions. The browser first sends a file manifest to the control plane, which validates permissions, paths, file count, declared sizes, blocked file types, and group quota before accepting bytes. It then sends bounded binary chunks through the authenticated control-plane API. The control plane streams each chunk to a reserved compute worker without buffering the file or request body in memory.

Compute workers stage chunks under shared storage, persist per-file offsets, stream files while hashing, create missing content-addressed blobs, and apply completed files to the active workspace. Only the control plane commits workspace and snapshot metadata. New workspaces remain invisible until commit; existing workspaces hold a mutation lease from session creation through commit or cancellation.

The public upload lifecycle is:

1. `POST /api/uploads` creates a session from a manifest.
2. `PUT /api/uploads/<id>/files/<index>?offset=<bytes>` accepts idempotent chunks.
3. `GET /api/uploads/<id>` reconciles offsets and processing state after reconnects.
4. `POST /api/uploads/<id>/complete` starts worker finalization.
5. `DELETE /api/uploads/<id>` cancels a session before finalization begins.

Network progress and server processing are separate phases. The UI reports completion only after the worker result and authoritative metadata commit are complete. A browser reload requires the user to reselect the same folder; path, size, and modification time are matched to the persisted session. Expired pre-commit sessions release their quota and workspace reservations.

## Storage Model

The active workspace directory is the mutable working tree used by a Codex run. Historical content is not stored as full directory copies because audit workspaces may contain large binary files.

## Group Disk Accounting

Administrative disk usage is logical current-workspace usage rather than physical storage consumption. A user's usage is the sum of `sizeBytes` for workspaces they own, and a group's usage is the sum for all current members. Forked workspaces count their full logical size even when content-addressed blobs are deduplicated.

Groups are unlimited unless an administrator sets `diskLimitBytes`. Workspace creation, uploads, replacements, forks, and new runs check the owner's group before proceeding. Deleting files or workspaces is always allowed so an over-limit group can recover. An administrator may set a limit below current usage, which freezes growth and new runs.

Agent writes are not continuously metered inside the running container. A run that starts below the limit may finish above it; the final snapshot refreshes usage, and subsequent growth or runs are blocked until usage is reduced or the limit is raised. Snapshots, blobs, previews, bundles, chat files, and Codex homes are excluded from quota accounting.

Workspace history uses content-addressed storage backed by normalized SQLite metadata:

- File content is stored as immutable blobs keyed by checksum, for example `blobs/sha256/ab/cd/<hash>`.
- Identical file content is stored once, even if referenced by many workspaces or file versions.
- Each workspace path retains its current content and at most one immediately previous distinct content version.
- Deleted paths retain their last content as the previous version until the path changes again or the workspace is deleted.
- SQLite stores workspaces, lightweight checkpoint records, current/previous file versions, session links, and the latest artifact diff. Full file maps are not duplicated into checkpoint JSON manifests.

This model gives content deduplication without unbounded binary history or dependence on Git semantics for user-uploaded folders. Retention is bounded per normalized path; a workspace that continually creates and deletes new path names can still accumulate one retained version for every deleted path.

## Permissions

Workspaces can be:

- Private to the owner.
- Shared with the owner's group.
- Forked by a user with access.

Group-shared workspaces support collaborative access. Users with access can inspect files, start chats, and download artifacts.

The owner controls an exclusive run lock while the workspace is idle. It is disabled by default. When enabled, only one mutating Codex run may be active. When disabled, runs from different chat sessions may share the live workspace after the initiating user confirms an overwrite and artifact-attribution warning. A chat session still permits only one active run.

The operational workspace write lock remains held from the first active run until the last active run reaches a terminal state. Uploads, file replacement, deletion, workspace deletion, and forking remain blocked throughout. Concurrent terminal scans are serialized, but changes made by overlapping shared-directory runs cannot be attributed exclusively; artifact results are therefore best-effort for overlapping runs.

## Forking

Forking creates a new workspace from a source workspace snapshot. The fork has a new owner, independent permissions, and independent chat history.

Fork metadata should preserve:

- Source workspace.
- Source snapshot.
- Fork creator.
- Fork timestamp.

## Checkpoints and File History

The system records lightweight workspace checkpoints for:

- Initial upload state.
- Chat stop and resume.
- Chat completion.
- Workspace fork source points.
- Artifact comparison.

A checkpoint records a workspace lifecycle boundary and retains a lightweight identifier, parent identifier, reason, actor, and timestamp. The current file table records:

- Relative file path.
- Content checksum.
- File size.
- Modification time.
- File mode when relevant.
- Blob storage location or blob identifier.

Checkpoint creation should:

1. Scan the active workspace directory.
2. Compute checksums for new or changed files.
3. Add missing blobs to content-addressed storage.
4. Compare the scan with the current file-version rows.
5. Shift changed or deleted current content into the single previous slot and discard any displaced older version.
6. Commit the checkpoint, file versions, workspace size, and artifact diff in one SQLite transaction.
7. Delete displaced blobs only when no current or previous version in any workspace references them.

The active workspace directory remains materialized on the shared filesystem for normal browsing and future runs. Previous file versions are retained internally for bounded recovery and are not currently exposed through download or restore APIs. Historical whole-workspace checkpoints are not materializable.

## Snapshot Diffs

The backend computes a checkpoint diff by comparing the pre-scan current file rows with the new workspace scan:

- Same path and same checksum: unchanged.
- Same path and different checksum: modified.
- Path exists only in the newer manifest: added.
- Path exists only in the older manifest: deleted.
- Same checksum at a different path: possible rename or copy.

The primary diff used for artifacts is the current file state before a run compared with the final scan after that run.

Line-level text diffs are optional and should be generated on demand for supported text files. For common audit binaries such as spreadsheets, PDFs, archives, and scanned documents, the system should treat checksum changes as file-level modifications.

## Artifact Detection

Artifacts are files created or modified by a Codex run. The backend detects artifacts by comparing the pre-run current file rows with the final workspace scan.

Artifact records should include:

- Workspace.
- Chat session and run.
- File path.
- File size.
- Checksum.
- Blob identifier.
- Created or modified status.
- Timestamp.

Deletion records may be shown in history but are not downloadable artifacts.

## Downloads

Artifact delivery should default to changed files only. The backend builds the downloadable file set from the pre-run and post-run manifest diff:

- Added files are included.
- Modified files are included.
- Deleted files are listed in metadata only.
- Unchanged files are excluded by default.

The UI should expose:

- Download changes: default action that downloads only added and modified files.
- Download selected: user selects specific added or modified files from the diff list.
- Download full workspace: secondary action for users who need the complete current workspace state.
- Sync to local folder: optional browser enhancement that writes added and modified files into a user-selected local folder.

Every delivery path must pass workspace permission checks.

### Zip Download

Zip download is the universal fallback and should work in all supported browsers. The backend streams a package that preserves workspace-relative paths:

```text
artifacts-run-<run_id>/
  manifest.json
  files/
    reports/summary.xlsx
    output/findings.md
```

The package manifest should include:

- Workspace id.
- Chat session id.
- Run id.
- Base snapshot id.
- Result snapshot id.
- Included change types.
- File paths, sizes, checksums, blob identifiers, and change status.
- Deleted file records as metadata only.

Users can extract the zip into a chosen local folder. Because relative paths are preserved, added and modified files can overlay a local copy of the workspace.

### Sync to Local Folder

Sync to local folder is an optional enhancement for browsers that support Chromium's File System Access API. It should be shown only when the frontend detects required APIs such as `window.showDirectoryPicker`.

The sync flow should:

1. Ask the user to select a local folder through the browser permission prompt.
2. Request the same added/modified artifact set used by Download changes.
3. Write files into matching workspace-relative paths under the selected folder.
4. Create parent directories as needed.
5. Skip deleted files by default and show them as metadata/history.
6. Report per-file success and failure.

The frontend must not assume arbitrary local filesystem access. The browser must require explicit user interaction and folder permission. If permission is denied, the API is unavailable, or any file write fails, the UI should offer zip download as the fallback.

Sync should never write files outside the selected folder. Before writing, the frontend must normalize and validate artifact paths to reject absolute paths, parent-directory traversal, and invalid path segments.

## Retention

Because uploaded audit data is sensitive, the system retains:

- Uploaded workspace files.
- Lightweight checkpoint headers.
- At most one previous content version per workspace path.
- Unreferenced content-addressed blobs.
- Chat transcripts and run events.
- Artifact bundles.

Deleting a workspace removes its database records, active files, preview cache, and artifact records, then invokes blob garbage collection.

Blob deletion should be reference-counted or mark-and-sweep:

- Keep blobs referenced by any current or previous file-version row.
- Delete blobs after the database transaction only when no retained file version references them.
- A prepared upload verifies or recreates every blob from its materialized active file before publishing metadata, so collection cannot leave a committed upload with missing content.

This prevents one workspace deletion from removing file content still referenced by another workspace, fork, or retained file version.

User and group cascade deletion applies the same retained-reference rule: owned workspaces, previews, chats, and Codex homes are removed, then only blobs unreferenced by every retained file version are collected.

## Metadata Persistence and Migration

Production persists workspace, session-reference, checkpoint, bounded
file-version, and artifact metadata in PostgreSQL when
`AI_AUDIT_DATABASE_URL` is set. The actual workspace files, content-addressed
blobs, previews, bundles, upload staging, and Codex homes remain on shared
storage. Development and isolated tests use
`workspace_storage/workspace.sqlite3` when the database URL is unset.

To migrate an existing SQLite deployment, stop the control plane so workspace
metadata cannot change, back up `workspace.sqlite3`, and run:

```bash
python backend/migrate_workspace_store.py --storage-root /mnt/workspace_storage
```

The importer requires empty PostgreSQL workspace tables, copies every row in a
single target transaction, and verifies per-table row counts. It leaves the
SQLite source untouched for rollback. Start the service with the same
`AI_AUDIT_DATABASE_URL` only after validation succeeds.

Older `metadata.json` or per-checkpoint manifest JSON files are not imported.
Startup on the SQLite fallback refuses a populated legacy `metadata.json`; an
empty legacy file is tolerated and ignored.
