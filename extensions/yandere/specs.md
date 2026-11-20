# yande.re (yr) — extension specs

- Site: https://yande.re/
- Engine: moebooru_json (Moebooru JSON API: post.json), confidence high
- robots.txt: found, crawl-delay 0.0 s
- Tag list: `yr_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1): `tag.json?limit=1000&page=N&order=name` until a short page. Lustra runs it only when the user asks (Tags A-Z, "Cập nhật"). The list in the zips is made with `lustra-core tags --modules yr` (robots.txt applies). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lustra.extgen (template)

## Terms (read 2026-11-19)
- https://yande.re/static/terms_of_service: nothing about automated access (uploads, content, copyright, privacy). https://yande.re/help/api: "mostly compatible with Danbooru API (version 1.13.0) to make scripting easy"; 100 posts per request at most, HTTP 421 "User Throttled". robots.txt: a sitemap only.
- 1 request per second.

## Evidence
- `https://yande.re/index.php?page=dapi&s=post&q=index&limit=1` → 404 text/html
- `https://yande.re/posts.json?limit=1` → 404 text/html
- `https://yande.re/post.json?limit=1` → 200 application/json

## Test runs (2026-11-19)
- `record --query "seifuku" --limit 3`: ok (count 158,688, 3 file links); `check`: ok (replay, same posts).
- A real download run with the core: the files downloaded (then deleted).

## Shipped tag list
- `python -m lustra.tags_updater --modules yr --min-posts 2` (the extension's own `tags.py`, 1 request per second):
  90,001 tags, 138 requests, 144 s. `tag.json` by name, 1000 per page, ended by a short page; 136,753 rows read, 28 not valid tags skipped by the core. yande.re keeps few general tags (465 with 2 posts or more): most of the list is artists and characters. Compressed: 723 KB.

## Review checklist
- [x] robots.txt / terms of the site allow automated access to the paths used
- [x] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [x] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [x] test run (`python -m lustra.extgen check <folder>`) passes
