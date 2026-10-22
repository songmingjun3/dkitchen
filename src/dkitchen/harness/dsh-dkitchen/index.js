/**
 * Host half of dsh-dkitchen.
 *
 * It has nothing to run: the tools come from `ding mcp-serve`, mounted by the patch, and
 * the page below is a Client module. A Loader row is still required for the browser half to
 * be served — the client module system looks the package up through the row that the patch
 * inserts, and a package with no Host half would have none.
 */
export function apply() {}
