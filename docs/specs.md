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
| F-09 | Tuỳ chọn tải: ảnh/video, định dạng video, ảnh chất lượng thấp, khoảng ngày, số luồng, giới hạn số post, thứ tự, post cha/con, bỏ qua file đã tải (F-19), thư mục, tiền tố tên file, thêm tag vào tên, lưu info (tags, nguồn, bình luận, hash, 3 chế độ) |
| F-10 | Kết nối: proxy `http`, `https`, `socks5`, `socks5h` (host có thể là IP hoặc tên miền), tạm bỏ qua proxy, tải file không qua proxy, timeout, số lần retry, headers/cookies JSON, API key theo module (lưu mã hoá) |
| F-11 | Extensions: liệt kê; cài từ catalog cục bộ, từ file zip (bắt buộc SHA-256) hoặc từ catalog từ xa (URL + SHA-256); gỡ; không cho thay hay gỡ extension builtin |
| F-12 | Cập nhật danh sách tag: thủ công trong app, tự động theo chu kỳ khi mở app, và qua cronjob/systemd timer |
| F-13 | Giao diện: 4 theme (Ember, Graphite, Paper, OLED); 4 icon; nền là icon làm mờ (mặc định), ảnh tuỳ chọn không làm mờ, hoặc không có nền; chỉnh độ mờ, độ hiện, lớp phủ; làm mờ thumbnail (riêng tư) |
| F-13b | Đổi icon áp dụng cả ngoài app, không cần khởi động lại trừ dock trên Wayland:<br>• Icon cửa sổ/taskbar (Windows, X11) và dock macOS đổi ngay; dock macOS được đặt lại mỗi khi app mở.<br>• Linux: launcher `~/.local/share/applications/lustra.desktop` do Lustra tạo (có dòng `X-Lustra-Launcher=true`) được ghi lại, với file icon tên mới mỗi lần (`<data>/icons/lustra-icon-<id>.png`); launcher do công cụ khác tạo thì không sửa. Dock trên Wayland chỉ đọc launcher khi app mở, nên cần khởi động lại.<br>• Windows (bản cài đặt): shortcut ở Start menu, Desktop và taskbar đã ghim trỏ tới exe của Lustra được đổi sang `<data>/icons/lustra-icon-<id>.ico`.<br>• Icon của chính file app (AppImage, `.exe`, gói `.app`) cố định lúc build.<br>`settings:set` trả về `iconSync = {launcher: updated\|unchanged\|missing\|foreign\|unsupported, shortcuts, restartForDock}` |
| F-14 | Giới hạn 2 tag của Danbooru: metatag miễn phí (`rating:`, `id:`, `date:`, `score:`, `status:`, `parent:`) không bị tính. Tag dương vượt giới hạn được kiểm tra cục bộ (tag hiếm nhất theo danh sách tag được gửi lên server); tag phủ định vượt giới hạn chuyển thành nhóm loại trừ cục bộ |
| F-15 | Nhận diện trang bot challenge (`cf-mitigated: challenge`): dừng ngay, không retry, báo hướng dẫn |
| F-16 | Lọc theo giới tính nhân vật, tầng 0 (chỉ dựa vào tag): `-gender only_female,only_male,both,none,unknown`, `-gender_conf low\|medium\|high` (mặc định medium), `-gender_strict` (futa/trap/mơ hồ không tính là only_*). `-gender_mode model` chưa có (P3). Bộ luật theo site nằm ở `lustra/gender_rules/*.json`; extension có thể tự mang bộ luật riêng |
| F-18 | Tạo extension cho site mới, ra **bản nháp, không tự cài** (Extensions → "Tạo extension cho site mới"):<br>• Nhập link https và loại media. Có 2 chế độ: **Dùng AI** (Claude, cần khoá API) và **Không dùng AI** (chỉ site Gelbooru 0.2 DAPI / Danbooru, dùng template có sẵn, miễn phí).<br>• Một phiên: khảo sát robots.txt, nhận diện engine → tạo `manifest.json` / `downloader.py` / `specs.md` → kiểm tra tĩnh → chạy thử trong sandbox (ghi fixture, chỉ lấy link file, không tải file) → kiểm tra độc lập offline. Kết quả nằm ở `<data>/extgen/drafts/<phiên>/` kèm `report.json`.<br>• Trên bản nháp, người dùng có thể: xem chi tiết, mở thư mục, xuất .zip (kèm sha256), hoặc bấm **Cài vào app…**. Nút cài chỉ có với bản nháp đạt; khi bấm, main kiểm tra lại offline, đóng gói rồi hỏi bằng hộp thoại của hệ điều hành.<br>• Giới hạn mỗi phiên: chi phí tối đa (mặc định 2 USD, tối đa 20), 30 lượt model, 5 lần chạy thử, 60 request tới site, 1 request/giây.<br>• Id / abbr không trùng extension đã biết. Nếu site đã có extension, bản tóm tắt nói rõ.<br>• Rule R1–R13: [plan](plans/site-extension-skill-and-ai-generator.md) Phần C |
| F-19 | Index cục bộ các lần tải (`lustra/dlindex.py`, `<data>/index/downloads.sqlite`):<br>• Mỗi file tải xong (chế độ `full`, kể cả file đã có sẵn đúng tên) được ghi một dòng: module, post id, md5, đường dẫn, tags, score, rating, ngày post, thời điểm tải, đuôi file, dung lượng. Chỉ dùng metadata đã có từ lượt quét: không đọc hay băm file, không gửi request thêm. Tắt ghi bằng `LUSTRA_DLINDEX=0`.<br>• `-skip_known` (mặc định tắt): bỏ qua post đã có trong index theo cặp module + id (trước khi lấy info, nên module phải tải trang post để lấy info cũng tiết kiệm request) hoặc theo md5 (sau khi lấy info, khớp với mọi module), **chỉ khi file vẫn còn trên đĩa** (file thường, > 0 byte). File đã bị xoá thì tải lại.<br>• Tìm offline qua `index.search` (tag, `-tag`, `tiền_tố*`, `rating:`, `md5:`, `id:`, module, khoảng ngày post, phân trang) và `index.stats`. App: ô "Bỏ qua file đã tải" trong tuỳ chọn tải, số liệu index ở Cài đặt; trang tìm offline đầy đủ để sau |
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
| N-07 | Index các lần tải không làm chậm việc tải: luồng tải chỉ đẩy bản ghi vào hàng đợi, một thread nền ghi theo lô (tối đa 200 bản ghi hoặc 1,5 s, một transaction mỗi lô). SQLite chế độ WAL, `busy_timeout` 10 s: nhiều job CLI ghi cùng lúc được tuần tự hoá, sidecar đọc song song. Lô bị khoá quá lâu được thử lại 3 lần rồi bỏ, kèm một cảnh báo; lỗi index không bao giờ làm hỏng lượt tải |

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
| `tags.update` | `{module, apiKey?, outDir?, delay?}` | `{job, outDir}`; sau đó gửi notification `tags.updateProgress`, `tags.updateDone {ok, written, count, previous, message}`. Mỗi module chỉ một lượt cùng lúc (`-32004`) |
| `search.count` | `{module, tags, args[]}` | `{count, taskCounts[], approximate, requiredTags[], cfChallenge}`. `count` là tổng các task (nhóm OR bị tách); `approximate` = true khi có nhiều task (post có thể trùng) hoặc còn tag lọc cục bộ. Lỗi website trả `-32002` kèm thông điệp |
| `search.preview` | `{module, tags, page, pageSize?, args[]}`: `args` có thể chứa `-gender …`; `pageSize` mặc định min(cỡ trang module, 200). Truy vấn nhiều task: trang N của mỗi task được gộp, bỏ trùng, sắp theo id giảm dần; kết quả có thêm `hasMore` | `{page, pageSize, items:[{id, preview, sample, isVideo, tags[], score, width, height, ext, date, rating, excluded, excludedBy[], gender?}], tasks[], requiredTags[], site, genderEnabled, genderStats{}, blockedCount}`. `excludedBy` ⊂ `required, neg, gender`; `gender = {label, conf, flags[], evidence[], conflict, source, effective}`; post bị chặn bị bỏ khỏi `items` |
| `tags.resolve` | `{module, tags[]}` (tối đa 64) | `{items:[{tag, canonical, alias, posts, type, known}], hasList}`. Metatag: `type:'meta'`, `known:true`. Tag chứa `*`/`?`: `type:'wildcard'`, `known` = có tag khớp. `alias` chỉ true khi tag không có trong danh sách mà tag đích thì có |
| `tags.fuzzy` | `{module, text, limit=8 (≤ 50)}` | `[{tag, posts, type, distance}]`. Sai tối đa 1 ký tự (3–5 ký tự) hoặc 2 ký tự (từ 6 ký tự); thiếu/thừa `_` không tính; dưới 3 ký tự trả `[]` |
| `search.related` | `{module, tags, args[], limit=30 (≤ 200)}` | `{items:[{tag, count, posts, type}], sample}`: tag xuất hiện cùng trong trang 0 (200 post, đã qua bộ lọc cục bộ), trừ tag của truy vấn và metatag |
| `files.nonEmpty` | `{path}` | `{exists, nonEmpty}` |
| `index.search` | `{tags, module?, dateFrom?, dateTo?, page=0, pageSize=50 (≤ 200), order?}`. `tags`: chuỗi hoặc mảng; mọi điều kiện phải khớp: `tag`, `-tag`, `tiền_tố*`, `rating:g\|s\|q\|e`, `md5:<hash>`, `id:<n>` (tối đa 24 điều kiện). Ngày theo ngày post, `YYYY-MM-DD` hoặc `DD-MM-YYYY`, gồm cả hai đầu. `order`: `downloaded` (mặc định, mới nhất trước), `downloaded_asc`, `date`, `date_asc`, `score`, `id` | `{total, page, pageSize, terms[], items:[{module, id, md5, path, name, ext, size, tags, score, rating, date, downloadedAt, exists}]}`. Chỉ đọc index, không gửi request tới website. `exists` = file còn trên đĩa |
| `index.stats` | — | `{path, exists, schema, files, posts, withMd5, tags, bytes, dbBytes, first, last, recording, modules:[{module, files, bytes, last}]}`. `exists` = false cho tới khi file đầu tiên được ghi; `recording` = false khi `LUSTRA_DLINDEX=0` |
| `files.identify` | `{names[] (≤ 500), module}`: tên file, đường dẫn, link post hoặc link ảnh | `{items:[{name, module, kind:'id'\|'md5'\|'', value, note}], queries:[{module, tags, count, dropped}], max}`. Nhận ra: tên Lustra đặt (`db_12345…`, `12345…` = module đang chọn), md5 32 ký tự (`<md5>.jpg`, `sample_<md5>`, `__tags__<md5>` của Danbooru), link post/ảnh của các site đã biết (so theo host và tên miền gốc trong `site` của manifest). Mỗi module một truy vấn: Danbooru dùng danh sách `id:1,2,3` / `md5:a,b` (100 mục mỗi danh sách); site khác dùng nhóm OR `(id:1~id:2)`, tối đa 64 mục |
| `app.shutdown` | — | (notification) thoát |

Notification từ sidecar: `ready` (giống `app.info`), `log {text, scope?}`, `tags.updateProgress`, `tags.updateDone`. Mã lỗi: `-32601` không có method, `-32602` tham số sai, `-32001` module chưa cài, `-32002` thao tác thất bại (kèm `data.log`), `-32003` không lấy được trang, `-32004` đang cập nhật danh sách tag của module đó, `-32000` lỗi khác. Đường dẫn trong `files.nonEmpty` hiểu `~` và đường dẫn tương đối theo thư mục home (giống job tải).

## 5. Giao thức job tải (CLI IPC)
- Lệnh: `LUSTRA_IPC=1 LUSTRA_CATALOG_AUTOLOAD=0 python -m lustra -module <abbr> [connection args] [date args] [download args] -- <tags...>`. Tag luôn đứng sau `--`, nên tag loại trừ trùng tên tuỳ chọn (`-webm`, `-v`) vẫn là tag.
- stdout: log thường, cùng các dòng `@@LUSTRA_PROGRESS {"state","stateName","task","tasks","total","totalAll","processed","success","failed","pages","cfChallenge","genderFiltered","blocked","known","streaming","scanning"}` (`known`: số post bỏ qua nhờ `-skip_known`). `streaming` = true khi file được tải trong lúc vẫn quét trang (truy vấn 1 task), khi đó `totalAll` còn tăng cho tới khi `scanning` = false. Dòng cuối cùng có `stateName: "DONE"` kèm `failedItems[]`, `genderStats{}`, `ok`, `exitCode`, `error`, `cancelled`.
- Exit code: `0` thành công (kể cả không tìm thấy gì), `1` lỗi (lỗi nặng, bot challenge mà không tải được file nào, hoặc mọi file đều lỗi), `2` sai tham số (argparse), `3` xong nhưng một số file lỗi, `130` bị huỷ. App coi `0` và `3` là "Xong".
- stdin: gửi `cancel\n` để huỷ có kiểm soát (đặt cờ `killed` cho thread gốc). Nếu sau 15 s tiến trình chưa thoát, main sẽ kill.
- Trạng thái (`DownloaderStates`): LAUNCHING → SEARCHING → SCANNING_PAGES1/2 → FILTERING_ITEMS1..4 → DOWNLOADING → DONE. Ở chế độ streaming, các trạng thái quét/lọc và DOWNLOADING xen kẽ theo từng lô trang.
- Pipeline:
  - Truy vấn 1 task (không parent/favorites/pool) dùng **streaming**: quét trang theo thứ tự tải, lọc từng lô, tải ngay, `-dlimit` dừng quét sớm. Kết quả giống pipeline cũ; tắt bằng `LUSTRA_STREAMING=0`.
  - Danbooru với sort mặc định phân trang bằng cursor id (`page=a<id>`/`b<id>`), không giới hạn số trang.
- Tuỳ chọn `-skip_known` (F-19): bỏ qua post đã có trong index các lần tải mà file vẫn còn trên đĩa. Không bật thì hành vi như cũ. Mỗi file tải xong luôn được ghi vào index, trừ khi `LUSTRA_DLINDEX=0`.
- Tuỳ chọn `-tags_limit N`: giới hạn số tag mỗi lượt tìm của tài khoản (Danbooru: 2 thường, 6 Gold, 12 Platinum), thay giới hạn mặc định của module. Nhóm OR bị tách thành nhiều lượt tìm tối đa 64 lượt.
- Header/cookie: app truyền từng cặp `-header name=value` / `-cookie name=value`. `-headers`/`-cookies` nhận object JSON gồm các chuỗi (giữ đúng `"`, `\`, `\uXXXX`). Header người dùng thắng header mặc định của module.
- Giới hạn tốc độ API của module (`_get_min_request_interval`) dùng chung giữa mọi tiến trình qua file khoá `<data>/ratelimit/`.
- Tải file:
  - Một GET dạng stream mỗi lần thử, không HEAD; ghi vào `<tên>.part`, xong mới đổi tên.
  - Resume bằng Range/If-Range; 400/401/403/404/405/410/451 không retry; 429/503 theo `Retry-After`, mọi thread cùng chờ.
  - Sau 3 lần bot challenge liên tiếp thì không tải tiếp các file còn lại.

## 5b. Worker tạo extension (`python -m lustra.aigen.worker`, lõi đóng băng: `lustra-core aigen`)
- Mỗi tiến trình chạy một phiên. Main sinh tiến trình, không truyền tham số dòng lệnh.
- stdin, mỗi dòng một JSON:
  - dòng đầu: `{"cmd":"start","url","media":[…],"mode":"ai"|"template","model":"claude-opus-5-5"|"claude-sonnet-5-5","maxUsd","maxTurns","query","apiKey"}`;
  - về sau: `{"cmd":"cancel"}`. Đóng stdin cũng tính là huỷ.
  - Khoá API **chỉ** đi qua stdin, không nằm trong argv, biến môi trường, file hay event. Worker xoá `ANTHROPIC_API_KEY` khỏi môi trường của chính nó.
- stdout chỉ gồm các dòng `@@GEN {"type": …}`; mọi output khác đi ra stderr. Các loại:
  - `start {session,url,mode,model}`;
  - `phase {tool,label,state: writing|running,detail}`;
  - `log {text,source?: model,tool?,error?}`;
  - `usage {usd,maxUsd,turns,input,output,cacheRead,cacheWrite,requests}`;
  - `done {session,status,summary,ruleIds,draftDir,extId,usd,report}`.
- `status` nhận một trong các giá trị `ready`, `failed`, `refused`, `cancelled`, `error`. Exit code: `0` khi ready, `1` khi failed / refused / cancelled, `2` khi error.
- Lời gọi Claude:
  - `client.beta.messages.stream`, model `claude-opus-5-5` (mặc định), `max_tokens` 64000, `output_config.effort = "high"`; adaptive thinking là mặc định nên không gửi tham số `thinking`.
  - Prompt caching: `cache_control` ở system và ở cấp request (tự động).
  - Fallback khi model từ chối: `fallbacks: "default"` với beta `server-side-fallback-2026-07-01`.
  - Tool: 9 tool phía client, `strict` cho mọi tool trừ `write_file` (`eager_input_streaming`, input được kiểm tra thủ công). `tool_choice` để mặc định (auto).
  - Phản hồi `refusal` → `refused`; `max_tokens` giữa chừng một tool → tool đó không chạy; JSON tool không parse được → gửi lại lượt đó (tối đa 2 lần).
- Hai lệnh dùng chung thư viện `lustra.extgen`: `python -m lustra.extgen survey | fetch | new | record | check | pack` (skill Claude Code dùng). Kết quả là JSON. Exit code: `0` ok, `1` có kiểm tra không đạt, `2` bị từ chối hoặc lỗi.

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
                "prefix": true, "appendInfo": false, "reverse": false, "limit": 0, "parchi": false, "preserveDate": false, "skipKnown": false,
                "subfolderPerQuery": false, "dumpTags": false, "dumpSources": false, "dumpComments": false, "dumpHashes": false,
                "infoMode": "run|item|merge", "warnNonEmpty": true, "maxConcurrentJobs": 1 },
  "connection": { "proxy": "", "ignoreProxy": false, "proxyNoDownload": false, "timeout": 0, "retries": 0, "headers": {}, "cookies": {} },
  "tags": { "autoUpdate": false, "autoUpdateDays": 7, "lastAutoUpdate": {} },
  "advanced": { "pythonPath": "", "verbose": false, "catalogUrl": "" },
  "ai": { "model": "claude-opus-5-5|claude-sonnet-5-5", "maxUsd": 2 },
  "secrets": { "<abbr>": { "enc": "<base64 safeStorage>" }, "@anthropic": { "enc": "<base64 safeStorage>" } }  // khoá Claude: chỉ dạng mã hoá
}
```

## 9. Thư mục dữ liệu
`LUSTRA_DATA_DIR`, hoặc mặc định: `~/.config/Lustra` (Linux), `%APPDATA%\Lustra` (Windows), `~/Library/Application Support/Lustra` (macOS). Đây cũng là `userData` của Electron. Bên trong gồm: `settings.json`, `extensions/`, `tags/`, `backgrounds/`, `icons/`, `cache/thumbs/`, `ratelimit/`, `index/downloads.sqlite` (index các lần tải, F-19; kèm `-wal`/`-shm` khi đang mở), `extgen/drafts/<phiên>/` (bản nháp extension: `report.json` và `<id>/` gồm manifest, downloader, specs, tests/fixtures).
