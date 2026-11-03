/* SPDX-License-Identifier: Apache-2.0
 * Adapted from xpzouying/xiaohongshu-mcp, xiaohongshu/search.go
 * commit 7797fd375aba02dcd33afe143f6bee13e5870859 (SearchAction.Search JS block).
 * Changes: return the value directly; accept Vue _rawValue as well as value/_value.
 * License and attribution: see UPSTREAM.md and LICENSE.apache-2.0 in this directory.
 */
() => {
    const feeds = window.__INITIAL_STATE__?.search?.feeds;
    if (!feeds) return null;
    return feeds.value ?? feeds._value ?? feeds._rawValue ??
        (Array.isArray(feeds) ? feeds : null);
}
