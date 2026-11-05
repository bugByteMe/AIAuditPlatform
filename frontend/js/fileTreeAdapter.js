import { t } from "./i18n.js";
import { state } from "./state.js";

const ROOT_KEY = "__workspace_root__";
const instances = new Map();
let callbacks = {};

export function configureFileTrees(nextCallbacks) {
  callbacks = { ...callbacks, ...nextCallbacks };
}

function fancytree() {
  const $ = window.jQuery;
  return $?.ui?.fancytree ? $ : null;
}

function parentPath(path) {
  const index = String(path || "").lastIndexOf("/");
  return index < 0 ? "" : path.slice(0, index);
}

function basename(path) {
  return String(path || "").split("/").pop() || "";
}

function directChildren(items, parent) {
  return (items || []).filter((item) => parentPath(item.path || item.name) === parent);
}

function nodeSource(item, items, collapsedFolders, statuses = new Map()) {
  const path = item.path || item.name;
  const folder = item.type === "folder";
  const loaded = folder && Boolean(item.childrenLoaded);
  const children = loaded
    ? directChildren(items, path).map((child) => nodeSource(child, items, collapsedFolders, statuses))
    : undefined;
  return {
    key: path,
    title: item.name || basename(path),
    folder,
    lazy: folder && Boolean(item.hasChildren) && !loaded,
    expanded: folder && Boolean(item.hasChildren) && !collapsedFolders.has(path),
    children,
    data: {
      path,
      type: item.type,
      size: item.size || "",
      status: folder ? "" : statuses.get(path) || item.status || "",
      hasChildren: Boolean(item.hasChildren),
    },
  };
}

function treeSource(config) {
  const items = config.items || [];
  const children = directChildren(items, "").map((item) => nodeSource(item, items, config.collapsedFolders, config.statuses));
  return [{
    key: ROOT_KEY,
    title: t("files.workspaceRoot"),
    folder: true,
    expanded: true,
    checkbox: false,
    unselectable: config.mode !== "destination",
    children,
    data: { path: "", type: "folder", workspaceRoot: true, hasChildren: children.length > 0 },
  }];
}

function canMutate(config) {
  return Boolean(config.mutable && !config.workspace?.locked);
}

function validName(value) {
  const name = String(value || "").trim();
  return name && name !== "." && name !== ".." && !/[\\/]/.test(name) ? name : "";
}

function decorateNode(node, config) {
  if (!node.span) return;
  const span = node.span;
  span.dataset.filePath = node.data.path ?? "";
  span.dataset.fileType = node.data.type || (node.folder ? "folder" : "file");
  span.dataset.treeMode = config.mode;
  span.classList.toggle("file-tree-root", node.key === ROOT_KEY);
  span.querySelectorAll(".app-tree-icon, .file-meta, .file-status").forEach((element) => element.remove());

  const expander = span.querySelector(".fancytree-expander");
  if (expander) {
    expander.replaceChildren();
    if (node.folder && (node.hasChildren() !== false || node.lazy)) {
      const expanderIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      expanderIcon.setAttribute("class", "ui-icon");
      expanderIcon.setAttribute("aria-hidden", "true");
      const expanderUse = document.createElementNS("http://www.w3.org/2000/svg", "use");
      expanderUse.setAttribute("href", `#icon-chevron-${node.expanded ? "down" : "right"}`);
      expanderIcon.append(expanderUse);
      expander.append(expanderIcon);
    }
  }

  const title = span.querySelector(".fancytree-title");
  if (!title) return;
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("class", "ui-icon app-tree-icon");
  icon.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#icon-${node.folder ? "folder" : "file"}`);
  icon.append(use);
  title.before(icon);

  if (node.key !== ROOT_KEY && config.mode !== "destination") {
    const meta = document.createElement("span");
    meta.className = "file-meta";
    meta.textContent = node.folder ? t("files.folder") : node.data.size || t("files.file");
    span.append(meta);
    if (node.data.status) {
      const status = document.createElement("span");
      status.className = `file-status ${node.data.status}`;
      status.textContent = t(`artifact.${node.data.status}`);
      span.append(status);
    }
  }
}

function selectedRoots(tree) {
  return tree.getSelectedNodes(true)
    .filter((node) => node.key !== ROOT_KEY)
    .map((node) => node.data.path);
}

function syncArtifactSelection(record) {
  if (record.config.mode !== "artifacts") return;
  record.suppressSelection = true;
  record.tree.visit((node) => {
    if (node.key === ROOT_KEY) return;
    const selected = [...state.selectedArtifacts].some((path) => node.data.path === path || node.data.path.startsWith(`${path}/`));
    if (node.selected !== selected) node.setSelected(selected, { noEvents: true });
  });
  record.suppressSelection = false;
}

function destinationPath(node) {
  return node?.key === ROOT_KEY ? "" : node?.data?.path || "";
}

function createOptions(record) {
  const config = record.config;
  const selectable = config.mode === "artifacts";
  const options = {
    source: treeSource(config),
    extensions: config.mode === "destination" ? [] : ["dnd5", "edit"],
    aria: true,
    escapeTitles: true,
    icon: false,
    keyboard: true,
    checkbox: selectable,
    selectMode: config.mode === "artifacts" ? 3 : 1,
    clickFolderMode: config.mode === "destination" ? 1 : 2,
    init(_event, data) {
      record.tree = data.tree;
      syncArtifactSelection(record);
      if (config.mode === "destination") {
        const selected = data.tree.getNodeByKey(config.selectedPath ? config.selectedPath : ROOT_KEY);
        selected?.setSelected(true, { noEvents: true });
        selected?.setActive(true, { noFocus: true, noEvents: true });
      }
    },
    renderNode(_event, data) {
      decorateNode(data.node, record.config);
    },
    expand(_event, data) {
      if (data.node.key !== ROOT_KEY) record.config.collapsedFolders.delete(data.node.data.path);
    },
    collapse(_event, data) {
      if (data.node.key !== ROOT_KEY) record.config.collapsedFolders.add(data.node.data.path);
    },
    lazyLoad(_event, data) {
      const loader = record.config.loadFolder || callbacks.loadFolder;
      data.result = Promise.resolve(loader?.(data.node.data.path, record.config.workspace?.id))
        .then((items) => directChildren(items || [], data.node.data.path)
          .map((item) => nodeSource(item, items || [], record.config.collapsedFolders, record.config.statuses)))
        .catch((error) => {
          callbacks.showToast?.(error?.message || t("toast.workspaceLoadFailed"));
          throw error;
        });
    },
    select(_event, data) {
      if (record.suppressSelection) return;
      if (record.config.mode === "artifacts") {
        callbacks.setArtifactSelection?.(selectedRoots(data.tree));
      } else if (record.config.mode === "destination") {
        const selected = data.tree.getSelectedNodes()[0] || data.node;
        record.config.onSelect?.(destinationPath(selected));
      }
    },
    activate(_event, data) {
      if (record.config.mode !== "destination") return;
      record.suppressSelection = true;
      data.tree.selectAll(false);
      data.node.setSelected(true, { noEvents: true });
      record.suppressSelection = false;
      record.config.onSelect?.(destinationPath(data.node));
    },
    dblclick(_event, data) {
      if (data.node.data.type === "file") callbacks.preview?.(data.node.data.path);
      return false;
    },
  };

  if (config.mode !== "destination") {
    options.edit = {
      triggerStart: ["f2"],
      beforeEdit(_event, data) {
        return data.node.key !== ROOT_KEY && canMutate(record.config);
      },
      save(_event, data) {
        const name = validName(data.input.val());
        if (!name) {
          callbacks.showToast?.(t("files.invalidName"));
          return false;
        }
        const sourcePath = data.node.data.path;
        const destination = [parentPath(sourcePath), name].filter(Boolean).join("/");
        if (sourcePath !== destination) {
          Promise.resolve(callbacks.movePath?.(sourcePath, destination))
            .catch(() => data.node.setTitle(data.orgTitle));
        }
        return true;
      },
    };
    options.dnd5 = {
      autoExpandMS: 650,
      preventRecursion: true,
      preventVoidMoves: true,
      dragStart(node) {
        return node.key !== ROOT_KEY && canMutate(record.config);
      },
      dragEnter(node, data) {
        if (!node.folder || !canMutate(record.config)) return false;
        const sourcePath = data.otherNode?.data?.path || "";
        const targetPath = destinationPath(node);
        if (targetPath === sourcePath || targetPath.startsWith(`${sourcePath}/`)) return false;
        return ["over"];
      },
      dragDrop(node, data) {
        const sourcePath = data.otherNode?.data?.path;
        if (!sourcePath) return;
        const targetPath = destinationPath(node);
        const destination = [targetPath, basename(sourcePath)].filter(Boolean).join("/");
        if (destination !== sourcePath) {
          Promise.resolve(callbacks.movePath?.(sourcePath, destination)).catch(() => {});
        }
      },
    };
  }
  return options;
}

function signature(config) {
  return JSON.stringify({
    workspace: config.workspace?.id || config.workspaceId || "",
    snapshot: config.workspace?.latestSnapshotId || "",
    lang: state.lang,
    mode: config.mode,
    mutable: config.mutable,
    locked: config.workspace?.locked,
    selectedPath: config.mode === "destination" ? config.selectedPath : undefined,
    statuses: config.mode === "artifacts" ? [...config.statuses] : undefined,
    files: (config.items || []).map((item) => [item.path, item.type, item.childrenLoaded, item.hasChildren]),
  });
}

function renderTree(element, config) {
  if (!element) return;
  const $ = fancytree();
  if (!$) {
    element.textContent = t("files.treeUnavailable");
    return;
  }
  element.classList.add("fancytree-host");
  const nextSignature = signature(config);
  let record = instances.get(element);
  if (record && record.signature === nextSignature) {
    record.config = config;
    record.tree?.visit((node) => decorateNode(node, config));
    syncArtifactSelection(record);
    return;
  }
  if (record) $(element).fancytree("destroy");
  record = { config, signature: nextSignature, tree: null, suppressSelection: false };
  instances.set(element, record);
  element.replaceChildren();
  $(element).fancytree(createOptions(record));
  record.tree = $.ui.fancytree.getTree(element);
}

export function renderArtifactFileTree(element, workspace, statuses, mutable) {
  renderTree(element, {
    mode: "artifacts", workspace, items: workspace?.files || [], statuses,
    mutable, collapsedFolders: state.collapsedArtifactFolders,
  });
}

export function renderChatFileTree(element, workspace, mutable) {
  renderTree(element, {
    mode: "chat-files", workspace, items: workspace?.files || [], statuses: new Map(),
    mutable, collapsedFolders: state.collapsedFileFolders,
  });
}

export function renderDestinationFileTree(element, config) {
  renderTree(element, {
    ...config, mode: "destination", statuses: new Map(),
    collapsedFolders: config.collapsedFolders || new Set(),
  });
}

export function destroyFileTree(element) {
  const $ = fancytree();
  if (element && instances.has(element) && $) $(element).fancytree("destroy");
  if (element) instances.delete(element);
}

export function startFileTreeRename(path, mode) {
  for (const record of instances.values()) {
    if (record.config.mode !== mode || !canMutate(record.config)) continue;
    const node = record.tree?.getNodeByKey(path);
    if (node) {
      node.setActive(true);
      node.editStart();
      return true;
    }
  }
  return false;
}
