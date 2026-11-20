# Gelbooru (gb) — extension specs

- Site: https://gelbooru.com/
- Engine: gelbooru_dapi (Gelbooru 0.2 DAPI (XML): index.php?page=dapi&s=post&q=index), confidence low
- robots.txt: found, crawl-delay 0.0 s
- Tag list: `gb_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1): DAPI tag index, `index.php?page=dapi&s=tag&q=index&limit=1000&pid=N` until an empty page, with the user's key. Lustra runs it only when the user asks (Tags A-Z, "Cập nhật"). The list in the zips is made with `lustra-core tags --modules gb` (robots.txt applies). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lustra.extgen (template)

## Terms (read 2026-11-19)
- https://gelbooru.com/tos.php: "You will not use any automated process to retrieve or index any portion of the site or site contents, with the exception of public search engines." Personal use only.
- API docs https://gelbooru.com/index.php?page=wiki&s=view&id=18780: keys (`&api_key=...&user_id=...`, account options) and throttling "to mitigate abusive behavior"; the API terms speak to "all bots, apps and websites" (no ads, no paywalls on API content). Anonymous DAPI requests answer HTTP 401: the user's own key is required (manifest `auth.apiKey`, capability API_KEY_REQUIRED).
- R3: the terms forbid automation and the API docs address bots and apps, so it is unclear. **Owner override, 2026-11-19:** asked "bạn cho phép vượt R3 với site nào?", the owner chose Gelbooru (with TBIB and Realbooru). Built API only: favorites / pools (html pages) are refused, 1 request per second.
- Not live-tested: `record` needs the owner's key (no fixtures). Test in the app with a key before publishing the catalog entry.

## Evidence
- `https://gelbooru.com/index.php?page=dapi&s=post&q=index&limit=1` → 401 text/html
- `https://gelbooru.com/posts.json?limit=1` → 404 text/html
- `https://gelbooru.com/post.json?limit=1` → 404 text/html
- `https://gelbooru.com/api/v1/json/search/images?q=*&per_page=1` → 404 text/html
- `https://gelbooru.com/api/posts/?limit=1` → 404 text/html
- `https://gelbooru.com/api/danbooru/find_posts?limit=1` → 404 text/html
- `https://gelbooru.com/` → 200 text/html

## Notes
- The DAPI answers 401 without a key (the survey above, made without one): the downloader uses the DAPI with the user's own
  key (`&api_key=..&user_id=..`), the same as rule34.xxx.
- No shipped list: there is no `gb_tags.json`, in the draft zip nor next to the manifest (the core skips a missing list). The
  user builds it in the app with their own key (Tags A-Z, "Cập nhật"), or the maintainer makes it with
  `LUSTRA_API_KEY_GB=KEY,USER_ID python -m lustra.tags_updater --modules gb --out <dir>` and builds the catalog with
  `tools/build_catalog.py --lists <dir>`.
- Not tested live: no fixtures, `check` reports "no recorded test run" until `record` runs with a key.

## Review checklist
- [x] robots.txt allows the paths used; the terms forbid automated access: R3 overridden by the owner (above)
- [x] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [x] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [ ] test run (`python -m lustra.extgen check <folder>`) passes: needs the owner's key (not done)
