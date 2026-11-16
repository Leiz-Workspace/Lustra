# Lustra — Specs (đặc tả)

> 2.0 r1 · 2026-11-16 · Xem thêm: [components](components.md), [acceptance criteria](acceptance-criteria.md)

## 1. Phạm vi
Lustra tải ảnh và video từ các trang booru dựa trên truy vấn tag, với bộ lọc chính xác hơn khả năng của chính các trang đó. Mỗi website là một extension. Hai extension mặc định là **Rule34** (`rx`) và **Danbooru** (`db`).

Ngoài phạm vi:
- vượt qua cơ chế chống bot, CAPTCHA, paywall;
- tải nội dung có DRM;
- crawl hàng loạt không giới hạn ("Lustra is not a scraping tool", kế thừa từ Ruxx).

## 2. Yêu cầu chức năng

| ID | Yêu cầu |
|---|---|
| F-01 | Tìm và tải theo tag, hỗ trợ tag loại trừ `-tag`, wildcard `*`, meta tag (`id:>X`, `score:>=X`, `rating:`, `date:`…), nhóm OR `(a~b)`, nhóm loại trừ `-(a,b)`, bỏ qua kiểm tra cú pháp bằng `%tag%` |
| F-02 | Chọn module (extension đã cài); mặc định có `rx` và `db` |
| F-03 | Autocomplete tag từ danh sách offline, xếp theo số post giảm dần, hiển thị loại tag |
| F-04 | Duyệt tag A–Z (và nhóm `#` cho tag không bắt đầu bằng chữ cái), lọc theo loại tag và số post tối thiểu, sắp xếp theo độ phổ biến hoặc A→Z, nút thêm/loại trừ vào truy vấn |
| F-05 | Gộp tag phức hợp trên UI: gộp OR, gộp nhóm loại trừ, đảo bao gồm/loại trừ, tách nhóm, sửa chip |
| F-06 | Đếm số kết quả (Check tags) |
| F-07 | Preview: lưới thumbnail theo trang, đánh dấu post bị lọc cục bộ, chọn/bỏ chọn/đảo, tải các post đã chọn, lightbox xem tag và thêm/loại trừ tag |
| F-08 | Tải nền theo hàng đợi, xem tiến độ (trạng thái, số file, thành công/lỗi), huỷ, mở thư mục, xem log |
| F-09 | Tuỳ chọn tải: ảnh/video, định dạng video, ảnh chất lượng thấp, khoảng ngày, số luồng, giới hạn số post, thứ tự, post cha/con, thư mục, tiền tố tên file, thêm tag vào tên, lưu info (tags, nguồn, bình luận, hash, 3 chế độ) |
| F-10 | Kết nối: proxy `http`, `https`, `socks5`, `socks5h` (host có thể là IP hoặc tên miền), tạm bỏ qua proxy, tải file không qua proxy, timeout, số lần retry, headers/cookies JSON, API key theo module (lưu mã hoá) |
| F-11 | Extensions: liệt kê; cài từ catalog cục bộ, từ file zip (bắt buộc SHA-256) hoặc từ catalog từ xa (URL + SHA-256); gỡ; không cho thay hay gỡ extension builtin |
| F-12 | Cập nhật danh sách tag: thủ công trong app, tự động theo chu kỳ khi mở app, và qua cronjob/systemd timer |
| F-13 | Giao diện: 4 theme (Ember, Graphite, Paper, OLED); 4 icon; nền là icon làm mờ (mặc định), ảnh tuỳ chọn không làm mờ, hoặc không có nền; chỉnh độ mờ, độ hiện, lớp phủ; làm mờ thumbnail (riêng tư) |
| F-14 | Giới hạn 2 tag của Danbooru: metatag miễn phí (`rating:`, `id:`, `date:`, `score:`, `status:`, `parent:`) không bị tính. Tag dương vượt giới hạn được kiểm tra cục bộ (tag hiếm nhất theo danh sách tag được gửi lên server); tag phủ định vượt giới hạn chuyển thành nhóm loại trừ cục bộ |
| F-15 | Nhận diện trang bot challenge (`cf-mitigated: challenge`): dừng ngay, không retry, báo hướng dẫn |
| F-16 | Lọc theo giới tính nhân vật, tầng 0 (chỉ dựa vào tag): `-gender only_female,only_male,both,none,unknown`, `-gender_conf low\|medium\|high` (mặc định medium), `-gender_strict` (futa/trap/mơ hồ không tính là only_*). `-gender_mode model` chưa có (P3). Bộ luật theo site nằm ở `lustra/gender_rules/*.json`; extension có thể tự mang bộ luật riêng |
| F-17 | Danh sách chặn an toàn (tag gợi ý trẻ vị thành niên, `lustra/safety.py`): **chỉ chạy khi bật bộ lọc giới tính** (quyết định 2026-11-16); khi đang lọc thì không tắt được; chạy cho mọi task kể cả task parent; post bị chặn không xuất hiện trong preview (kể cả thumbnail). Extension chỉ được thêm tag (`safetyTags`) |

## 3. Yêu cầu phi chức năng

| ID | Yêu cầu |
|---|---|
| N-01 | Tôn trọng website: mỗi module có khoảng giãn request (`_get_min_request_interval`); đọc `Retry-After` (tối đa 120 s); tối đa 8 luồng tải; updater mặc định chờ 0,5 s giữa hai request |
| N-02 | Bảo mật:
- renderer chạy `contextIsolation` và `sandbox`, không có `nodeIntegration`; CSP chặn script ngoài;
- không điều hướng ra khỏi trang app (kéo thả và script đều bị chặn); mọi IPC kiểm sender;
- API key, cookies, headers, proxy không bao giờ đi tới renderer, không nằm trong argv của CLI (dùng `LUSTRA_SECRET_ARGS`) và được ẩn trong log;
- `settings.json` quyền 0600;
- thumbnail chỉ tải từ host công khai, không chuyển hướng, giới hạn 25 MB / 20 s |
| N-03 | Extension là code Python chạy với quyền người dùng:
- cài từ zip hoặc URL cần SHA-256, https và hộp xác nhận native; file zip phải được chọn qua dialog;
- cài theo giao dịch (import thử trước khi thay, lỗi thì rollback);
- chống zip-slip và symlink; giới hạn 10 MB nén / 50 MB và 2000 file sau giải nén;
- gỡ chỉ trong `<data>/extensions` |
| N-04 | Tính độc lập: mỗi job tải là một tiến trình riêng, nên trạng thái toàn cục của lõi (module hiện tại) không bị lẫn giữa các job; sidecar tuần tự hoá các truy vấn phụ thuộc module bằng `_core_lock` |
| N-05 | Tương thích: Python ≥ 3.11 (CI chạy 3.11 → 3.15), Electron 45; lõi chạy được khi không có tkinter |
| N-06 | Hiệu năng: autocomplete < 50 ms sau khi danh sách tag đã nạp (khoảng 0,7 triệu tag, ~1 s nạp lần đầu); cache thumbnail tối đa 512 MB, dọn theo thời gian truy cập |

## 4. Giao thức sidecar (JSON-RPC 2.0, stdio, mỗi dòng một object)

Khởi động bằng `python -m lustra.server` với các biến môi trường `LUSTRA_DATA_DIR` và (tuỳ chọn) `LUSTRA_TAGS_DIR`, `LUSTRA_CATALOG_DIR`, `LUSTRA_CATALOG_URL`.

| Method | Params | Result |
|---|---|---|
| `app.info` | — | `{name, version, protocol, upstream, python, dataDir, tagsDirs}` |
| `ext.list` | — | `[manifest + {installed, loaded, source, path}]` |
| `ext.install` | `{id}` \| `{url, sha256}` (https) \| `{archivePath, sha256}` \| `{path}` (chỉ CLI/dev; app desktop không chuyển tiếp) | manifest + `restartRequired` |
| `ext.uninstall` | `{id}` | `{ok}` |
| `ext.remoteCatalog` | `{url?}` | `[{id, name, abbr, version, url, sha256, description}]` |
| `tags.autocomplete` | `{module, prefix, limit}` (prefix có thể bắt đầu bằng `-`) | `[{tag, posts, type, insert}]` |
| `tags.browse` | `{module, prefix ('a'..'z' \| '#'), offset, limit, minPosts, types[], sort: 'posts'\|'az'}` | `{total, items:[{tag, posts, type}]}` |
| `tags.validate` | `{module, tags}` | `{valid, tags[]}` |
| `tags.status` | — | `[{module, path, size, mtime, loaded}]` |
| `tags.update` | `{module, apiKey?, outDir?, delay?}` | `{job, outDir}`; sau đó gửi notification `tags.updateProgress`, `tags.updateDone` |
| `search.count` | `{module, tags, args[]}` | `{count, requiredTags[], cfChallenge}` |
| `search.preview` | `{module, tags, page, args[]}`: `args` có thể chứa `-gender …` | `{page, pageSize, items:[{id, preview, sample, isVideo, tags[], score, width, height, ext, date, rating, excluded, excludedBy[], gender?}], tasks[], requiredTags[], site, genderEnabled, genderStats{}, blockedCount}`. `excludedBy` ⊂ `required, neg, gender`; `gender = {label, conf, flags[], evidence[], conflict, source, effective}`; post bị chặn bị bỏ khỏi `items` |
| `files.nonEmpty` | `{path}` | `{exists, nonEmpty}` |
| `app.shutdown` | — | (notification) thoát |

Notification từ sidecar: `ready` (giống `app.info`), `log {text, scope?}`, `tags.updateProgress`, `tags.updateDone`. Mã lỗi: `-32601` không có method, `-32602` tham số sai, `-32001` module chưa cài, `-32002` thao tác thất bại (kèm `data.log`), `-32003` không lấy được trang, `-32000` lỗi khác.

## 5. Giao thức job tải (CLI IPC)
- Lệnh: `LUSTRA_IPC=1 python -m lustra -module <abbr> [connection args] [date args] [download args] <tags...>`
- stdout: log thường, cùng các dòng `@@LUSTRA_PROGRESS {"state","stateName","task","tasks","total","totalAll","processed","success","failed","pages","cfChallenge","genderFiltered","blocked"}`. Dòng cuối cùng có `stateName: "DONE"` kèm `failedItems[]` và `genderStats{}`.
- stdin: gửi `cancel\n` để huỷ có kiểm soát (đặt cờ `killed` cho thread gốc). Nếu sau 15 s tiến trình chưa thoát, main sẽ kill.
- Trạng thái (`DownloaderStates`): LAUNCHING → SEARCHING → SCANNING_PAGES1/2 → FILTERING_ITEMS1..4 → DOWNLOADING → DONE.

## 6. Manifest extension
Schema: [`schemas/extension-manifest.schema.json`](schemas/extension-manifest.schema.json). Ràng buộc chính:
- `id` trùng tên thư mục;
- `abbr` gồm 2–4 ký tự `[a-z0-9]` và là duy nhất;
- `apiVersion` = 1;
- `entry` trỏ tới class kế thừa `lustra.download.Downloader`;
- khối `syntax` chỉ bắt buộc với site mà lõi chưa biết;
- tuỳ chọn `genderRules` (file luật giới tính trong thư mục extension; module sẽ được thêm capability `gender_tags`) và `safetyTags` (chỉ thêm vào danh sách chặn).

## 7. Định dạng danh sách tag
`2tags/<abbr>_tags.json` (bản đi kèm repo, được cron cập nhật) hoặc `<data dir>/tags/<abbr>_tags.json` (do app cập nhật). Mỗi dòng có dạng `"tag": "N posts (type)"`, sắp xếp theo thứ tự so sánh chuỗi của Python. `type` thuộc `general | artist | copyright | character | metadata | species | lore | invalid1`. Khi đọc, sidecar chọn file **mới nhất** cho từng module; nếu có `LUSTRA_TAGS_DIR` thì thư mục đó luôn được ưu tiên.

| Module | Nguồn cập nhật | Ghi chú |
|---|---|---|
| rx | DAPI `s=tag&q=index&after_id=` | cần API key (dùng key mặc định nếu không cấu hình) |
| db | `tags.json?page=a<id>&search[hide_empty]=true` | ~1,1 triệu tag; cron mặc định chỉ giữ tag có ≥ 2 post (~0,77 triệu) |
| xb, bb | DAPI `after_id` | |
| en | `db_export/tags-YYYY-MM-DD.csv.gz` | nhẹ tải cho server hơn so với gọi API |
| rn, rs, rp | không có nguồn | giữ nguyên file đi kèm |

Sanity check: nếu số tag lấy được < 50% file hiện có, hoặc lần lấy chỉ là một phần (`--max-pages`), file sẽ không được ghi đè trừ khi dùng `--force`.

## 8. Settings (`<userData>/settings.json`)
```jsonc
{
  "appearance": { "theme": "ember|graphite|paper|oled", "icon": "full-mask-orange|…",
                  "background": { "mode": "icon|image|none", "image": "<path>", "blur": 4, "opacity": 0.9, "dim": 0.18 } },
  "search": { "module": "rx", "autocomplete": true, "safePreview": false },
  "download": { "path": "~/Downloads/Lustra", "threads": 4, "gender": { "select": [], "conf": "medium", "strict": false }, "images": true, "videos": true, "videoFormat": "", "lowRes": false,
                "prefix": true, "appendInfo": false, "reverse": false, "limit": 0, "parchi": false, "preserveDate": false,
                "subfolderPerQuery": false, "dumpTags": false, "dumpSources": false, "dumpComments": false, "dumpHashes": false,
                "infoMode": "run|item|merge", "warnNonEmpty": true, "maxConcurrentJobs": 1 },
  "connection": { "proxy": "", "ignoreProxy": false, "proxyNoDownload": false, "timeout": 0, "retries": 0, "headers": {}, "cookies": {} },
  "tags": { "autoUpdate": false, "autoUpdateDays": 7, "lastAutoUpdate": {} },
  "advanced": { "pythonPath": "", "verbose": false, "catalogUrl": "" },
  "secrets": { "<abbr>": { "enc": "<base64 safeStorage>" } }
}
```

## 9. Thư mục dữ liệu
`LUSTRA_DATA_DIR`, hoặc mặc định: `~/.config/Lustra` (Linux), `%APPDATA%\Lustra` (Windows), `~/Library/Application Support/Lustra` (macOS). Đây cũng là `userData` của Electron. Bên trong gồm: `settings.json`, `extensions/`, `tags/`, `backgrounds/`, `icons/`, `cache/thumbs/`.
