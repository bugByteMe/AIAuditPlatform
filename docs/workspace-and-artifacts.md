# Workspace and Artifacts

## Workspace Model

A workspace is a managed directory on the shared filesystem. It contains the files used by Codex for one audit task or related set of tasks.

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

The active workspace directory is the mutable working tree used by a Codex run. Snapshots should not be stored as full directory copies because audit workspaces may contain large binary files.

## Group Disk Accounting

Administrative disk usage is logical current-workspace usage rather than physical storage consumption. A user's usage is the sum of `sizeBytes` for workspaces they own, and a group's usage is the sum for all current members. Forked workspaces count their full logical size even when content-addressed blobs are deduplicated.

Groups are unlimited unless an administrator sets `diskLimitBytes`. Workspace creation, uploads, replacements, forks, and new runs check the owner's group before proceeding. Deleting files or workspaces is always allowed so an over-limit group can recover. An administrator may set a limit below current usage, which freezes growth and new runs.

Agent writes are not continuously metered inside the running container. A run that starts below the limit may finish above it; the final snapshot refreshes usage, and subsequent growth or runs are blocked until usage is reduced or the limit is raised. Snapshots, blobs, previews, bundles, chat files, and Codex homes are excluded from quota accounting.

Instead, snapshots use content-addressed storage:

- File content is stored as immutable blobs keyed by checksum, for example `blobs/sha256/ab/cd/<hash>`.
- Identical file content is stored once, even if referenced by many workspaces, snapshots, or artifacts.
- Snapshot manifests reference blobs by checksum and store file metadata.
- The metadata database stores snapshot records, parent relationships, and manifest locations.

This model gives Git-like deduplication without depending on Git semantics for user-uploaded folders and binary audit files.

## Permissions

Workspaces can be:

- Private to the owner.
- Shared with the owner's group.
- Forked by a user with access.

Group-shared workspaces support collaborative access. Users with access can inspect files, start chats when no mutating run is active, and download artifacts.

To prevent conflicting agent writes, only one mutating Codex run may be active for a workspace at a time. Other users can view, fork, or wait until the active run stops or completes.

## Forking

Forking creates a new workspace from a source workspace snapshot. The fork has a new owner, independent permissions, and independent chat history.

Fork metadata should preserve:

- Source workspace.
- Source snapshot.
- Fork creator.
- Fork timestamp.

## Snapshots and Checkpoints

The system records workspace snapshots for:

- Initial upload state.
- Chat stop and resume.
- Chat completion.
- Workspace fork source points.
- Artifact comparison.

A snapshot is a manifest of workspace file state, not a full copy of every file. Each manifest entry should include:

- Relative file path.
- Content checksum.
- File size.
- Modification time.
- File mode when relevant.
- Blob storage location or blob identifier.

Each snapshot should also record:

- Workspace.
- Parent snapshot, when one exists.
- Creating user or run.
- Creation timestamp.
- Snapshot reason, such as upload, stop, resume, completion, or fork.

Checkpoint creation should:

1. Scan the active workspace directory.
2. Compute checksums for new or changed files.
3. Add missing blobs to content-addressed storage.
4. Write a new snapshot manifest.
5. Link the snapshot to the previous snapshot.

The active workspace directory can remain materialized on the shared filesystem for normal browsing and future runs. Historical versions are reconstructed from manifests and blobs only when needed.

## Snapshot Diffs

The backend computes diffs by comparing two snapshot manifests:

- Same path and same checksum: unchanged.
- Same path and different checksum: modified.
- Path exists only in the newer manifest: added.
- Path exists only in the older manifest: deleted.
- Same checksum at a different path: possible rename or copy.

The primary diff used for artifacts is the pre-run snapshot compared with the post-run snapshot. The primary diff used for workspace history is any snapshot compared with its parent.

Line-level text diffs are optional and should be generated on demand for supported text files. For common audit binaries such as spreadsheets, PDFs, archives, and scanned documents, the system should treat checksum changes as file-level modifications.

## Artifact Detection

Artifacts are files created or modified by a Codex run. The backend detects artifacts by comparing the pre-run snapshot manifest with the post-run snapshot manifest.

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

Because uploaded audit data is sensitive, the system should support configurable retention policies for:

- Uploaded workspace files.
- Workspace snapshots.
- Unreferenced content-addressed blobs.
- Chat transcripts and run events.
- Artifact bundles.

Deleting a workspace should remove or tombstone associated manifests, active workspace files, and artifact records according to the configured retention policy.

Blob deletion should be reference-counted or mark-and-sweep:

- Keep blobs referenced by any retained snapshot or artifact.
- Mark blobs with no retained references as garbage.
- Delete garbage only after a retention grace period.

This prevents one workspace deletion from removing file content still referenced by another workspace, fork, snapshot, or artifact.

User and group cascade deletion applies the same retained-reference rule: owned workspaces, manifests, previews, chats, and Codex homes are removed, then only blobs unreferenced by every retained snapshot are collected.
