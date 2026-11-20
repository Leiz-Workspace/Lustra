# Rule34.world (rw) — extension specs

- Site: https://rule34.world/
- Engine: custom (Angular SPA rendered on the server, own JSON API `api/v2`), downloader written by hand (`DownloaderRw`, the same code as `rule34xyz` apart from the site constants: a `Downloader` like `extensions/rule34us`: search page + one request per post)
- robots.txt: found, `User-Agent: *` / `Allow: /`, no Crawl-delay, `Sitemap: https://rule34.world/sitemaps/rule34.world/index.xml`
- Interval: 1.0 s (html pages, no published limit, no Crawl-delay; R4)
- Tag list: `rw_tags.json`, made by the extension's own update `tags.py:read_tags` (manifest `tagUpdate`, SDK 1.1). Lustra runs it only when the user asks (Tags A-Z, "Cập nhật"); the shipped list was made by `python -m lustra.tags_updater --modules rw --min-posts 2`. Not committed (`.gitignore` leaves out `extensions/*/*_tags.json`): the list stays next to `manifest.json` on the maintainer's machine and goes into the draft zip (`.extgen-drafts/`) and the catalog zip (`tools/build_catalog.py`: the list next to the manifest, or `--lists <dir>`)
- Generated: lustra.extgen 2.0 r7 (`new --engine custom --no-tags`), survey 2026-11-19

## Terms (R3)
- https://rule34.world/terms (same text as https://rule34.xyz/terms), read 2026-11-19.
- Silent on bots, scrapers and automated access. Personal, non-commercial use only. Clauses for a second look by the user:
  content may be accessed "for your information and personal use solely as intended through the provided functionality of
  this Website"; "You shall not download, copy, reproduce ... any Content which are not permitted in the Terms of Service";
  no interfering with the site, its ads, age verification or security tools.
- The site's own post menu has a "Download" action for guests (the same file this extension downloads, see R6 below).
- Premium (paid in crypto) sells search filters, more search tags, a bigger blacklist and a badge; no HD / original downloads.

## Endpoints used (all GET, anonymous, no cookies)
| Use | URL | Evidence |
|---|---|---|
| Search page | `https://rule34.world/<tag1>%7C<tag2>/?page=N` (N from 1) | html rendered on the server; 30 `<a class="box" data-post-id="ID" href="/post/ID">` per page with the `pic256.jpg` thumbnail and a `<video>` preview for video posts |
| Post | `https://rule34.world/api/v2/post/<id>` | json: `id`, `created`, `posted`, `likes`, `type` (0 image, 1 video), `status` (2 posted), `width`, `height`, `files` (variant id → storage ids), `tags` (`id`, `value`, `count`, `type`), `data.sources`; 204 No Content for a missing post |
| File | `https://rule34storage.b-cdn.net/posts/<id // 1000>/<id>/<id>.pic.jpg` (image) / `.mov.mp4` (video) | see R6 |
| Tag list | `api/v2/tag/search`, `api/v2/tag-statistics/bounds`, `api/v2/tag-statistics/top-n` | see Tag list |

Not used: `api/v2/post/search/root` (the site's search API) is POST only; unknown or POST-only `api/v2/...` paths answer the SPA shell (200 text/html).

### Search syntax (checked live on rule34.xyz; the same answers on rule34.world: `overwatch/?page=400` → page 300, `d.va/` works, 6 tags = the first 5, `overwatch|-brown_hair` = `overwatch`)
- Tags: spaces as `_`, joined with `|` in the path (`/overwatch|brown_hair` → title "overwatch, brown hair", fewer posts than `/overwatch`); `%7C` works the same. `,` and `+` are not separators.
- At most 5 tags for guests: `female|video|cum|sex|brown_hair|overwatch` gives exactly the posts of the first 5 tags (the 6th is dropped silently). Manifest: `maxTags` 5 + `localPositiveOverflow` (the core keeps the 5 rarest tags in the url and checks the others locally).
- Negative tags are ignored: `/overwatch|-brown_hair` gives the posts of `/overwatch`. The downloader moves plain `-tag`s from the url to the core's own local exclusion filter (`neg_and_groups`), and refuses a `-tag` inside an OR group (it would apply to one split task only). No metatags (`metaCount` / `sort` match nothing), no wildcards (refused with a message), no favorites / pools.
- A last tag with a dot (`/d.va`, `/d.va?page=1`, `/d%2Eva`) answers 404 like a missing static file; `/d.va/` works: the downloader always ends the path with `/`.
- Depth: `?page=300` is the last page; `?page=400` / `5000` answer page 300 again (the paginator says 300). Past the last page of a smaller result: 0 posts (`yuna_lee?page=6`). `MAX_PAGES` 300 (9000 posts); deeper results need narrower tags (no id / date metatags on the site).
- No total count on the page: the downloader binary-searches the last non-empty page (~10 cached requests for a big tag, 1 for a small one). Default order: newest first (posted, ids descending).
- `?skip=` and `?p=` are ignored; `?sort=`, `?type=`, `?range=` exist in the SPA (query keys) but are not used.

### Dates, tags, types
- Date: `posted` (fallback `created`), dd-mm-yyyy; `DATE_FILTER` works by the core's page bisection (one post json per step).
- Tags: every post json lists its tags (R9); names keep the site's value with spaces as `_`.
- Tag types: 1 general, 2 copyright, 4 character, 8 artist (chip classes and the Trends page titles); 32 seen on `16:9` (metadata); 16 is left out of the site's own page titles (taken as metadata); unknown → general.

## R6: the file url
- The search page gives only thumbnails (`<id>.pic256.jpg`, `.pic256avif.avif`, `.mov256.mp4`). The post page rendered on the server gives reduced files only: `<id>.picsmall.jpg` / `.picsmallavif.avif` (image scale "optimized", the default) and `<id>.mov480.mp4` (video player), `og:image` = `<id>.picpreview.jpg`. The post json has no url, only the `files` variant map.
- The site's own "Download" action (post menu, `main-BLPMBRMM.js`):
  `download(){let t=this.post(),o=t.type,e=o===1?100:10;if(!Ro.hasFile(t,e)){...showErrorText("You can't download this post");return}let i=this.postFileResolverService.getUrl(t,e,!0),n=o===1?"".concat(t.id,".mp4"):"".concat(t.id,".jpg");this.downloadFileService.downloadViaLink(i,n)}`
  → variant 10 for an image, 100 for a video, only when the post's `files` lists it.
- Storage → host: `app.json` `"storage":{"storages":[{"id":0,"type":0},{"id":2,"parameters":{"PullZoneName":"rule34storage"},"type":1}],"defaultReadPriority":[2,0]}`; the site's resolver (`chunk-YY26Q6EV.js`) maps type 1 (Cdn) to `https://<PullZoneName>.b-cdn.net` (or `parameters.Hostname`). Path `posts/<id // 1000>/<id>/<id>.<variant name>` as in every url the site prints.
- Variant name: 13 = `picpreview.jpg` is proven by `og:image` (the SEO code uses variant 13, else 10). The body of `postFileResolverService` (variant id → name) sits past the first 200,000 characters of the 1 MB `chunk-YY26Q6EV.js` (rule34.world serves the same build as rule34.xyz: same `ngsw.json` file list), which `extgen fetch` cannot show: 10 → `pic.jpg` and 100 → `mov.mp4` were confirmed on the CDN instead: `rule34storage.b-cdn.net/posts/1439/1439695/1439695.pic.jpg` 200 image/jpeg 501,561 bytes, JPEG frame 3240 × 2160 = the json; `1439789.mov.mp4` 200 video/mp4, track 1280 × 720 = the json (the post page gives `1439789.mov480.mp4`). The same on rule34.xyz (`4779392.pic.jpg` 3240 × 2160, `4713370.mov.mp4` 1440 × 1000).
- Never rebuilt for posts the site hides: no file when `status` ≠ 2 (the post page shows only posted / created posts), when a tag has id -10 (the post page then asks guests to sign in: `authRequiredIsEnabled = post.tags.find(e => e.id === -10)`), when `files` has no variant 10 / 100 (the site's own "You can't download this post"), or when the storage is not the known CDN. Those posts are filtered out (`_execute_module_filters`).
- Premium sells no HD or original downloads: nothing paid is downloaded.

## Tag list (`tags.py`, cost)
- No GET index of tags: `api/v2/tag/search?searchText=zelda` (also `&take=100`, `text=`) answers the same 20 most popular tags (the search text is read from a POST body); `api/v2/tag/<id>`, `tag/initial` answer the SPA shell; no tags page in the SPA routes. The sitemap `sitemaps/rule34.xyz/tags_1.xml.gz` of rule34.xyz is gzip (`application/x-gzip`, 197,764 bytes, names only; the rule34.world one was not fetched): `tags.py` may not decompress it.
- Method: the Trends rankings, `api/v2/tag-statistics/top-n?periodType=P&source=S&periodStart=YYYY-MM-DD&tagType=T&limit=100` (100 tags with `value`, `count` = posts, `type`; `limit` above 100 still answers 100), for tag types 1 / 8 / 4 / 2 (16 and 32 answer nothing), metrics likes / views, every year and month of `tag-statistics/bounds` and the newest 8 weeks, plus `tag/search` (top 20). Each tag once; names with `_`.
- Bounds (2026-11-19): year likes 2022–2025, views 2022–2026; month likes 2022-01–2025-09, views 2022-01–2026-11; week likes none (`minDate` null: skipped), views 2024-12-02–2026-11-16.
- Cost: 1 + 6 bounds + 9 years × 4 + 104 months × 4 + 8 weeks × 4 = 491 requests, about 8 minutes at 1 request / s (core budget 5000).
- Coverage: the popular tags of each period, not every tag: no metadata tags (types 16 / 32), few rare tags (a tag missing from the list counts as the rarest when the core picks the 5 tags kept in the url). Harness (`--budget 15`): 569 unique tags from 15 requests (`female` 1,135,656 … ), stopped by the test budget as expected. Full run (2026-11-19, the shipped list): 2,160 tags, 492 requests, 491 s.
- **Tag names with HTML codes are kept on purpose** (`five_nights_at_freddy&#039;s`, `panty_&amp;_stocking_with_garterbelt`,
  `pok&#233;mon_(species)`, `&#12615;&#12615;`): they are the site's own tag names and the site searches by them; the
  unescaped forms are other tags (checked on rule34.world 2026-11-19: `/five_nights_at_freddy&%23039;s/` and
  `/five_nights_at_freddy's/` list different posts, the first one the newest; the same for `&#12615;&#12615;` / `ㅇㅇ`). The
  core accepts these names and the downloader keeps `&` in the path like the site's router. Do not unescape them in `tags.py`.

## Not supported
- Comments, favorites, pools, parent / child, metatags, wildcards, sort orders, low-res samples (`picsmall` variant id unknown).
- Results deeper than 300 pages (9000 posts) per search.

## Evidence (survey of `new`)
- `https://rule34.world/index.php?page=dapi&s=post&q=index&limit=1` → 404
- `https://rule34.world/posts.json?limit=1` → 404
- `https://rule34.world/api/v1/json/search/images?q=*&per_page=1` → 200 text/html (SPA shell)
- `https://rule34.world/` → 200 text/html
- Own posts and ids (`overwatch` page 1 starts at 1439789; rule34.xyz at 4779498), often the same files as rule34.xyz.

## Test run
- `record --query "teapie" --limit 3`: ok (count 10, 3 file links `…/<id>.pic.jpg`, 10 / 10 tagged and dated, 12 requests).
- `check`: ok: true (replay 12 requests, expected same posts).

## Review checklist
- [x] robots.txt / terms of the site allow automated access to the paths used (terms silent on bots; see the clauses above)
- [x] no login, cookie or challenge bypass; only public data
- [x] request interval is polite (1.0 s)
- [x] file urls come from the site's own Download action (variant map confirmed on the CDN, see R6), never for hidden / members only posts
- [x] test run (`python -m lustra.extgen check <folder>`) passes
