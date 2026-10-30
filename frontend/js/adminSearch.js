function normalized(value) {
  return String(value || "").trim().toLocaleLowerCase();
}

export function adminGroupMatches(group, accounts, query) {
  const needle = normalized(query);
  if (!needle) return true;
  if (normalized(group?.name).includes(needle)) return true;
  return accounts.some((account) => normalized(account.username || account.id).includes(needle));
}
