function normalized(value) {
  return String(value || "").trim().toLocaleLowerCase();
}

export function adminGroupMatches(group, accounts, query) {
  const needle = normalized(query);
  if (!needle) return true;
  if (normalized(group?.name).includes(needle)) return true;
  return accounts.some((account) => [account.displayName, account.username || account.id]
    .some((value) => normalized(value).includes(needle)));
}
