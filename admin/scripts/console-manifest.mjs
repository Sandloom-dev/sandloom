// The one group of tools/packaging/public-source.json that names the packaged console files.
// `npm run package` rewrites that group, because hashed asset names change with each build.
// The group is one hand-ordered run (tools/agent_registry.py puts a new path beside its
// sibling), not a sorted one: only the console block is replaced, in the place it already
// holds and sorted; every other path keeps its place. Sorting the whole group instead
// reordered it on every local run and left a diff on a clean checkout.

// `paths` with every entry under `prefix` replaced by `files` (sorted), at the position of
// the first one, or at the end when there was none.
export const withConsolePaths = (paths, prefix, files) => {
  const block = [...files].sort();
  const result = [];
  let placed = false;
  for (const path of paths) {
    if (!path.startsWith(prefix)) {
      result.push(path);
    } else if (!placed) {
      result.push(...block);
      placed = true;
    }
  }
  if (!placed) result.push(...block);
  return result;
};
