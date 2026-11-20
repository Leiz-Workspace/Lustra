# Sakugabooru (sk) — extension specs

- Site: https://www.sakugabooru.com/
- Engine: moebooru_json (Moebooru JSON API: post.json), confidence high
- robots.txt: found, crawl-delay 0.0 s
- Tag list: `sk_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1): `tag.json?limit=1000&page=N&order=name` until a short page. Lustra runs it only when the user asks (Tags A-Z, "Cập nhật"). The list in the zips is made with `lustra-core tags --modules sk` (robots.txt applies). Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lustra.extgen (template)

## Terms (read 2026-11-19)
- https://www.sakugabooru.com/static/terms_of_service: nothing about automated access (same text as yande.re); /help/api: Moebooru API, 100 posts per request at most, 421 when throttled. robots.txt (Cloudflare managed): `Allow: /` for every agent, AI training crawlers disallowed by name, `Content-Signal: ai-train=no`.
- 1 request per second. Fixtures: recorded with `western`, because the posts of most tags link their YouTube source and the domain denylist scan (R7) reads the fixtures too.

## Evidence
- `https://www.sakugabooru.com/index.php?page=dapi&s=post&q=index&limit=1` → 404 text/html
- `https://www.sakugabooru.com/posts.json?limit=1` → 404 application/json
- `https://www.sakugabooru.com/post.json?limit=1` → 200 application/json

## Test runs (2026-11-19)
- `record --query "western" --limit 3`: ok (count 17,313, 3 file links); `check`: ok (replay, same posts).
- A real download run with the core: the files downloaded (then deleted).

## Shipped tag list
- `python -m lustra.tags_updater --modules sk --min-posts 2` (the extension's own `tags.py`, 1 request per second):
  15,511 tags, 24 requests, 24 s. `tag.json` by name, 1000 per page, ended by a short page; 22,573 rows read, 19 not valid tags skipped by the core. Compressed: 152 KB.

## Review checklist
- [x] robots.txt / terms of the site allow automated access to the paths used
- [x] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [x] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [x] file urls come from the site's own fields (no urls built for posts the site hides)
- [x] test run (`python -m lustra.extgen check <folder>`) passes
