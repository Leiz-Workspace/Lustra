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
| F-07 | Preview: lưới thumbnail theo trang, đánh dấu post bị lọc cục bộ, chọn/bỏ chọn/đảo, tải các post đã chọn, lightbox xem tag và thêm/loại trừ tag. Chip lọc Ảnh / Video / Âm thanh (kèm số lượng, chỉ hiện khi trang có từ 2 loại). Huy hiệu "✓ đã tải" cho post đã có trong index tải với file còn trên đĩa (cùng post của module, hoặc cùng md5 từ module khác; tra index, không thêm request). Lightbox phát video / âm thanh ngay trong app: main stream file gốc qua `lustra-thumb://m/` (chỉ `video/*`, `audio/*`, địa chỉ công khai, không lưu cache, chuyển tiếp `Range` để tua) |
| F-08 | Tải nền theo hàng đợi, xem tiến độ (trạng thái, số file, thành công/lỗi, dung lượng đã nhận, tốc độ trong ~6 s gần nhất, thời gian còn lại ước theo nhịp mỗi file khi đã quét xong), huỷ, mở thư mục, xem log. Job giống hệt một job đang chờ / chạy bị từ chối trừ khi người dùng xác nhận (`allowDuplicate`). **Chạy lại** job đã xong / lỗi / huỷ với đúng truy vấn và tuỳ chọn cũ (`jobs:rerun`). Thông báo của hệ điều hành khi job xong hoặc lỗi lúc Lustra không ở cửa sổ trước (`download.notify`, mặc định bật); bấm thông báo mở trang Tải xuống |
| F-09 | Tuỳ chọn tải: ảnh/video/âm thanh (ô "Âm thanh" chỉ hiện với module khai báo media `audio`; CLI `-skip_aud`, thư mục con `-audsub`; file âm thanh nhận theo đuôi `mp3 ogg oga opus flac m4a wav` và luôn tải file gốc), định dạng video, ảnh chất lượng thấp, khoảng ngày, số luồng, giới hạn số post, thứ tự, post cha/con, bỏ qua file đã tải (F-19), thư mục, tiền tố tên file, thêm tag vào tên, lưu info (tags, nguồn, bình luận, hash, 3 chế độ) |
| F-10 | Kết nối: proxy `http`, `https`, `socks5`, `socks5h` (host có thể là IP hoặc tên miền), tạm bỏ qua proxy, tải file không qua proxy, timeout, số lần retry, headers/cookies JSON, API key theo module (lưu mã hoá) |
| F-11 | Extensions: liệt kê; cài từ catalog cục bộ, từ file zip (bắt buộc SHA-256) hoặc từ catalog từ xa (URL + SHA-256); gỡ; không cho thay hay gỡ extension builtin. Báo bản mới: catalog cục bộ có bản cao hơn bản đã cài (`ext.list` trả `catalogVersion`, `updateAvailable`; so số phiên bản theo từng phần) → huy hiệu trên thẻ, số trên mục Extensions, một thông báo, nút "Cập nhật lên vX"; catalog từ xa so với bản đã cài cùng id → "Cập nhật vA → vB" / "Đã cài". Thẻ extension do trình tạo sinh ra có huy hiệu nguồn gốc (AI / template / extgen).<br>• **Catalog từ xa phải có chữ ký Ed25519** của người bảo trì (`<index>.sig`, kiểm bằng khoá công khai đi kèm Lustra trước khi đọc bất kỳ mục nào). Chưa ký, ký sai, khoá lạ hoặc hết hạn (`expires`) → từ chối cả catalog, kèm lý do bằng tiếng Việt. Thẻ catalog từ xa có huy hiệu "đã ký". Khi cài, main lấy URL và sha256 từ catalog lõi đã kiểm (renderer chỉ gửi id), lõi kiểm lại chữ ký rồi tải đúng archive có sha256 người dùng đã xác nhận.<br>• **Danh sách chặn tên miền** (R6 / R7 / R8): extension cho site trong danh sách (kể cả tên miền con) hiện huy hiệu "bị chặn", không có nút cài; mọi đường cài (catalog cục bộ, zip, catalog từ xa, bản nháp) và lúc nạp đều từ chối |
| F-12 | Cập nhật danh sách tag: thủ công trong app, tự động theo chu kỳ khi mở app, và qua cronjob/systemd timer |
| F-13 | Giao diện: 4 theme (Ember, Graphite, Paper, OLED); 4 icon; nền là icon làm mờ (mặc định), ảnh tuỳ chọn không làm mờ, hoặc không có nền; chỉnh độ mờ, độ hiện, lớp phủ; làm mờ thumbnail (riêng tư) |
| F-13b | Đổi icon áp dụng cả ngoài app, không cần khởi động lại trừ dock trên Wayland:<br>• Icon cửa sổ/taskbar (Windows, X11) và dock macOS đổi ngay; dock macOS được đặt lại mỗi khi app mở.<br>• Linux: launcher `~/.local/share/applications/lustra.desktop` do Lustra tạo (có dòng `X-Lustra-Launcher=true`) được ghi lại, với file icon tên mới mỗi lần (`<data>/icons/lustra-icon-<id>.png`); launcher do công cụ khác tạo thì không sửa. Dock trên Wayland chỉ đọc launcher khi app mở, nên cần khởi động lại.<br>• Windows (bản cài đặt): shortcut ở Start menu, Desktop và taskbar đã ghim trỏ tới exe của Lustra được đổi sang `<data>/icons/lustra-icon-<id>.ico`.<br>• Icon của chính file app (AppImage, `.exe`, gói `.app`) cố định lúc build.<br>`settings:set` trả về `iconSync = {launcher: updated\|unchanged\|missing\|foreign\|unsupported, shortcuts, restartForDock}` |
| F-14 | Giới hạn 2 tag của Danbooru: metatag miễn phí (`rating:`, `id:`, `date:`, `score:`, `status:`, `parent:`) không bị tính. Tag dương vượt giới hạn được kiểm tra cục bộ (tag hiếm nhất theo danh sách tag được gửi lên server); tag phủ định vượt giới hạn chuyển thành nhóm loại trừ cục bộ |
| F-15 | Nhận diện trang bot challenge (`cf-mitigated: challenge`): dừng ngay, không retry, báo hướng dẫn |
| F-16 | Lọc theo giới tính nhân vật, tầng 0 (chỉ dựa vào tag): `-gender only_female,only_male,both,none,unknown` (giữ các nhãn này), `-gender_exclude …` (bỏ các nhãn này, giữ mọi nhãn còn lại; không được bỏ hết; đi cùng `-gender` thì là nhãn của `-gender` trừ đi). Ví dụ `robot -gender_exclude only_male`: robot chỉ nữ, cả hai, không có nhân vật hoặc không rõ. App: nút gạt **Giữ / Loại bỏ** trước các nút nhãn (`download.gender.op`), đổi chế độ thì các nút đổi sang phần bù để kết quả lọc không đổi. `-gender_conf low\|medium\|high` (mặc định medium), `-gender_strict` (futa/trap/mơ hồ không tính là only_*). `-gender_mode model` (tầng 1, xem F-21). Bộ luật theo site nằm ở `lustra/gender_rules/*.json`; extension có thể tự mang bộ luật riêng |
| F-18 | Tạo extension cho site mới, ra **bản nháp, không tự cài** (Extensions → "Tạo extension cho site mới"):<br>• Nhập link https và loại media. Có 2 chế độ: **Dùng AI** (Claude, cần khoá API) và **Không dùng AI** (miễn phí, dùng template có sẵn; chỉ cho site chạy Gelbooru 0.2 DAPI, Danbooru, e621, Moebooru, Philomena, Szurubooru hoặc Shimmie có API danbooru). Năm engine JSON / XML dùng lớp nền chung `DownloaderJsonApi` (`lustra/download_json.py`); e621, Moebooru và Philomena quét theo con trỏ id khi số bài vượt độ sâu trang.<br>• Một phiên: khảo sát robots.txt, nhận diện engine → tạo `manifest.json` / `downloader.py` / `specs.md` → kiểm tra tĩnh → chạy thử trong sandbox (ghi fixture, chỉ lấy link file, không tải file) → kiểm tra độc lập offline. Kết quả nằm ở `<data>/extgen/drafts/<phiên>/` kèm `report.json`.<br>• Trên bản nháp, người dùng có thể: xem chi tiết, mở thư mục, xuất .zip (kèm sha256), hoặc bấm **Cài vào app…**. Nút cài chỉ có với bản nháp đạt; khi bấm, main kiểm tra lại offline, đóng gói rồi hỏi bằng hộp thoại của hệ điều hành.<br>• Giới hạn mỗi phiên: chi phí tối đa (mặc định 2 USD, tối đa 20), 30 lượt model, 5 lần chạy thử, 60 request tới site, 1 request/giây.<br>• Id / abbr không trùng extension đã biết. Nếu site đã có extension, bản tóm tắt nói rõ.<br>• Ngoài các giới hạn trên, còn dừng ở: site trong danh sách chặn tên miền (R6 / R7 / R8, trước mọi request và mọi lời gọi model; so cả tên miền con, chữ hoa, dấu chấm cuối, `%xx`, punycode, ký tự full-width), robots.txt cấm toàn bộ hoặc trả 5xx (R2), Cloudflare challenge (R5), manifest khai báo cookie đăng nhập / challenge (R1), đổi `site` / `id` / `abbr` / `$generator` của manifest. Code chạy thử không đọc được file ngoài thư viện Python, mã Lustra, bản nháp và thư mục tạm.<br>• **Sửa bằng AI** cho bản nháp chưa đạt (còn thư mục bản nháp, không bị từ chối): phiên mới chép bản nháp cũ sang (bản cũ giữ nguyên), gửi cho Claude bản tóm tắt và các kiểm tra lỗi lần trước; `report.json` ghi `repairOf`.<br>• Rule R1–R13: [plan](plans/site-extension-skill-and-ai-generator.md) Phần C |
| F-20 | Truy vấn đã lưu tự tải lại định kỳ (⟳ trong ngăn "Đã lưu"): chu kỳ 6 giờ / 12 giờ / mỗi ngày / 3 ngày / mỗi tuần. Lần đầu chọn "chỉ post mới từ bây giờ" (mốc = id lớn nhất hiện tại của website, `-get_maxid`) hoặc "tải cả kết quả hiện có". Main kiểm tra mỗi 5 phút khi app mở (và sau khi mở app 1 phút): truy vấn đến hạn chạy thành job "Tự cập nhật: <tên>" với thư mục / tuỳ chọn tải mặc định, `-skip_known`, và `id:>maxId` cho module dùng metatag `:`; không chạy khi job của nó còn đang chờ / chạy. Khi job xong: `maxId` = max(cũ, `maxId` của lượt chạy), số file mới, trạng thái / lỗi lưu trong `search.saved[].auto` |
| F-21 | Lọc giới tính tầng 1, model nhận diện bằng ảnh (plan gender-filter-model phương án A0): `-gender_mode model`, app: ô "Nhận diện" → "Tag + model (ảnh)" (`download.gender.mode`).<br>• **Model chạy ở model server riêng** (`lustra/model_server.py`, HTTP API), app và CLI chỉ gọi API: bản app không kèm numpy / onnxruntime / Pillow / model. Địa chỉ: `-gender_model_url` (hoặc `LUSTRA_GENDER_MODEL_URL`), token: `-gender_model_token` (hoặc `LUSTRA_GENDER_MODEL_TOKEN`). App: Cài đặt → Model nhận diện giới tính (`genderModel.url`, mặc định `http://127.0.0.1:8765`; token là secret, mã hoá bằng keyring, không tới renderer, đưa cho lõi qua `LUSTRA_SECRET_ARGS` / tham số RPC). Không có địa chỉ: CLI dùng model trong tiến trình nếu có `lustra[model]`.<br>• **Bật / tắt trong app** (`genderModel.enabled`, mặc định tắt): tắt thì app không truyền `-gender_mode model` (truy vấn đã lưu ở chế độ model cũng lọc theo tag) và không đưa token cho lõi. Trước khi bật, app hỏi server phần cứng (`check` của `gender.modelStatus`): không có GPU có VRAM riêng ≥ 1,5 GB (`no_gpu`), có GPU nhưng onnxruntime bản CPU (`gpu_no_runtime`), server bị đặt `--device cpu` (`cpu_forced`), VRAM trống không đủ (`vram_low`) hoặc đã nạp mà rơi về CPU (`gpu_fallback`) → hộp cảnh báo khuyên để tắt, nút **Hướng dẫn** (hướng dẫn chung `app/renderer/guide.js`), **Vẫn bật (chạy bằng CPU)**, **Để tắt**; server không kết nối được thì không bật được. Bật → `gender.modelLoad` (nạp model ngay, báo thiết bị thật); tắt → `gender.modelUnload` khi server ở máy này (giải phóng RAM / VRAM).<br>• Địa chỉ: http / https, không user / query / fragment; **http chỉ cho máy này hoặc mạng nội bộ** (loopback, IP private, `.local`, `.lan`, `.home.arpa`, `.internal`), máy chủ khác phải https vì ảnh và token đi qua mạng.<br>• Chỉ post mà tag chưa đủ để kết luận (nhãn `unknown` hoặc độ tin cậy dưới ngưỡng `-gender_conf`) mới được model xem; post có cờ `real` (ảnh người thật) không bao giờ đưa vào model.<br>• Model: WD tagger v3 ViT (SmilingWolf, Apache-2.0), ONNX: chạy trên GPU khi onnxruntime của server có provider GPU (onnxruntime-gpu: CUDA; ROCm, MIGraphX, DirectML), nếu không thì CPU (`--device auto\|cpu\|gpu`, `LUSTRA_GENDER_MODEL_DEVICE`). Đo trên RTX 4070 Ti SUPER: ~18 ms / ảnh qua HTTP, ~1,3 GB VRAM; CPU: ~335 ms / ảnh; kết quả hai bên lệch < 0,001. Server chỉ trả tag đoán ra (≥ 0,05); bộ luật `db`, safety gate và cache kết quả nằm ở phía client, server không biết truy vấn, site hay post. Ảnh: `sample_url` nếu là ảnh, nếu không thì thumbnail (video: ảnh poster, độ tin cậy tối đa medium). Client tải ảnh bằng session của downloader (cùng giãn cách request) rồi gửi bytes tới server; server giải mã trong RAM, không lưu, không ghi log ảnh hay header.<br>• Kết hợp với tag: tag đủ tin cậy thì thắng; tag yếu cùng nhãn với model → nhãn đó, độ tin cậy cao hơn (`source: tags+model`); khác nhãn → `unknown`; tag không có gì → nhãn của model (`source: model`); cờ trap / futa / ảnh thật không bao giờ thành only_*. Model đoán ra `photo_(medium)` ≥ 0,5 → `unknown`.<br>• Safety gate: model đoán ra tag nằm trong danh sách chặn (`safety.blocked_tag`) với xác suất ≥ 0,15 thì post bị chặn như tag blacklist (tính vào `blocked`).<br>• Kết quả lưu theo post và id model trong `<data>/cache/gender_model.sqlite` (nhãn, độ tin cậy, bằng chứng; không lưu ảnh).<br>• Server không dùng được (không phản hồi, sai token, địa chỉ bị từ chối): job dừng với lỗi `-gender_mode model: …` thay vì để mọi post thành unknown; preview ghi lỗi trong `genderModel.error`. Ảnh server từ chối (400 / 413) chỉ làm post đó không có kết quả.<br>• Preview: tối đa 40 post mỗi trang được model xem, phần còn lại giữ nhãn từ tag (`gender.modelSkipped`) |
| F-19 | Index cục bộ các lần tải (`lustra/dlindex.py`, `<data>/index/downloads.sqlite`):<br>• Trang **Đã tải**: tìm offline theo tag (`tag -tag tiền_tố* rating: md5: id:`), module, khoảng ngày post, 6 kiểu sắp xếp, phân trang; lưới hiện ảnh trực tiếp từ đĩa, video / âm thanh có ô thay thế; file đã mất được làm mờ. Xem chi tiết: phát video / âm thanh từ đĩa, bấm tag để tìm tiếp, "Mở file" (chỉ đuôi media: jpg, jpeg, png, gif, webp, mp4, webm, mp3, ogg, oga, opus, flac, m4a, wav) và "Mở thư mục".<br>• Mỗi file tải xong (chế độ `full`, kể cả file đã có sẵn đúng tên) được ghi một dòng: module, post id, md5, đường dẫn, tags, score, rating, ngày post, thời điểm tải, đuôi file, dung lượng. Chỉ dùng metadata đã có từ lượt quét: không đọc hay băm file, không gửi request thêm. Tắt ghi bằng `LUSTRA_DLINDEX=0`.<br>• `-skip_known` (mặc định tắt): bỏ qua post đã có trong index theo cặp module + id (trước khi lấy info, nên module phải tải trang post để lấy info cũng tiết kiệm request) hoặc theo md5 (sau khi lấy info, khớp với mọi module), **chỉ khi file vẫn còn trên đĩa** (file thường, > 0 byte). File đã bị xoá thì tải lại.<br>• Tìm offline qua `index.search` (tag, `-tag`, `tiền_tố*`, `rating:`, `md5:`, `id:`, module, khoảng ngày post, phân trang) và `index.stats`. App: ô "Bỏ qua file đã tải" trong tuỳ chọn tải, số liệu index ở Cài đặt; trang tìm offline đầy đủ để sau |
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
- catalog từ xa chỉ được tin khi có chữ ký Ed25519 hợp lệ với khoá đi kèm Lustra (`lustra/safety_data/catalog_keys.json`); ngoại lệ chỉ cho nhà phát triển qua biến môi trường (`LUSTRA_CATALOG_ALLOW_UNSIGNED=1`, `LUSTRA_CATALOG_KEYS_FILE`);
- không cài, không nạp extension có `site` / `homepage` hoặc URL trong code thuộc danh sách chặn tên miền (`lustra/safety_data/denied_domains.txt`);
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
| `ext.install` | `{id}` (catalog cục bộ) \| `{catalogUrl, remoteId, sha256?}` (catalog từ xa: lõi tải và kiểm lại chữ ký, lấy URL + sha256 của mục; `sha256` là bản người dùng đã xác nhận, khác thì từ chối) \| `{url, sha256}` (https, chỉ CLI/dev) \| `{archivePath, sha256}` \| `{path}` (chỉ CLI/dev; app desktop không chuyển tiếp) | manifest + `restartRequired` |
| `ext.uninstall` | `{id}` | `{ok}` |
| `ext.remoteCatalog` | `{url?}` | `[{id, name, abbr, version, url, sha256, site?, description, signed, keyId, denied?: {rule, message}}]`. Tải `<url>` và `<url>.sig`, kiểm chữ ký trước khi đọc mục; catalog không tin được → lỗi (lý do tiếng Việt) |
| `tags.autocomplete` | `{module, prefix, limit}` (prefix có thể bắt đầu bằng `-`) | `[{tag, posts, type, insert}]` |
| `tags.browse` | `{module, prefix ('a'..'z' \| '#'), offset, limit, minPosts, types[], sort: 'posts'\|'az'}` | `{total, items:[{tag, posts, type}]}` |
| `tags.validate` | `{module, tags}` | `{valid, tags[]}` |
| `tags.status` | — | `[{module, path, size, mtime, loaded}]` |
| `tags.update` | `{module, apiKey?, outDir?, delay?}` | `{job, outDir}`; sau đó gửi notification `tags.updateProgress`, `tags.updateDone {ok, written, count, previous, message}`. Mỗi module chỉ một lượt cùng lúc (`-32004`) |
| `search.count` | `{module, tags, args[]}` | `{count, taskCounts[], approximate, requiredTags[], cfChallenge}`. `count` là tổng các task (nhóm OR bị tách); `approximate` = true khi có nhiều task (post có thể trùng) hoặc còn tag lọc cục bộ. Lỗi website trả `-32002` kèm thông điệp |
| `search.preview` | `{module, tags, page, pageSize?, args[]}`: `args` có thể chứa `-gender …`; `pageSize` mặc định min(cỡ trang module, 200). Truy vấn nhiều task: trang N của mỗi task được gộp, bỏ trùng, sắp theo id giảm dần; kết quả có thêm `hasMore` | `{page, pageSize, items:[{id, preview, sample, isVideo, mediaKind (image\|video\|audio), tags[], score, width, height, ext, date, rating, excluded, excludedBy[], gender?}], tasks[], requiredTags[], site, genderEnabled, genderStats{}, blockedCount}`. `excludedBy` ⊂ `required, neg, gender`; `gender = {label, conf, flags[], evidence[], conflict, source (tags\|model\|tags+model), effective, modelSkipped?}`; post bị chặn bị bỏ khỏi `items`. Với `-gender_mode model`: `genderModel = {checked, labeled, skipped}` hoặc `{error}` |
| `tags.resolve` | `{module, tags[]}` (tối đa 64) | `{items:[{tag, canonical, alias, posts, type, known}], hasList}`. Metatag: `type:'meta'`, `known:true`. Tag chứa `*`/`?`: `type:'wildcard'`, `known` = có tag khớp. `alias` chỉ true khi tag không có trong danh sách mà tag đích thì có |
| `tags.fuzzy` | `{module, text, limit=8 (≤ 50)}` | `[{tag, posts, type, distance}]`. Sai tối đa 1 ký tự (3–5 ký tự) hoặc 2 ký tự (từ 6 ký tự); thiếu/thừa `_` không tính; dưới 3 ký tự trả `[]` |
| `search.related` | `{module, tags, args[], limit=30 (≤ 200)}` | `{items:[{tag, count, posts, type}], sample}`: tag xuất hiện cùng trong trang 0 (200 post, đã qua bộ lọc cục bộ), trừ tag của truy vấn và metatag |
| `files.nonEmpty` | `{path}` | `{exists, nonEmpty}` |
| `index.search` | `{tags, module?, dateFrom?, dateTo?, page=0, pageSize=50 (≤ 200), order?}`. `tags`: chuỗi hoặc mảng; mọi điều kiện phải khớp: `tag`, `-tag`, `tiền_tố*`, `rating:g\|s\|q\|e`, `md5:<hash>`, `id:<n>` (tối đa 24 điều kiện). Ngày theo ngày post, `YYYY-MM-DD` hoặc `DD-MM-YYYY`, gồm cả hai đầu. `order`: `downloaded` (mặc định, mới nhất trước), `downloaded_asc`, `date`, `date_asc`, `score`, `id` | `{total, page, pageSize, terms[], items:[{module, id, md5, path, name, ext, size, tags, score, rating, date, downloadedAt, exists}]}`. Chỉ đọc index, không gửi request tới website. `exists` = file còn trên đĩa |
| `gender.modelStatus` | `{url, token?}` | `{url, reachable, local, error, model?: {id, repo, revision, license, inputSize, server, workers, loaded, device, idleUnload, hardware, ms}, check?: {level (ok\|warn), code (gpu\|no_gpu\|gpu_no_runtime\|cpu_forced\|vram_low\|gpu_fallback\|unknown), gpu, runtime, device, vramMinMiB, ramFreeMiB}}`: model server ở `url` dùng được không (`local` = máy này), chạy model trên phần cứng nào (`lustra/model_hw.py hardware_check`). Không có `url`: `{local: true, inProcess: {id, path, installed, bytes, missingRuntime, …}}` (model trong tiến trình). App chỉ gọi qua `gmodel:status` (main thêm token), renderer không gọi trực tiếp được |
| `gender.modelLoad` | `{url, token?}` | như `gender.modelStatus`, sau khi server nạp model (`POST /v1/load`); nạp lỗi → `reachable: true` kèm `error`. App: `gmodel:load` khi bật model |
| `gender.modelUnload` | `{url, token?}` | như `gender.modelStatus`, sau khi server giải phóng model (`POST /v1/unload`). App: `gmodel:unload` khi tắt model và server ở máy này |
| `index.stats` | — | `{path, exists, schema, files, posts, withMd5, tags, bytes, dbBytes, first, last, recording, modules:[{module, files, bytes, last}]}`. `exists` = false cho tới khi file đầu tiên được ghi; `recording` = false khi `LUSTRA_DLINDEX=0` |
| `files.identify` | `{names[] (≤ 500), module}`: tên file, đường dẫn, link post hoặc link ảnh | `{items:[{name, module, kind:'id'\|'md5'\|'', value, note}], queries:[{module, tags, count, dropped}], max}`. Nhận ra: tên Lustra đặt (`db_12345…`, `12345…` = module đang chọn), md5 32 ký tự (`<md5>.jpg`, `sample_<md5>`, `__tags__<md5>` của Danbooru), link post/ảnh của các site đã biết (so theo host và tên miền gốc trong `site` của manifest). Mỗi module một truy vấn: Danbooru dùng danh sách `id:1,2,3` / `md5:a,b` (100 mục mỗi danh sách); site khác dùng nhóm OR `(id:1~id:2)`, tối đa 64 mục |
| `app.shutdown` | — | (notification) thoát |

Notification từ sidecar: `ready` (giống `app.info`), `log {text, scope?}`, `tags.updateProgress`, `tags.updateDone`. Mã lỗi: `-32601` không có method, `-32602` tham số sai, `-32001` module chưa cài, `-32002` thao tác thất bại (kèm `data.log`), `-32003` không lấy được trang, `-32004` đang cập nhật danh sách tag của module đó, `-32000` lỗi khác. Đường dẫn trong `files.nonEmpty` hiểu `~` và đường dẫn tương đối theo thư mục home (giống job tải).

## 5. Giao thức job tải (CLI IPC)
- Lệnh: `LUSTRA_IPC=1 LUSTRA_CATALOG_AUTOLOAD=0 python -m lustra -module <abbr> [connection args] [date args] [download args] -- <tags...>`. Tag luôn đứng sau `--`, nên tag loại trừ trùng tên tuỳ chọn (`-webm`, `-v`) vẫn là tag.
- stdout: log thường, cùng các dòng `@@LUSTRA_PROGRESS {"state","stateName","task","tasks","total","totalAll","processed","success","failed","pages","cfChallenge","genderFiltered","blocked","known","streaming","scanning","bytes"}` (dòng DONE có thêm `maxId`: id lớn nhất đã xử lý, hoặc tìm được bằng `-get_maxid`; và `genderModel`: số post model nhận diện đã xem) (`known`: số post bỏ qua nhờ `-skip_known`; `bytes`: số byte file đã nhận trong lượt chạy, phần resume không tính). `streaming` = true khi file được tải trong lúc vẫn quét trang (truy vấn 1 task), khi đó `totalAll` còn tăng cho tới khi `scanning` = false. Dòng cuối cùng có `stateName: "DONE"` kèm `failedItems[]`, `genderStats{}`, `ok`, `exitCode`, `error`, `cancelled`.
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

## 5c. Model server (`python -m lustra.model_server`, F-21)
- HTTP/1.1 (keep-alive), JSON. Token (`LUSTRA_GENDER_MODEL_TOKEN`) qua `Authorization: Bearer …`, so sánh hằng thời gian; bắt buộc khi lắng nghe ngoài loopback (không thì server không chạy, trừ `--allow-no-token`).
- `GET /v1/status` → `{id, repo, revision, license, inputSize, deviceSetting (auto\|cpu\|gpu: --device), server: "lustra-model/1", workers, loaded, device ("" khi chưa nạp, "cuda", "cpu"…), idleUnload, hardware: {gpus: [{vendor, name, vramMiB, vramFreeMiB, shared}], providers, runtime, ramMiB, ramFreeMiB, cpus, os}}`. GPU: NVIDIA qua `nvidia-smi`, AMD / Intel trên Linux qua sysfs (`/sys/class/drm`); kết quả giữ 10 s.
- `POST /v1/tag`, body là bytes của ảnh (jpeg / png / webp / gif, ≤ 12 MB, cần `Content-Length`) → `{id, tags: {tag: xác suất ≥ 0,05}, ms}`. Model chưa nạp thì nạp trước.
- `POST /v1/load` (không body) → status sau khi nạp; không nạp được (ví dụ `--device gpu` mà không có GPU runtime) → 503 kèm lý do. `POST /v1/unload` (không body) → status sau khi giải phóng; ảnh tiếp theo nạp lại.
- Lỗi: 400 không phải ảnh / body thừa ở load, unload, 401 token, 404, 405, 411 thiếu `Content-Length`, 413 quá lớn (không đọc body, đóng kết nối), 500 lỗi của model (ví dụ hết VRAM: client dừng job), 503 bận (mọi worker đang chạy model quá 60 s; client chỉ thử lại 2 lần với `error: "busy"`) hoặc không nạp được model.
- Model nạp lười: khi có ảnh đầu tiên, `/v1/load` hoặc `--preload`; tự giải phóng sau `--idle-unload` giây không có ảnh (mặc định 900, 0 = giữ mãi).
- Model chạy trong **tiến trình con** (`ModelProcess`, multiprocessing `spawn`, pipe): giải phóng = tiến trình con thoát (sau khi các ảnh đang xử lý xong), trả lại toàn bộ RAM và VRAM. Bỏ session onnxruntime trong cùng tiến trình không đủ: trên CUDA còn 675 MB RAM và 224 MiB VRAM (context, bộ đệm cuDNN / cuBLAS). Tiến trình con chết (bị kill, crash) thì ảnh đang xử lý được thử lại một lần với tiến trình mới; chết tiếp thì 503. Đo trên máy dev (CUDA): server chưa nạp ~30 MB, model nạp ~530–850 MB RAM và ~1,3 GB VRAM ở tiến trình con, giải phóng xong VRAM về 0.
- Socket tắt Nagle (`TCP_NODELAY`): header và body phản hồi là hai lần ghi, Nagle cộng delayed ACK làm mỗi ảnh chậm thêm ~40 ms.
- `--workers` (mặc định 2) ảnh chạy model cùng lúc; `--device auto|cpu|gpu`; lần đầu chạy tự tải model (ghim revision, kiểm sha256) trừ khi `--no-download`; ảnh giải mã trong RAM, giới hạn 40 triệu pixel (chống decompression bomb). Log chỉ có method, đường dẫn, mã trả về, cùng dòng `hardware:` lúc khởi động và các lần nạp / giải phóng model.

## 6. Manifest extension
Schema: [`schemas/extension-manifest.schema.json`](schemas/extension-manifest.schema.json). Ràng buộc chính:
- `id` trùng tên thư mục;
- `abbr` gồm 2–4 ký tự `[a-z0-9]` và là duy nhất;
- `apiVersion` = 1;
- `entry` trỏ tới class kế thừa `lustra.download.Downloader`;
- khối `syntax` chỉ bắt buộc với site mà lõi chưa biết;
- tuỳ chọn `genderRules` (file luật giới tính trong thư mục extension; module sẽ được thêm capability `gender_tags`) và `safetyTags` (chỉ thêm vào danh sách chặn);
- tuỳ chọn `minSdkVersion` (`"MAJOR.MINOR"`): bản `lustra.sdk` tối thiểu extension cần. Lustra từ chối cài và nạp, kèm lời nhắn rõ, khi SDK của nó cũ hơn hoặc khác phiên bản chính. Template của trình tạo ghi `"1.0"`.

`lustra.sdk` (`lustra/sdk.py`, `SDK_VERSION` = `1.0`) là API ổn định cho người viết extension: `Downloader`, `DownloaderGelbooru`, `DownloaderDb`, `DownloaderJsonApi` + `JsonPost`, `ItemInfo`, `Comment`, `DownloadModes`, `MediaKind`, `ModuleCaps`, hằng số ngày / đuôi file, regex item chung, `HTMLContent`, `thread_exit`, `DownloadInterruptException`, `LUSTRA_USER_AGENT`, `trace`, `as_date`, `format_score`. Bản MINOR chỉ thêm tên; bản MAJOR mới được bỏ / đổi tên, sau khi đã báo deprecated ít nhất một bản MINOR. Extension cũ import thẳng `lustra.defines`, `lustra.download`… vẫn chạy.

Danh sách chặn tên miền: `lustra/safety_data/denied_domains.txt`, mỗi dòng `<tên miền | sha256:<hex>> <R6|R7|R8> [lý do]`; một mục chặn tên miền đó và mọi tên miền con.

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
  "search": { "module": "rx", "autocomplete": true, "safePreview": false, "filtersOpen": true,
              "saved": [{ "name": "", "module": "db", "tags": "", "at": 0,
                          "auto": { "every": 24, "maxId": 0, "lastRun": 0, "lastStatus": "", "lastCount": 0, "lastError": "" } }] },
  "download": { "path": "~/Downloads/Lustra", "threads": 4, "gender": { "select": [], "conf": "medium", "strict": false, "op": "keep|exclude", "mode": "tags|model" }, "images": true, "videos": true, "audio": true, "videoFormat": "", "lowRes": false,
                "prefix": true, "appendInfo": false, "reverse": false, "limit": 0, "parchi": false, "preserveDate": false, "skipKnown": false,
                "subfolderPerQuery": false, "dumpTags": false, "dumpSources": false, "dumpComments": false, "dumpHashes": false,
                "infoMode": "run|item|merge", "warnNonEmpty": true, "maxConcurrentJobs": 1, "notify": true },
  "connection": { "proxy": "", "ignoreProxy": false, "proxyNoDownload": false, "timeout": 0, "retries": 0, "headers": {}, "cookies": {} },
  "tags": { "autoUpdate": false, "autoUpdateDays": 7, "lastAutoUpdate": {} },
  "advanced": { "pythonPath": "", "verbose": false, "catalogUrl": "" },
  "ai": { "model": "claude-opus-5-5|claude-sonnet-5-5", "maxUsd": 2 },
  "genderModel": { "url": "http://127.0.0.1:8765", "enabled": false },  // enabled: người dùng bật model (sau khi kiểm GPU / VRAM); token ở secrets["@gendermodel"]
  "secrets": { "<abbr>": { "enc": "<base64 safeStorage>" }, "@anthropic": { "enc": "<base64 safeStorage>" },
               "@gendermodel": { "enc": "<base64 safeStorage>" } }  // khoá Claude: chỉ dạng mã hoá; khoá module và token model server: "plain" khi không có keyring
}
```

## 9. Thư mục dữ liệu
`LUSTRA_DATA_DIR`, hoặc mặc định: `~/.config/Lustra` (Linux), `%APPDATA%\Lustra` (Windows), `~/Library/Application Support/Lustra` (macOS). Đây cũng là `userData` của Electron. Bên trong gồm: `settings.json`, `extensions/`, `tags/`, `backgrounds/`, `icons/`, `cache/thumbs/`, `ratelimit/`, `index/downloads.sqlite` (index các lần tải, F-19; kèm `-wal`/`-shm` khi đang mở), `cache/gender_model.sqlite` (kết quả của model nhận diện giới tính theo post, F-21), `models/wd-vit-tagger-v3/` (chỉ khi model chạy trong tiến trình hoặc model server dùng thư mục dữ liệu này: `model.onnx`, `selected_tags.csv`, `manifest.json`), `extgen/drafts/<phiên>/` (bản nháp extension: `report.json` và `<id>/` gồm manifest, downloader, specs, tests/fixtures), `extgen/eval/<thời điểm>/` (báo cáo của `python -m lustra.aigen.eval`: `report.json`, `report.md`, `drafts/`).
