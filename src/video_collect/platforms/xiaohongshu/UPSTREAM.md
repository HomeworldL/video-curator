# Reused source

`search_state.js` adapts the browser-state JavaScript block in
[xpzouying/xiaohongshu-mcp `xiaohongshu/search.go`](https://github.com/xpzouying/xiaohongshu-mcp/blob/7797fd375aba02dcd33afe143f6bee13e5870859/xiaohongshu/search.go).

- Fixed upstream commit: `7797fd375aba02dcd33afe143f6bee13e5870859`.
- Upstream author/project: xpzouying and xiaohongshu-mcp contributors.
- License: Apache-2.0, retained in `LICENSE.apache-2.0`.
- Changes: return the already-deserialized value to Playwright; support the Vue `_rawValue` spelling.
- No publisher, comment, like, or account operation was copied.

The note/stream field names in `detail.py` are mapped from the site's public page
state, cross-checked against the same project's `xiaohongshu/types.go`.
XHS-Downloader's original-stream/stream-list selection was inspected as a second
reference; its GPL source is not imported or copied here.
