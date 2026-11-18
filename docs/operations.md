# Vận hành Lustra

## 1. Cài đặt môi trường (dev)
```bash
cd ~/Workspace/Lustra
uv venv .venv --python 3.13 --python-preference only-managed   # bản Python của uv có sẵn tkinter (GUI Tk cũ)
uv pip install --python .venv/bin/python -r requirements.txt ruff
cd app && npm install && node node_modules/electron/install.js   # tải binary Electron 45.2.1
```

## 2. Chạy
| Mục đích | Lệnh |
|---|---|
| App desktop | `cd app && npm start` (`npm run start:debug` mở DevTools) |
| CLI | `.venv/bin/python -m lustra -module db -path ~/Downloads/Lustra cat_ears rating:g` |
| GUI Tk cũ | `.venv/bin/python -m lustra` |
| Sidecar (debug) | `.venv/bin/python -m lustra.server`, rồi gõ `{"jsonrpc":"2.0","id":1,"method":"app.info"}` |
| UI mock trên trình duyệt | `python3 -m http.server 5178 --directory app`, mở `http://localhost:5178/renderer/index.html` |
| Tạo extension (CLI / skill) | `.venv/bin/python -m lustra.extgen survey https://site` → `new https://site --out .extgen-drafts` → `record .extgen-drafts/<id> --query "<tag>"` → `check .extgen-drafts/<id>` → `pack .extgen-drafts/<id> --out <file>.zip` |
| Công cụ index các lần tải | `.venv/bin/python -m lustra.dlindex_tools clean --dry-run`, `import ~/Pictures/booru --module db`, `retag ~/Downloads/Lustra --source index` (mục 3b) |
| Worker tạo extension (debug) | `(echo '{"cmd":"start","url":"https://site","mode":"template"}'; sleep 120) \| .venv/bin/python -m lustra.aigen.worker` |

Biến môi trường:
- `LUSTRA_PYTHON`: đường dẫn python cho app;
- `LUSTRA_DATA_DIR`: thư mục dữ liệu;
- `LUSTRA_USER_DATA`: userData của Electron;
- `LUSTRA_TAGS_DIR`: thư mục tag list ưu tiên;
- `LUSTRA_CATALOG_DIR`, `LUSTRA_CATALOG_URL`: catalog extension;
- `LUSTRA_CATALOG_KEYS_FILE`: **chỉ cho nhà phát triển**. File khoá công khai thêm vào danh sách tin cậy của catalog từ xa (cùng định dạng `lustra/safety_data/catalog_keys.json`), dùng để thử catalog tự host (mục 3d);
- `LUSTRA_CATALOG_ALLOW_UNSIGNED=1`: **chỉ cho nhà phát triển**. Nhận catalog từ xa không có file `.sig`; thẻ hiện "chưa ký", hộp xác nhận cảnh báo. Chữ ký có mà sai thì vẫn bị từ chối. Không bao giờ đặt biến này cho người dùng;
- `LUSTRA_IPC=1`: CLI chạy ở chế độ báo tiến độ;
- `LUSTRA_CATALOG_AUTOLOAD=0`: chỉ dùng extension builtin và đã cài (app đặt cho sidecar và job). Mặc định CLI và GUI Tk vẫn nạp extension trong catalog khi cần;
- `LUSTRA_CORE`: đường dẫn lõi đóng băng (`lustra-core`) cho app dev;
- `LUSTRA_STREAMING=0`: tắt pipeline streaming (quay lại kiểu quét hết rồi mới tải, dùng để so sánh / gỡ lỗi);
- `LUSTRA_DLINDEX=0`: không ghi file tải xong vào index các lần tải (mục 3b). Index vẫn đọc được, `-skip_known` vẫn chạy. Các test đặt sẵn biến này (`lustra/tests/__init__.py`);
- `LUSTRA_TAGS_LEARN=0`: không học danh sách tag từ post đã xem / đã tải (mục 3). Các test đặt sẵn biến này, `test_tag_sources` tự bật lại;
- `LUSTRA_GENDER_MODEL_URL`, `LUSTRA_GENDER_MODEL_TOKEN`: địa chỉ và token model server cho CLI (mục 3c; app dùng Cài đặt). Model server đọc `LUSTRA_GENDER_MODEL_TOKEN` làm token của nó;
- `LUSTRA_GENDER_MODEL_DIR`: thư mục model (model server hoặc model trong tiến trình) thay cho `<data>/models/wd-vit-tagger-v3`. Đặt biến này khi chạy test thì có thêm test với model thật;
- `LUSTRA_GENDER_MODEL_DEVICE`: `auto` (mặc định: GPU nếu onnxruntime có provider GPU, nếu không thì CPU), `cpu`, `gpu` (không có GPU thì báo lỗi thay vì chạy CPU); model server có `--device` tương ứng;
- `LUSTRA_API_KEY_<MODULE>` (ví dụ `LUSTRA_API_KEY_RX=KEY,USER_ID`): khoá API cho CLI và cron khi không có `-api_key`. App luôn tự truyền `-api_key` từ khoá đã lưu. **Rule34 bắt buộc khoá riêng của người dùng**: Lustra không có khoá dùng chung (khoá của tác giả bản gốc đã bị bỏ ngày 2026-11-18), thiếu khoá thì lõi dừng với lỗi `module 'RX' needs your own API key`;
- `LUSTRA_SECRET_ARGS`: danh sách JSON các tham số bí mật (`["-api_key", "KEY,USER"]`) do app truyền cho CLI; CLI đọc xong thì xoá khỏi môi trường;
- `LUSTRA_SECRET_MODULE_ARGS`: object JSON `{"db": ["-api_key", "KEY,LOGIN"]}`, khoá API theo module cho job nhập thư viện (`dlindex_tools import`); đọc xong thì xoá khỏi môi trường;
- `LUSTRA_SELFTEST_SECURITY=1`: thêm kiểm tra bảo mật vào chế độ tự kiểm thử;
- `LUSTRA_SELFTEST_DIR`: chạy tự kiểm thử Electron. Bước `generator` kiểm: khoá chỉ ghi (không lộ ra renderer, không có dạng chữ thường), phiên bị từ chối (R10), phiên cho site trong danh sách chặn tên miền bị từ chối (R7, lời nhắn tiếng Việt), cài từ catalog từ xa không nhận URL / sha256 từ renderer, không cài được bản nháp chưa đạt, cài cần xác nhận, dọn dẹp.
  Bước `indexTools` kiểm: `index.clean` / `index.cleanCancel` không gọi trực tiếp được qua `rpc`, dọn cần xác nhận (từ chối thì không chạy, chạy thử thì không hỏi), nhập thư viện chỉ với thư mục chọn từ hộp thoại và module hợp lệ, một job nhập chạy thử thật (lõi `index import --dry-run`) và thẻ job của nó, nhóm tải nhiều site (2–8 module, ép `-skip_known` / `-prefix`, site sau chờ site trước, renderer không tự tạo nhóm), giao diện (Kiểm tra / Dọn index ở Cài đặt, hộp nhập thư viện, chọn site thêm, khung nhóm trong hàng đợi; ảnh `20-…` tới `24-…`).

Chế độ AI của trình tạo extension cần gói `anthropic`: `uv pip install --python .venv/bin/python 'anthropic>=2.9'`, hoặc `pip install "lustra[ai]"`. Khoá Claude API nhập trong app (Cài đặt → AI tạo extension). Khoá được mã hoá bằng keyring của hệ điều hành; không có keyring thì app không lưu. Chế độ "Không dùng AI" không cần gói hay khoá này. Bản nháp nằm ở `<data>/extgen/drafts/`.

Exit code của CLI: `0` thành công, `1` lỗi, `2` sai tham số, `3` một số file lỗi, `130` bị huỷ. Script hoặc cron có thể dựa vào mã này.

## 3. Cập nhật danh sách tag (autocomplete, Tags A–Z)
**Nguồn danh sách** (`lustra/tags_updater.py`, lệnh `lustra-core tags` / `python -m lustra.tags_updater`):
- 5 module đã duyệt: Rule34 (`rx`, cần khoá của bạn), Danbooru (`db`, dùng khoá nếu có), Xbooru (`xb`), Bbooru (`bb`), e621 (`en`).
- Mọi extension tạo từ một engine có danh sách tag. Updater đọc `$generator.engine` và `site` trong manifest; trình tạo extension (`lustra.extgen`) ghi sẵn hai trường này, kèm `tagList: "<abbr>_tags.json"`.

| Engine | Cách đọc danh sách | Khoá |
|---|---|---|
| `gelbooru_dapi` | `index.php?page=dapi&s=tag&q=index`, phân trang theo số trang `pid` (danh sách của site không theo thứ tự id) | bắt buộc nếu manifest có `auth.apiKey` (site trả "Missing authentication"), nếu không thì dùng khi có (`API_KEY,USER_ID`) |
| `danbooru_json` | `tags.json`, con trỏ id `page=a<id>` | dùng khi có (`API_KEY,LOGIN`) |
| `moebooru_json` | `tag.json?order=name`, theo trang | không |
| `e621_json` | bản export `db_export/tags-<ngày>.csv.gz` (thử 4 ngày gần nhất) | không |
| `philomena_json` | `api/v1/json/search/tags`: 10.000 tag dùng nhiều nhất (giới hạn cửa sổ tìm kiếm của site); dấu cách trong tên tag thành `_` | không |
| `szurubooru_json` | `api/tags/`, theo offset | không |
| `shimmie_api` | `api/danbooru/find_tags`: cả danh sách trong một câu trả lời | không |

- Engine `shimmie` (không có API) và `custom` không có nguồn cập nhật: danh sách của chúng được học dần (xem dưới).
- Danh sách được ghi vào `<thư mục dữ liệu>/tags/<abbr>_tags.json`. App tìm danh sách theo tên file (file mới nhất của mỗi module), nên autocomplete và Tags A–Z dùng được ngay sau khi cập nhật.

**Extension mới có danh sách tag ngay** (yêu cầu của chủ repo, 2026-11-18):
- Engine có API danh sách tag và robots.txt cho phép: trình tạo tải luôn danh sách lúc bản nháp đạt (`lustra/extgen/taglist.py`), cả chế độ AI, chế độ template và skill (`python -m lustra.extgen new` / `tags`).
  - File `<abbr>_tags.json` nằm cạnh `manifest.json` (trường `tagList`), được đóng vào file .zip và nằm lại trong thư mục extension khi cài. Lõi dùng nó ngay sau khi cài.
  - Giới hạn cho bản nháp: 30 request, 90 giây, 8 MB. Danh sách lớn hơn, hoặc site cần khoá của người dùng, thì app tải ngay sau khi cài (`tags.update`), bằng khoá đã lưu nếu có.
  - Đọc danh sách cũng theo đúng luật lịch sự ở dưới (robots.txt nghiêm ngặt, `Crawl-delay`, `Retry-After`). Báo cáo phiên ghi kết quả ở `tagList` (`bundled`, `later`, `learned`, `failed`).
  - Eval harness của trình tạo (`python -m lustra.aigen.eval`) không tải danh sách tag.
- Site không có API danh sách tag (HTML tuỳ biến, Shimmie không có API) hoặc robots.txt cấm: app **học dần** danh sách (`lustra/tags_learned.py`), không gửi thêm request nào.
  - Nguồn: tag của các post hiện trong Preview (`search.preview`) và của các file đã tải (index các lần tải; app gọi `tags.learn` sau mỗi lượt tải xong, hoặc nút "Học từ file đã tải" ở Tags A–Z).
  - Mỗi post chỉ đếm một lần. File ghi dạng `"tag": "N posts (unknown)"`, với N là số post đã thấy ở đây, không phải số trên site; loại tag không biết.
  - Trạng thái ở `<thư mục dữ liệu>/tags/learned/<abbr>.sqlite`.
  - Chỉ học khi module chưa có danh sách nào từ nguồn. Khi updater, extension hoặc `2tags/` cho module một danh sách thật, việc học dừng và không ghi đè danh sách đó.

**Lịch sự** (mọi nguồn):
- **Nhịp request:** khoảng cách giữa hai request là `--delay` (mặc định 0,5 giây). Bộ cập nhật dùng giá trị lớn nhất trong các mức sau, tối đa 60 giây:
  - site của extension: ít nhất 1 giây;
  - giới hạn của khoá API: Rule34 công bố 60 request mỗi 60 giây cho mỗi khoá, nên 1,05 giây (xem "Giới hạn theo khoá" bên dưới);
  - `Crawl-delay` trong robots.txt.
- **Gặp 429 / 503:**
  - chờ theo `Retry-After`; nếu site không gửi thì chờ 5, 15, 30, 60, 120, 180 giây (tổng khoảng 7 phút), rồi mới dừng danh sách;
  - phần còn lại của lượt tải chạy chậm hơn: theo nhịp site ghi trong phản hồi ("limit of N requests every M second(s)"), không thì chậm 1,5 lần;
  - lỗi mạng thử lại tối đa 5 lần;
  - mỗi lần chờ hoặc thử lại đều hiện trong tiến độ (app và log), ví dụ `api.rule34.xxx asks to wait (HTTP 429): waiting 5 s, then one request every 1.05 s`.
- **Giới hạn theo khoá** (Rule34, và extension khai báo `auth.rateLimit` trong manifest):
  - Mặc định là giới hạn site công bố (`RATE_LIMIT_RX` trong `lustra/defines.py`, `auth.rateLimit` trong manifest).
  - Admin của site cấp thêm cho khoá của bạn thì nhập số mới ở **Cài đặt → API key theo module**, ô "Giới hạn của khoá" ngay dưới khoá (request mỗi phút; trống hoặc 0 = giới hạn site công bố).
  - App ghi số đó vào `<thư mục dữ liệu>/ratelimit/limits.json` (`{"rx": 120}`). Mọi tiến trình lõi đọc file này nên dùng số mới ngay, kể cả sidecar đang chạy. Dòng lệnh / cron: biến `LUSTRA_RATE_LIMIT_RX=120` thắng file.
  - Giới hạn tính theo khoá, nên mọi tiến trình dùng chung khoá giữ chung một nhịp qua file khoá `<thư mục dữ liệu>/ratelimit/<băm>.ts`: các job tải, Preview, cập nhật danh sách tag. Tải ảnh trong lúc cập nhật tag không còn làm vượt giới hạn. Trang yêu thích / pool của Rule34 là trang HTML của site chính, không tính vào giới hạn API.
  - Nhập số cao hơn mức site thật sự cho thì site trả 429: lõi tự chậm lại theo câu báo của site, chờ rồi tải tiếp.
- **Rule34 mất lâu:** danh sách đầy đủ khoảng 1.620 trang (1.000 tag mỗi trang, site không cho trang lớn hơn), khoảng 1,6 triệu tag.
  - Site trả tag theo thứ tự lưu trong database: tag vừa đổi số post bị dời xuống cuối, nên các tag phổ biến (`female`, `breasts`, `1girls`…) chỉ có ở **vài trang cuối**. Danh sách dở thiếu đúng những tag hay dùng nhất, vì vậy không bao giờ được dùng.
  - Trang càng sâu site trả càng chậm (trang 5: 0,5 giây, trang 1.200: 4 giây, trang 1.600: 6–7 giây). Một lần cập nhật đầy đủ mất khoảng 1,5–2 giờ. Giới hạn của khoá chỉ là nút thắt ở khoảng 100 trang đầu (site trả nhanh hơn 1 giây), nên khoá được cấp thêm request gần như không làm việc này nhanh hơn; nó giúp nhiều hơn cho việc tải ảnh.
  - Đóng app giữa chừng không sao: lần sau tải tiếp từ trang đã dừng.
- **Tải tiếp sau khi dừng giữa chừng** (Gelbooru: theo số trang `pid`; Danbooru: theo con trỏ id):
  - tiến độ được lưu ở `<thư mục tags>/.partial/<module>_tags.json` sau mỗi 20 trang và ngay khi dừng (429, mạng, đóng app);
  - lần cập nhật sau, trong vòng 3 ngày, tải tiếp từ con trỏ đó; xong thì xoá file tiến độ;
  - danh sách dở không bao giờ được dùng làm danh sách thật.
- Mỗi danh sách có ngân sách request `--max-requests` (mặc định 5000, tính cả robots.txt và các lần thử lại). Hết ngân sách thì dừng và không ghi danh sách dở.
- Không gửi cookie. Gặp bot challenge (`cf-mitigated: challenge`) thì dừng.
- Chỉ dùng https, chỉ gọi host của site (và tên miền cha), chỉ địa chỉ công khai, không gọi site trong danh sách chặn tên miền.
- Chỉ dùng khoá của chính bạn: `--api-key`, biến `LUSTRA_API_KEY_<MODULE>`, hoặc khoá đã lưu trong app. Lustra không có khoá dùng chung. Khoá được che (`<REDACTED>`) trong log và thông báo lỗi.
- robots.txt được đọc theo RFC 9309 (`lustra/robots.py`): luật dài nhất thắng, hiểu `*` và `$`, nhóm của `Lustra` được ưu tiên hơn nhóm `*`.
  - Site của extension: **tuân thủ nghiêm ngặt.** Nếu robots.txt chặn endpoint danh sách tag thì không cập nhật và báo lỗi rõ. Nếu không đọc được robots.txt (5xx, lỗi mạng) thì coi như bị chặn hết.
  - **Quyết định 2026-11-18 (chủ repo, phương án a — ngoại lệ cho 5 site cũ):** Rule34, Danbooru, Xbooru, Bbooru, e621 là ngoại lệ có ghi rõ. Chúng dùng API chính thức của site, nên các dòng `Disallow` không được áp dụng; `Crawl-delay`, `Retry-After` và ngân sách request vẫn được tôn trọng. Lý do: robots.txt của Danbooru có `Disallow: /` và `Disallow: /*.json` cho mọi user-agent, nếu tuân thủ nghiêm thì `db` không cập nhật được nữa.
- Sanity check giữ nguyên: nếu danh sách mới ít hơn một nửa danh sách đang dùng thì không ghi (dùng `--force` để ghi).

**Trong app** (Tags A–Z → Danh sách tag):
- Bảng liệt kê mọi extension đã cài, có cột "Nguồn" (rê chuột để xem site, cách đọc, có tuân thủ robots.txt không).
  - Module có nguồn: có nút "Cập nhật".
  - Module bắt buộc khoá mà chưa có khoá: ghi "cần API key" (nhập ở Cài đặt → API key).
  - Module không có nguồn: cột "Nguồn" ghi "học từ post đã xem / đã tải", có nút "Học từ file đã tải"; cột dung lượng ghi "(đã học)" khi danh sách là danh sách học được. Module không có nguồn nhưng có danh sách đi kèm (ví dụ `2tags/` của repo) ghi "không có nguồn".
  - Cài một extension: nếu chưa có danh sách đi kèm mà extension có nguồn, app bắt đầu tải ngay.
- "Tự cập nhật mỗi N ngày khi mở app": Rule34 và Danbooru, bằng khoá đã lưu; Rule34 bị bỏ qua khi chưa có khoá.
- "Cập nhật theo lịch của hệ thống, kể cả khi app đóng" (mỗi tuần vào thứ Hai, hoặc mỗi ngày, lúc khoảng 4 giờ sáng):
  - Linux và macOS: app thêm hoặc xoá một khối có đánh dấu `# BEGIN lustra-tags` … `# END lustra-tags` trong crontab của người dùng, không đụng các dòng khác.
  - Windows: tác vụ `Lustra tag lists` của Task Scheduler.
  - Bản AppImage không hỗ trợ, vì lõi chỉ tồn tại khi app chạy.
- Lịch chạy `lustra-core tags --modules auto --out <thư mục dữ liệu>/tags --min-posts 2 --log <thư mục dữ liệu>/logs/tags-schedule.log`; bản phát triển chạy `python -m lustra.tags_updater`.
  - `auto` là mọi module đã cài có nguồn tag và không bắt buộc khoá: db, xb, bb, en (nếu đã cài) và các extension tạo từ engine.
  - Danh sách module được tính lúc lịch chạy, nên extension cài sau cũng được cập nhật mà không cần ghi lại mục lịch.
- Lịch không đọc được khoá đã mã hoá của app, nên module bắt buộc khoá (Rule34, extension Gelbooru cần khoá) không chạy theo lịch.
- App ghi lại mục lịch mỗi lần mở, nên đường dẫn lõi luôn đúng sau khi cập nhật app.

**Dòng lệnh** (bản checkout repo):
```bash
.venv/bin/python -m lustra.tags_updater --modules auto            # như lịch của app
.venv/bin/python -m lustra.tags_updater --modules fq --delay 1    # một extension, theo abbr
.venv/bin/python -m lustra.extgen tags <thư mục bản nháp>         # tải danh sách tag vào một bản nháp
```

**Không qua app** (bản checkout repo, danh sách ghi vào `2tags/` của repo):
```bash
chmod +x tools/cron/lustra-update-tags.sh
crontab -l 2>/dev/null | cat - tools/cron/crontab.example | crontab -    # thứ Hai 04:17
```
Hoặc dùng systemd user timer:
```bash
mkdir -p ~/.config/systemd/user && cp tools/systemd/lustra-update-tags.* ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now lustra-update-tags.timer
```
Biến môi trường của script:
- `LUSTRA_TAG_MODULES` (mặc định `rx,db`);
- `LUSTRA_TAG_MIN_POSTS` (mặc định 2);
- `LUSTRA_API_KEY_RX` (`KEY,USER_ID`): bắt buộc cho Rule34, Lustra không có khoá dùng chung;
- `LUSTRA_API_KEY_DB` (`API_KEY,LOGIN`);
- `LUSTRA_TAGS_OUT`.

Log ghi ở `~/.local/state/lustra/update-tags.log`. Chạy tay: `.venv/bin/python tools/update_tags.py --modules db --delay 0.5`.

## 3b. Index các lần tải
- File: `<data dir>/index/downloads.sqlite` (kèm `-wal`, `-shm` khi đang có tiến trình mở). Mỗi file tải xong ở chế độ `full` được ghi một dòng (module, id, md5, đường dẫn, tags, score, rating, ngày post, thời điểm tải). Không đọc nội dung file, không gửi dữ liệu ra ngoài.
- Bỏ qua post đã tải: CLI `-skip_known`; app: ô "Bỏ qua file đã tải" trong tuỳ chọn tải, mặc định ở Cài đặt → Index các lần tải. Post chỉ bị bỏ qua khi file đã ghi trong index vẫn còn trên đĩa, nên xoá file là cách tải lại.
  ```bash
  .venv/bin/python -m lustra -module db -skip_known -path ~/Downloads/Lustra -- cat_ears rating:g
  ```
- Nhiều job ghi cùng lúc an toàn (WAL, chờ khoá tối đa 10 s, mỗi lô thử lại 3 lần). Nếu một lô vẫn không ghi được, log có `Warning (W2): download index: …` và lượt tải không bị ảnh hưởng.
- Xem nhanh bằng `sqlite3`: `sqlite3 ~/.config/Lustra/index/downloads.sqlite "SELECT module, COUNT(*) FROM files GROUP BY module"`. Sidecar: `{"jsonrpc":"2.0","id":1,"method":"index.stats"}`, `{"jsonrpc":"2.0","id":2,"method":"index.search","params":{"tags":"cat_ears -rating:e","pageSize":20}}`.
- Sao lưu: copy file khi không có job nào chạy, hoặc `sqlite3 <file> ".backup <đích>"`. Làm lại từ đầu: đóng app và mọi job rồi xoá cả ba file `downloads.sqlite*`; index được tạo lại ở lần tải sau.
- **Dọn index** (bỏ dòng có file đã mất, không đụng tới file): app: Cài đặt → Index các lần tải → **Kiểm tra** (chỉ đếm) rồi **Dọn index…** (hộp xác nhận; ô "Xoá cả dòng có thư mục không còn" mặc định tắt: cắm ổ ngoài / kết nối thư mục mạng trước khi dọn). Chạy nền, dừng được, job tải vẫn chạy bình thường. CLI:
  ```bash
  .venv/bin/python -m lustra.dlindex_tools clean --dry-run
  ```
  Bỏ `--dry-run` để xoá; thêm `--missing-folders` để xoá cả dòng có thư mục đã mất; `--json` in kết quả dạng JSON.
- **Nhập thư viện có sẵn**: app: Cài đặt → Index → **Nhập thư mục…** (hoặc trang Đã tải → **Nhập thư viện…**), chọn thư mục bằng hộp thoại, rồi theo dõi job ở trang Tải xuống; nút **Danh sách** trên thẻ job liệt kê tên không nhận ra, post không có trên site, request lỗi. CLI (mỗi lần chạy chỉ tra những file chưa có trong index, nên chạy lại để thử tiếp phần lỗi):
  ```bash
  .venv/bin/python -m lustra.dlindex_tools import ~/Pictures/booru --module db --dry-run
  ```
  `--module`: module cho tên không có tiền tố (`12345.jpg`, `<md5>.jpg`), chỉ dùng khi cả thư mục là file của một site; `--no-recursive`; `--verify-md5` (đọc từng file, chỉ nhập file trùng md5 với file gốc trên site); `--dry-run` (chỉ nhận tên, không request); `--json`. Tốc độ: Danbooru 100 post mỗi request, site khác 1 post mỗi request, tối đa 2 request mỗi giây (10 000 file rx ≈ 1,5 giờ). Khoá API của module lấy từ `-api_key` trong `LUSTRA_SECRET_MODULE_ARGS` (app tự truyền).
- **Re-tag từ index**: GUI Tk (Tools → Re-tag files…) hoặc CLI:
  ```bash
  .venv/bin/python -m lustra.dlindex_tools retag ~/Downloads/Lustra --source index
  ```
  `--source both` (mặc định: index trước, file info JSON sau), `--types artist,character` (theo loại tag, cần danh sách tag), `--module` (bộ lọc tag của module khi `--types auto`). Exit code `3` khi có file không tìm được info. Re-tag / un-tag / sắp xếp file trong GUI Tk tự cập nhật đường dẫn trong index.
- **Tải nhiều site**: Tìm kiếm → Bộ lọc & tuỳ chọn tải → **Tải thêm từ site khác**, chọn thêm site rồi bấm **Tải xuống (N site)**. Mỗi site một job, chạy lần lượt, luôn bỏ qua file đã tải và đặt tiền tố module cho tên file; post trùng md5 với file site trước đã tải thì không tải lại. Thử truy vấn trên từng site bằng Preview trước: tên tag, metatag và alias khác nhau giữa các site.

## 3c. Model nhận diện giới tính (`-gender_mode model`)
Model (~361 MB) chạy ở **model server** riêng; app và CLI chỉ gọi API nên bản app không kèm numpy / onnxruntime / Pillow / model. Lần đầu chạy server tự tải model từ Hugging Face `SmilingWolf/wd-vit-tagger-v3` (revision ghim, kiểm sha256). Mỗi ảnh ~0,4 s trên CPU (Ryzen, 4 luồng).

Server nạp model khi có ảnh đầu tiên (hoặc khi app bật model, `--preload`) và giải phóng sau 15 phút không dùng (`--idle-unload 900`, `0` = giữ mãi). Model chạy trong một tiến trình con; giải phóng là tiến trình con thoát, nên RAM và VRAM được trả hết. Đo trên máy dev: server chưa nạp ~30 MB; model trên CUDA ~530–850 MB RAM và ~1,3 GB VRAM; giải phóng xong VRAM về 0. Log khởi động có dòng `hardware:` (GPU, VRAM trống, onnxruntime có GPU hay không), mỗi lần nạp có `model loaded on <cpu|cuda> in … s`.

- **Trên máy này** (app mặc định gọi `http://127.0.0.1:8765`, không cần token):
  ```bash
  uv pip install --python .venv/bin/python 'numpy>=1.26' 'onnxruntime>=1.18' 'pillow>=10'
  ```
  ```bash
  .venv/bin/python -m lustra.model_server
  ```
  Chạy nền theo user: `cp tools/systemd/lustra-model-server.service ~/.config/systemd/user/ && systemctl --user enable --now lustra-model-server`.
- **GPU (VRAM)**: model chạy trên GPU khi onnxruntime của server là bản GPU. NVIDIA: `onnxruntime-gpu` 1.32 dùng CUDA 13 nên cần driver 580 trở lên (`nvidia-smi` ghi "CUDA Version: 13.0"). Thay `onnxruntime` bằng `onnxruntime-gpu` kèm CUDA / cuDNN từ pip (`.venv` tăng từ 227 MB lên 2,6 GB; hoặc extra `.[model-gpu]` thay cho `.[model]`), rồi khởi động lại server:
  ```bash
  uv pip uninstall --python .venv/bin/python onnxruntime
  ```
  ```bash
  uv pip install --python .venv/bin/python 'onnxruntime-gpu[cuda,cudnn]'
  ```
  `GenderTagger` gọi `onnxruntime.preload_dlls()` để nạp CUDA / cuDNN từ các gói `nvidia-*`. Provider GPU không nạp được thì onnxruntime rơi về CPU: status báo `device: "cpu"`, app báo `gpu_fallback`; `--device gpu` biến trường hợp này thành lỗi. DirectML (Windows), ROCm / MIGraphX (AMD) được chọn nếu có, chưa thử; TensorRT (có trong danh sách provider của onnxruntime-gpu) không dùng. Image Docker mặc định chỉ có CPU; bản GPU: `docker build -f tools/model_server/Dockerfile --build-arg ORT_PACKAGE='onnxruntime-gpu[cuda,cudnn]' -t lustra-model-server:gpu .` rồi chạy với `--gpus all` (cần NVIDIA Container Toolkit). Đo trên máy dev: image 4,41 GB, nạp trên CUDA, 18 ms / ảnh, token bắt buộc, giải phóng trả lại VRAM.
  Đo trên RTX 4070 Ti SUPER (driver 580.178, onnxruntime-gpu 1.32.0, CUDA 13.5 / cuDNN 9.28 từ pip): riêng model 8 ms / ảnh (CPU 323 ms), qua HTTP ~18 ms / ảnh, 2 luồng ~10 ms / ảnh; VRAM ~1,3 GB; nạp 0,7 s. Xác suất lệch với CPU < 0,001, nhãn như nhau. `--device cpu` vẫn chạy CPU (app báo `cpu_forced`).
- **Bản chạy độc lập** (cho người dùng app không có mã nguồn hay Python): `python tools/build_model_server.py [--variant cpu|directml]` tạo `dist-model-server/lustra-model-server/` và file nén `lustra-model-server-<os>-<arch>-<variant>_v<phiên bản>.tar.gz` (`.zip` trên Windows). Script tự dựng môi trường build riêng (uv, hoặc venv + pip) với numpy, onnxruntime bản CPU (hoặc `onnxruntime-directml` trên Windows), Pillow, requests, yarl, PyInstaller, nên không phụ thuộc onnxruntime-gpu của `.venv`. Bản đóng gói không kèm model (lần đầu chạy tự tải), có `LICENSE`, `THIRD_PARTY_NOTICES.md` và script chạy nền của hệ điều hành build ra. Smoke test: `--help`; có model trên máy thì chạy server, nhận diện một ảnh trắng, giải phóng; không có model thì kiểm việc báo thiếu model. Đo trên Linux x86_64: 162 MiB giải nén, 47 MiB nén; model nạp trên CPU trong 0,7 s. CI: job `model-server` trong `.github/workflows/build.yml` (Linux, macOS, Windows CPU, Windows DirectML).
  Chạy nền mỗi khi đăng nhập:
  - Linux: giải nén vào `~/.local/share/lustra-model-server`, chép `lustra-model-server-bundle.service` (trong bản đóng gói, hoặc `tools/systemd/`) thành `~/.config/systemd/user/lustra-model-server.service`, rồi `systemctl --user daemon-reload && systemctl --user enable --now lustra-model-server`. Đã thử bản đóng gói dưới `systemd-run --user`.
  - macOS: `sh install-model-server.sh` trong thư mục bản đóng gói (LaunchAgent `local.lustra.model-server`, log `~/Library/Logs/lustra-model-server.log`); gỡ: `--uninstall`. Mới thử phần sinh plist trên Linux với `launchctl` giả; chưa chạy trên macOS.
  - Windows: `powershell -ExecutionPolicy Bypass -File install-model-server.ps1` trong thư mục bản đóng gói (Scheduled Task "Lustra model server" lúc đăng nhập, ẩn cửa sổ bằng `conhost.exe --headless`); gỡ: `-Uninstall`. Chưa chạy trên Windows.
- **Image đã phát hành** (CPU, amd64 + arm64): `ghcr.io/leiz-workspace/lustra-model-server:<version>` và `:latest`, đăng bởi workflow release. Lắng nghe trong container là ngoài loopback nên cần token (nhập cùng token ở Cài đặt của app):
  ```bash
  docker run -d --name lustra-model --restart unless-stopped -e LUSTRA_GENDER_MODEL_TOKEN=<token> -p 127.0.0.1:8765:8765 -v lustra-model:/models ghcr.io/leiz-workspace/lustra-model-server:latest
  ```
- **Docker tự build** (máy này hoặc máy chủ; lắng nghe ngoài loopback bắt buộc có token):
  ```bash
  docker build -f tools/model_server/Dockerfile -t lustra-model-server .
  ```
  ```bash
  docker run -d --name lustra-model --restart unless-stopped -e LUSTRA_GENDER_MODEL_TOKEN=<token> -p 127.0.0.1:8765:8765 -v lustra-model:/models lustra-model-server
  ```
  Model tải vào volume `/models` ở lần chạy đầu. Đã có model trên máy thì gắn thẳng: `-v ~/.config/Lustra/models/wd-vit-tagger-v3:/models:ro`. Image ~1,05 GB (chưa gồm model; có ffmpeg để đọc video cho `-gender_verify_media`, không có ffmpeg ~420 MB). Cho máy khác dùng: `-p 8765:8765` và đặt server sau reverse proxy https (app từ chối http tới máy ngoài mạng nội bộ).
- **App**: Cài đặt → Model nhận diện giới tính: công tắc **Dùng model** (mặc định tắt), địa chỉ API, token (nếu server có), nút Kiểm tra, dòng **Phần cứng của server**, nút **Hướng dẫn** (hướng dẫn chung, `app/renderer/guide.js`). Bật: app hỏi server GPU / VRAM trước; không có GPU có VRAM riêng ≥ 1,5 GB, onnxruntime bản CPU, server bị đặt `--device cpu` hoặc VRAM trống không đủ thì hiện cảnh báo khuyên để tắt (Hướng dẫn / Vẫn bật (chạy bằng CPU) / Để tắt), rồi nạp model ngay và báo chạy bằng gì. Tắt: job chỉ lọc theo tag, server trên máy này giải phóng model. Sau khi bật: Bộ lọc giới tính → Nhận diện → "Tag + model (ảnh)". Server không phản hồi thì preview ghi lỗi, job dừng với `Error: '-gender_mode model': model server … is not reachable`.
- **CLI**:
  ```bash
  .venv/bin/python -m lustra -module xb -gender only_female,both -gender_mode model -gender_model_url http://127.0.0.1:8765 -path ~/Downloads/Lustra -- scenery
  ```
  Token: `-gender_model_token` hoặc `LUSTRA_GENDER_MODEL_TOKEN`. Không có địa chỉ thì CLI chạy model trong tiến trình (cần `pip install "lustra[model]"` và `python -m lustra.gender_model install`). Log có `Gender model: looking at N / M item(s) …` và `N labeled, K still unknown`.
- **Xem lại video / ảnh động sau khi tải** (`-gender_verify_media`, F-21, plan §3.5 P4): file video / ảnh động vừa tải của post mà tag chưa đủ kết luận được gửi tới `POST /v1/frames`, server gắn tag tối đa 8 khung; file không khớp bộ lọc được **chuyển** vào `<thư mục lưu>/_gender_mismatch/` (không xoá).
  - Server cần `ffmpeg` trên PATH để đọc video (không có thì dùng PyAV nếu đã cài); ảnh động (GIF / APNG / WebP) chỉ cần Pillow. Dòng `media (/v1/frames): videos ffmpeg …, animated pictures pillow …` lúc khởi động server cho biết server đọc được gì. Image Docker đã có ffmpeg (gói Debian). Server không có ffmpeg / PyAV: job cảnh báo W2, video giữ nguyên, không xem lại.
  - CLI (thêm vào lệnh ở trên): `-gender_verify_media`. Log có `… does not match the gender filter (only_male on 8 frame(s)), moved to _gender_mismatch/`; với `-verbose` có thêm dòng cho file khớp. Dòng tiến độ / DONE có `genderVerified`, `genderMismatch`. App: bộ lọc giới tính → ô **Kiểm tra video sau khi tải** (hiện khi đã bật model, dùng được với "Tag + model (ảnh)"); card job hiện "Không khớp, đã chuyển: n / m file đã xem lại".
  - Đo trên máy dev: model trên GPU (RTX 4070 Ti SUPER, CUDA), mỗi video 4–5 s (512 px / 640 px) qua HTTP ~0,5 s (phần lớn là 8 lần chạy ffmpeg), GIF 10 khung ~0,12 s; image Docker (CPU) 2,7–4,2 s mỗi file. File tối đa 200 MB; ffmpeg mỗi lần tối đa 30 s, cả file tối đa 120 s. Image Docker có ffmpeg: ~1,05 GB trên đĩa (trước đó ~420 MB), vì gói `ffmpeg` của Debian kéo theo nhiều thư viện codec.
  - Kiểm tra tay: `python -m lustra.media_frames clip.mp4 anim.gif` (khung nào được lấy, bộ giải mã; không cần model), `LUSTRA_GENDER_MODEL_URL=http://127.0.0.1:8765 python -m lustra.gender_model classify clip.mp4` (nhãn, bằng chứng, số khung).
- Công cụ: `python -m lustra.gender_model status` (có `LUSTRA_GENDER_MODEL_URL` thì kiểm server), `install`, `remove`, `classify ảnh.jpg …` (in nhãn, độ tin cậy, bằng chứng, safety tag; video / ảnh động thì xét trên nhiều khung; qua server nếu có biến địa chỉ).
- Kết quả theo post nằm ở `<data>/cache/gender_model.sqlite` của client; xoá file này để model xem lại.

## 3d. Catalog extension từ xa: ký số
App và CLI chỉ tin catalog từ xa (`Settings → Advanced → Catalog URL`, `LUSTRA_CATALOG_URL`) khi `index.json` có file chữ ký `index.json.sig` cạnh nó, ký bằng Ed25519 với khoá có trong `lustra/safety_data/catalog_keys.json`. Lõi kiểm chữ ký trên đúng các byte của `index.json` trước khi đọc bất kỳ mục nào; mỗi mục ghi sha256 của archive nên chữ ký phủ luôn các archive. Kiểm bằng `lustra/ed25519.py` (Python thuần theo RFC 8032, có test vector), không cần thư viện thêm.

**Catalog chính thức** (từ Lustra 2.0.4): `https://github.com/Leiz-Workspace/Lustra/releases/download/catalog/index.json`, là mặc định của ô "Catalog URL" (`settings.advanced.catalogUrl`; ô để trống cũng dùng URL này).
- Các file nằm trong release `catalog` của repo GitHub: `index.json`, `index.json.sig` và một zip cho mỗi extension trong `extensions/`. Tag `catalog` không khớp `v*`, nên không kích workflow phát hành nào.
- Chữ ký dùng khoá `b852144452eea5dc`; khoá công khai có trong `catalog_keys.json`. Bản Lustra cũ hơn 2.0.4 không có khoá nên từ chối catalog này.
- Khoá bí mật nằm ngoài repo, trên máy của người bảo trì: `~/lustra-keys/signing-key.pem` (thư mục 0700, file 0600). Phải có bản sao lưu offline: mất khoá thì phải phát hành khoá mới; lộ khoá thì phải bỏ khoá khỏi app ngay (xem "Lộ khoá" bên dưới).

Tạo khoá (đã làm một lần, ngày 2026-11-18):
```bash
python tools/sign_catalog.py genkey --out ~/lustra-keys/signing-key.pem    # file 0600, không ghi đè; giữ offline, có bản sao lưu
python tools/sign_catalog.py pubkey --key ~/lustra-keys/signing-key.pem    # in mục {keyId, publicKey, comment}
```
Dán mục in ra vào `"keys"` của `lustra/safety_data/catalog_keys.json`, commit file đó (chỉ có khoá công khai) rồi phát hành bản Lustra mới. Không bao giờ commit khoá bí mật (`.gitignore` bỏ qua `*.pem`, `*.key`, `signing-key*`); có thể tạo khoá bằng `openssl genpkey -algorithm ed25519 -out signing-key.pem`, cùng định dạng.

Cập nhật catalog chính thức (khi một extension trong `extensions/` đổi `version`, và trước ngày `expires`, hiện là 2027-05-17):
```bash
python tools/build_catalog.py --out /tmp/lustra-catalog     # zip tất định cho mỗi extension + index.json
python tools/sign_catalog.py sign /tmp/lustra-catalog/index.json --key ~/lustra-keys/signing-key.pem --expires-days 180
python tools/sign_catalog.py verify /tmp/lustra-catalog/index.json
gh release upload catalog /tmp/lustra-catalog/* --repo Leiz-Workspace/Lustra --clobber
```
Extension không đổi giữ nguyên sha256, vì zip tất định. Zip của bản cũ có thể giữ lại trong release cho tới khi người dùng đã cập nhật.

Mỗi lần sửa catalog:
```bash
python tools/sign_catalog.py sign index.json --key ~/lustra-keys/signing-key.pem --expires-days 180   # ghi index.json.sig
python tools/sign_catalog.py verify index.json     # kiểm như app, với khoá đi kèm bản Lustra đang chạy
```
Đăng cả `index.json` lẫn `index.json.sig` ở cùng thư mục (URL chữ ký = URL index + `.sig`, giữ query). `--expires-days` ghi trường `expires` vào `index.json` rồi mới ký: quá hạn đó app từ chối catalog, nên phải ký lại trước khi hết hạn (chống việc dùng mãi một catalog cũ). Định dạng mục: `{"id", "name", "abbr", "version", "url" (https), "sha256", "site", "description"}`; công cụ từ chối ký nếu có mục app sẽ bỏ qua. Ký dùng gói `cryptography` nếu có (thời gian hằng), không thì dùng `lustra.ed25519`; chữ ký Ed25519 tất định nên hai cách ra cùng kết quả.

Đổi khoá: thêm khoá mới vào `catalog_keys.json` và phát hành, ký catalog bằng khoá mới khi phần lớn người dùng đã cập nhật, rồi bỏ khoá cũ ở bản sau. Lộ khoá: bỏ khoá đó khỏi `catalog_keys.json` và phát hành bản sửa ngay.

Thử catalog tự host khi phát triển: tạo khoá thử trong thư mục tạm, ký, rồi chạy app hoặc lõi với `LUSTRA_CATALOG_KEYS_FILE=<file json có mục khoá thử>`; hoặc `LUSTRA_CATALOG_ALLOW_UNSIGNED=1` cho catalog chưa ký.

## 3e. Danh sách chặn tên miền
`lustra/safety_data/denied_domains.txt` liệt kê các tên miền không bao giờ được tạo hay cài extension: dịch vụ streaming thương mại / có DRM (R7), site đăng lại nội dung trả phí của người khác (R6), site có nội dung bất hợp pháp (R8). Mỗi dòng: `<tên miền> <R6|R7|R8> [lý do]`; một mục chặn tên miền đó và mọi tên miền con. Với R8, người bảo trì nên ghi dạng băm để file không in tên site:
```bash
python -c "import hashlib,sys; print('sha256:' + hashlib.sha256(sys.argv[1].encode()).hexdigest())" ten-mien.example
```
Chặn ở: chuẩn hoá link site của trình tạo (`survey`, `fetch`, `new`, phiên AI, trước mọi request và lời gọi model), mọi request của `SafeFetcher` (kể cả redirect và robots.txt), kiểm tra offline bản nháp (`domain:R7`…), mọi đường cài (thư mục, zip, catalog cục bộ, catalog từ xa) và lúc nạp extension. Host được so ở mọi dạng mà thư viện mạng có thể đi tới: chữ thường, bỏ dấu chấm cuối, giải mã `%xx`, ASCII / punycode của tên miền quốc tế (IDNA 2008 / UTS 46 như `requests`, và IDNA 2003: ký tự full-width, dấu chấm kiểu Nhật, ký tự rộng 0). URL có `\`, khoảng trắng hoặc ký tự điều khiển bị từ chối (các thư viện tách host khác nhau). Địa chỉ IP của một tên miền bị chặn không được dò ngược. Thêm dòng không cần sửa code; `lustra.tests.test_extgen.DenylistTests` kiểm file và các cách lách.

## 3f. Đánh giá trình tạo extension (eval)
`python -m lustra.aigen.eval` chạy trình tạo trên một bộ site (mặc định `lustra/aigen/eval_sites.json`: kết quả mong đợi, engine, rule) và đo tỉ lệ đạt, chi phí (từ `usage` của API), số lượt model, số bước tool, số request tới site, thời gian, lỗi. Báo cáo `report.json` + `report.md` nằm ở `<data>/extgen/eval/<thời điểm>/` (hoặc `--out`). Bản nháp nằm trong thư mục báo cáo, không bao giờ được cài; mỗi site còn kiểm "không cài gì" và "khoá API không có trong file nào".
```bash
# 1. Ghi dữ liệu site thật một lần, chế độ không AI (miễn phí; lịch sự: robots.txt, 1 request/giây)
.venv/bin/python -m lustra.aigen.eval run --mode template --live --record ~/lustra-eval-fixtures
# 2. Chạy lại offline, không mạng: template, hoặc model giả lập (đo phần tính chi phí, không gọi API)
.venv/bin/python -m lustra.aigen.eval run --mode template --fixtures ~/lustra-eval-fixtures
.venv/bin/python -m lustra.aigen.eval run --mode fake --fixtures ~/lustra-eval-fixtures
# 3. Ước tính chi phí trước khi chạy với Claude thật (không gọi API)
.venv/bin/python -m lustra.aigen.eval estimate --max-usd 2
# 4. Chạy với Claude thật: TỐN PHÍ, cần --confirm-cost, khoá từ ANTHROPIC_API_KEY hoặc `ant auth login`
ANTHROPIC_API_KEY=... .venv/bin/python -m lustra.aigen.eval run --mode ai --live --confirm-cost --max-usd 2
```
- `--only a,b` chạy một số site; site có `"modes": ["ai"]` (site HTML tùy biến) chỉ chạy ở chế độ AI. Chạy offline với `--mode ai --fixtures DIR` cũng được, nhưng request nào model gọi mà chưa ghi sẽ nhận 404 và được liệt kê ở `missing`.
- Ước tính chi phí (`estimate`): (1) **trường hợp xấu nhất** = số site × trần mỗi phiên (`--max-usd`, phiên không bắt đầu lượt mới khi đã chạm trần; lượt cuối có thể vượt một ít); (2) **mô hình**: một phiên `--turns` lượt (mặc định 14), lượt đầu ghi cache tiền tố (system prompt + tool, đếm theo 3,5 ký tự / token), lượt sau đọc cache tiền tố + lịch sử và ghi phần mới (`--out-tokens` 1500 + `--result-tokens` 2500), giá theo `MODELS` trong `lustra/aigen/session.py` (Opus 5.5: 4 / 20 USD mỗi triệu token vào / ra, đọc cache 0,20, ghi cache 5). Với mặc định: ~0,77 USD / phiên, 9 site ~7 USD, xấu nhất 18 USD; (3) **đo được**: trung bình `usd` các phiên AI cũ (báo cáo eval và `report.json` của bản nháp) nếu có. Khi chạy thật, lệnh in ước tính và dừng cả bộ khi tổng chi phí chạm tổng các trần.

## 3g. Đánh giá model giới tính (`python -m lustra.gender_eval`, plan gender-filter-model P1)
Công cụ đo độ chính xác của tầng 0 (tag) và tầng 1 (model) theo plan §3.2, §3.7. **Không lưu ảnh**: ảnh được tải vào RAM bằng session của downloader (cùng header, proxy, giãn cách request), cho người xem hoặc gửi tới model rồi bỏ. File ra chỉ có id post, md5, URL, tag, nhãn và số đo. Nên chạy với `LUSTRA_DATA_DIR` riêng nếu không muốn đụng thư mục dữ liệu thật (chỉ thư mục `ratelimit/` được dùng).

1. **Lấy mẫu** (chỉ metadata, qua downloader của module, cách nhau ≥ 1 s mỗi trang):
   ```bash
   .venv/bin/python -m lustra.gender_eval sample --module db --query "random:100 rating:g" --query "1boy random:100 rating:g" --query "no_humans random:100 rating:g" --query "gentags:<=8 random:100 rating:g" --quota only_female=60,only_male=60,both=60,none=60,sparse=80,weak=20 --out eval/manifest.json
   ```
   - Mỗi `--query` là một truy vấn (không có nhóm OR), lấy `--pages` trang. Trên Danbooru, `random:N` chỉ trả một trang: muốn thêm post thì lặp lại cùng truy vấn; post trùng bị bỏ.
   - Thứ tự loại (log đếm từng bước, ghi vào `exclusions` của manifest): 1 danh sách chặn cứng §4.1 trên tag và alias (`gender_rules/*.json` + `all_tag_aliases.json`), 2 danh sách loại bổ sung §4.2 (`school_uniform`, `petite`…), 3 ảnh / người thật §4.3, 4 safety gate của model (cần ảnh nên áp ở `label` và `evaluate`), 5 trùng md5 (pHash chưa làm vì không giữ ảnh), 6 post đã xoá / không có file. Module mà trang tìm kiếm không có tag (rule34.us) bị từ chối vì không kiểm được danh sách chặn.
   - Mỗi post có nhãn tầng 0 + độ tin cậy, slice §3.7 (site, phong cách, ca biên futa / trap / POV / đông người ≥ 6 / `muscular_female` / da tối, mật độ tag, loại media) và tầng: nhãn tầng 0 mức high, `sparse` ("thiếu tag": ≤ 8 tag general hoặc không có tag giới) hoặc `weak` (tag giới mức medium / low). `--quota` đặt số post mỗi tầng (`all` = lấy hết).
2. **Gán nhãn** (mỗi người một file, độc lập, không thấy tag hay nhãn tầng 0):
   ```bash
   .venv/bin/python -m lustra.gender_eval label --manifest eval/manifest.json --annotator an --open
   ```
   - Trang chỉ nghe trên `127.0.0.1` (mặc định cổng 8790), kiểm Host và token phiên. Phím: `1`/`F` chỉ nữ, `2`/`M` chỉ nam, `3`/`B` cả hai, `4`/`N` không có, `S`/Space bỏ qua, `T` đổi sample / thumbnail, `Z` hoàn tác, `X` không an toàn (loại).
   - Nhãn ghi vào `eval/labels-an.json` sau mỗi lần bấm. `X`: post bị xoá khỏi manifest ngay, tag của nó được in ra và lưu trong `dropped` (kèm danh sách tag mà blacklist chưa chặn, để bổ sung vào `lustra/safety.py`).
   - Trước khi hiện ảnh, công cụ gửi ảnh tới model server (`--model-url`, mặc định `LUSTRA_GENDER_MODEL_URL` hoặc `http://127.0.0.1:8765`) làm safety gate: tag cấm ≥ 0,15 thì post bị loại tự động, người gán không thấy ảnh. Server không chạy thì lệnh báo lỗi; `--no-model-gate` tắt bước này.
3. **Độ đồng thuận** và tập vàng:
   ```bash
   .venv/bin/python -m lustra.gender_eval agree eval/labels-an.json eval/labels-binh.json --manifest eval/manifest.json --out eval/agree.json
   .venv/bin/python -m lustra.gender_eval label --manifest eval/manifest.json --annotator trongtai --only eval/agree.json
   .venv/bin/python -m lustra.gender_eval agree eval/labels-an.json eval/labels-binh.json --arbiter eval/labels-trongtai.json --manifest eval/manifest.json --out eval/agree.json --gold eval/gold.json
   ```
   `agree` tính Cohen's κ trên các post cả hai người đều gán một trong 4 nhãn (bỏ qua `skip`, post bị loại), in danh sách bất đồng cho trọng tài và kiểm AC-G08 (κ ≥ 0,8; ≥ 3.000 post; ≥ 30 % thiếu tag). `--gold`: nhãn hai người cùng chọn + quyết định của trọng tài.
4. **Đánh giá**:
   ```bash
   .venv/bin/python -m lustra.gender_eval evaluate --manifest eval/manifest.json --gold eval/gold.json --out-dir eval/report
   .venv/bin/python -m lustra.gender_eval evaluate --manifest eval/manifest.json --silver --out-dir eval/report-silver
   ```
   - Có tập vàng: đo tầng 0, model (chỉ ảnh) và tầng 0 + model (`combine`) so với nhãn của người. `--silver` (chưa có nhãn của người): tham chiếu là nhãn tầng 0 mức high, model chỉ nhận ảnh; đây là độ đồng ý với tag mạnh, không phải precision thật.
   - Đầu vào `--inputs sample,thumb` (mặc định cả hai), điểm vận hành `--min-conf medium`. Ảnh cách nhau `--image-interval` giây (mặc định 1). Site trả bot challenge thì lệnh dừng và vẫn ghi báo cáo (đánh dấu chưa chạy hết); tag đoán ra được cache ở `<out-dir>/predictions.jsonl` nên chạy lại sẽ tiếp tục, không tải lại ảnh đã xem. `--max-minutes` giới hạn thời gian.
   - Ra `report.json` và `report.md`: precision theo lớp, độ phủ, macro-F1 4 lớp, ma trận nhầm lẫn 4×4 + cột unknown, ECE (15 bin) trên xác suất mà nhãn dựa vào, các điểm risk–coverage, mọi slice kèm n, thumbnail so với sample, cờ thiên lệch (slice n ≥ 150 có precision thấp hơn toàn cục > 5 điểm), số post trap bị gán only_female.
   - Post mà safety gate của model chặn (tag cấm ≥ 0,15 trên ảnh sample hoặc thumbnail) bị loại khỏi mọi số đo và bị xoá khỏi file manifest như ở `label`: `dropped` của manifest chỉ ghi khoá và tag của gate; các tag mà danh sách chặn chưa bắt được in ra và ghi trong `posts.modelGateDropped` của `report.json` để xem lại `lustra/safety.py`.

## 3h. Chặn nội dung gợi ý trẻ vị thành niên (`-safety_blacklist`)
- **Bộ lọc riêng, mặc định tắt**, độc lập với bộ lọc giới tính (quyết định 2026-11-18, thay quyết định 2026-11-16 khi nó tự chạy cùng `-gender`).
  - CLI: `-safety_blacklist`.
  - App: ô "Chặn nội dung gợi ý trẻ vị thành niên" trong Bộ lọc & tuỳ chọn tải. Lựa chọn được nhớ (`settings.download.safetyBlacklist`) và lưu theo từng truy vấn, nên "chạy lại" và tải theo lịch giữ nguyên.
- **Khi bật:**
  - bỏ mọi post có tag trong danh sách cố định của `lustra/safety.py`, cho mọi task kể cả post cha;
  - ở Preview, post bị chặn không hiện (kể cả thumbnail), chỉ được đếm; post không có tag để kiểm bị ẩn;
  - với `-gender_mode model`, chốt an toàn của model (tag gợi ý trẻ vị thành niên mà model nhận ra) cũng chặn post, và với `-gender_verify_media` thì chặn cả file đã tải.
- Extension chỉ thêm được tag (`safetyTags`); không đường nào bớt được tag của danh sách.
```bash
.venv/bin/python -m lustra -module db -safety_blacklist -gender only_female -path ~/Downloads/Lustra -- scenery
```

## 4. Test
```bash
.venv/bin/python -m unittest lustra.tests.test_lustra
.venv/bin/python -m unittest lustra.tests.tests.DataStructureIntegrityTests lustra.tests.tests.FileCheckTests lustra.tests.tests.ArgParseTests lustra.tests.tests.TagParseTests lustra.tests.tests.LoggerTests lustra.tests.tests.DownloaderBaseTests lustra.tests.tests.ItemFilterTests
.venv/bin/ruff check lustra extensions tools
cd app && npm run check
LUSTRA_USER_DATA=/tmp/lustra-ud LUSTRA_SELFTEST_DIR=/tmp/lustra-shots xvfb-run -a ./node_modules/.bin/electron . --no-sandbox --ozone-platform=x11
```
`ConnTests` và `RealDownloadTests` gọi mạng thật. `test_gender.test_real_model_on_the_app_icon` chỉ chạy khi có `LUSTRA_GENDER_MODEL_DIR` (thư mục đã tải model, mục 3c). `test_model_server` chạy server thật trên cổng ngẫu nhiên với model giả (và onnxruntime giả cho phần chọn GPU / CPU), không cần numpy / onnxruntime hay GPU. `test_media_verify` (`-gender_verify_media`, `/v1/frames`) cũng dùng model giả; phần đọc khung thật chỉ chạy khi có Pillow (ảnh động) và `ffmpeg` trên PATH (video), thiếu thì tự bỏ qua. `lustra.tests.test_extgen` chạy offline (gồm `DenylistTests`: các cách lách danh sách chặn tên miền; `SdkTests`: `lustra.sdk` và `minSdkVersion`); test nối SDK `anthropic` tự bỏ qua nếu chưa cài gói. `lustra.tests.test_dlindex_tools` chạy offline (site giả, thư mục tạm). `lustra.tests.test_catalog_trust`: test vector RFC 8032, chữ ký catalog (sửa 1 byte, khoá lạ, chưa ký, khoá giữ chỗ, hết hạn, biến môi trường cho nhà phát triển), cài từ catalog từ xa qua transport giả, `tools/sign_catalog.py` (và đối chiếu với `openssl` nếu máy có). `lustra.tests.test_aigen_eval`: harness eval ghi / phát lại fixture (cả qua server HTTP cục bộ với `requests` thật), chế độ template, model giả lập, chế độ AI với client theo kịch bản, trần chi phí, kiểm khoá bị lộ; không gọi API trả phí. `test_gender_eval` chạy offline (trang tìm kiếm giả, ảnh giả, model giả, trang gán nhãn thật trên cổng ngẫu nhiên). `lustra.tests.test_tag_sources` chạy offline: site giả trả danh sách tag của cả 7 engine qua transport giả; kiểm robots.txt (RFC 9309, ngoại lệ cho module đã duyệt), `Crawl-delay`, `Retry-After`, ngân sách request, bot challenge, redirect sang host khác, khoá bắt buộc; và luồng đầu-cuối: trình tạo làm extension Gelbooru → cài → `tags.update` → `tags.autocomplete` có tag của site. Cùng file: danh sách đi vào bản nháp và file .zip (và các trường hợp để app tải sau), danh sách đi kèm extension đã cài, và danh sách học từ post Preview / index.

Toàn bộ test offline như CI (`.github/workflows/run-tests.yml`), với thư mục dữ liệu tạm:
```bash
LUSTRA_DATA_DIR=$(mktemp -d) .venv/bin/python -W ignore -m unittest lustra.tests.test_lustra lustra.tests.test_gender lustra.tests.test_security lustra.tests.test_batch_b lustra.tests.test_batch_c lustra.tests.test_batch_c_tags lustra.tests.test_batch_d_core lustra.tests.test_batch_d_search lustra.tests.test_batch_d_app lustra.tests.test_extgen lustra.tests.test_dlindex lustra.tests.test_dlindex_tools lustra.tests.test_model_server lustra.tests.test_media_verify lustra.tests.test_catalog_trust lustra.tests.test_aigen_eval lustra.tests.test_gender_eval lustra.tests.test_tag_sources
```

## 5. Build
- CLI exe: `python lustra/release` (PyInstaller, đã kèm manifest builtin và hidden-import `download_rx`/`download_db`, icon `images/lustra.ico`: icon mặt nạ cam của app ở 16–256 px). Thư mục `extensions/` cần được đặt cạnh file exe để làm catalog.
- App desktop:
  1. Build lõi Python đóng băng: `python -m pip install -r requirements.txt 'pyinstaller>=6.21' 'anthropic>=2.9' && python tools/build_core.py` → `app/build/core/lustra-core/`, khoảng 40 MB (khoảng 51 MB khi kèm `anthropic`; bỏ gói này thì bản build chỉ có chế độ "Không dùng AI"). Mặc định không kèm danh sách tag; dùng `--tags all` hoặc `--tags rx,db` để kèm. Smoke test kiểm `server`, client model server (`gender.modelStatus`), `cli`, `index` (nhập thư viện chạy thử), `aigen`, danh sách chặn tên miền (phiên `https://www.youtube.com/` bị từ chối R7) và các file `lustra/safety_data`. Đo ngày 2026-11-17 (có `lustra.sdk`, `lustra.trust`, danh sách chặn): 51,4 MiB khi kèm `anthropic`. numpy, onnxruntime, Pillow luôn bị loại khỏi bản build kể cả khi môi trường build có (model chạy ở model server, mục 3c): đo được 51,4 MiB khi kèm `anthropic`; trước đó kèm onnxruntime thì thêm ~110 MB.
  2. Build app: `cd app && npm ci && npm run dist` (Linux AppImage; Windows nsis/zip; macOS dmg/zip), hoặc `npm run dist:dir` để chỉ tạo thư mục chưa nén.
  3. Model server chạy độc lập (tuỳ chọn, phát hành riêng với app): `python tools/build_model_server.py`, xem mục 3c.
- **Release** (`.github/workflows/release.yml`, cùng cách làm với Mirrix). Tên file: `<app>-<os>-<arch>_v<version>.<ext>`.
  - Push lên `staging`: build mọi nền tảng rồi đăng pre-release `v<version>-rc.<lượt chạy>`. Push lên `production`: đăng release `v<version>` (latest) bằng chính các file của bản rc mới nhất có `build-info.json` khớp cây commit (kiểm lại `SHA256SUMS.txt`, không build lại); không có bản rc khớp thì build lại tất cả. Pull request và chạy tay: chỉ build và tự kiểm Linux x64, không đăng gì. Push chỉ đổi `docs/`, `.github/`, `.forgejo/`, `.claude/`, `lustra/tests/`, `.gitignore`, `*.md` ở gốc sau khi `v<version>` đã ra thì không đăng gì.
  - App: Linux x64 / arm64 (`lustra-linux-x86_64_v….AppImage`, `lustra-linux-amd64_v….deb`), Windows x64 / arm64 (`lustra-setup-<arch>_v….exe`), macOS arm64 / Intel (`lustra-mac-<arch>_v….dmg`, `.zip`, chưa ký). Lõi Python là bản PyInstaller nên mỗi kiến trúc build trên máy của nó (`ubuntu-24.04-arm`, `windows-11-arm`, `macos-15-intel`), không build chéo. Bản Linux: chuẩn hoá quyền file (umask 0022) và kiểm quyền từng mục trong `.deb`.
  - Mỗi bản app được tự kiểm bằng self-test của app (`LUSTRA_SELFTEST_QUICK=1`, kết quả đọc bằng `tools/check_selftest.py`): bắt buộc đạt trên mọi nền tảng (Linux chạy dưới xvfb; giới hạn 10 phút bằng Python vì macOS không có lệnh `timeout`).
  - Model server chạy độc lập: Linux x64 / arm64, Windows x64, macOS arm64 (CPU), Windows x64 DirectML. Image Docker CPU (amd64 + arm64) lên `ghcr.io/<owner>/lustra-model-server:<version>-rc.<n>`; release đổi tag bản rc thành `<version>` và `latest`. Image GPU không được đăng (thư viện NVIDIA có điều khoản riêng): người dùng tự build.
  - Package `lustra-model-server` trên ghcr được tạo ở chế độ riêng tư lần đầu đăng: chủ tổ chức chuyển sang Public trong Package settings → Danger Zone → Change visibility (tổ chức phải cho phép package công khai); đã công khai từ `2.0.1`, các tag sau giữ nguyên chế độ đó. Kiểm tra từ máy không đăng nhập ghcr: `docker logout ghcr.io && docker pull ghcr.io/leiz-workspace/lustra-model-server:<version>`.
  - Mọi release có `SHA256SUMS.txt` và `build-info.json` (version, cây commit, commit, lượt chạy, image). Ghi chú phát hành lấy từ `docs/releases/v<version>.md` nếu có, nếu không thì GitHub tự tạo.
  - Đưa code đi: `develop` → `staging` (bản rc) → `production` (bản chính thức). Version nằm ở `app/package.json` và `pyproject.toml` (bước plan báo lỗi khi hai nơi khác nhau); version đã phát hành thì phải tăng mới đăng được bản rc mới.
  - App đã đóng gói dùng `<resources>/core/lustra-core`. Biến `LUSTRA_CORE` trỏ app dev sang một lõi đóng băng.
  - Self-test chạy được trên file đã đóng gói với cùng các biến `LUSTRA_SELFTEST_*`; thêm `LUSTRA_SELFTEST_QUICK=1` để bỏ các bước cần mạng. `LUSTRA_SELFTEST_ICONS=1` kiểm việc đồng bộ launcher khi đổi icon; nó **ghi vào `~/.local/share/applications`**, nên chỉ chạy kèm `HOME=<thư mục tạm>`.
  - CI: job `desktop` trong `.github/workflows/build.yml`.

## 6. Nhánh git, pull request và Forgejo
`production` (nhánh mặc định, phát hành) ← `staging` (bản rc) ← `develop` (tích hợp), cùng quy trình với các repo khác của `Leiz-Workspace` (hướng dẫn của nền tảng phân phối, repo `self-software-distribution-platform`, `docs/GUIDE.md`).

- **Mọi thay đổi đi qua pull request.** Thay đổi vào `develop`: nhánh riêng → PR vào `develop`. Đưa lên `staging` / `production`: đẩy đầu nhánh nguồn sang nhánh tạm rồi mở PR, merge bằng merge commit; không dùng chính `develop` hay `staging` làm nhánh nguồn của PR.
  ```bash
  git push origin origin/develop:refs/heads/release/promote-staging
  ```
  ```bash
  gh pr create --head release/promote-staging --base staging --title "Promote develop to staging: <nội dung>" --body "<thay đổi và kết quả CI>"
  ```
  ```bash
  gh pr merge <số PR> --merge --delete-branch
  ```
  Lặp lại với `release/promote-production` → `production`. Chủ repo quyết định merge vào `production`.
- **Ruleset "develop, staging, production: CI must pass"** trên GitHub: chỉ cập nhật ba nhánh qua PR, PR chỉ merge được khi check `Tests passed` (job gộp kết quả của `.github/workflows/run-tests.yml`) và `Package (linux)` (build và self-test bản Linux của `release.yml`) đạt; không xoá, không force-push. Workflow Tests chạy cho mọi PR, kể cả PR chỉ đổi tài liệu, để check bắt buộc luôn có kết quả.
- **Merge PR có file trong `.github/workflows/`:** GitHub không cho token OAuth thiếu quyền `workflow` merge những PR đó qua API; merge trong trang PR, hoặc thêm quyền: `gh auth refresh -h github.com -s workflow`.
- **Runner:** CI của Lustra chạy trên runner của GitHub (miễn phí cho repo công khai). Runner tự host `aevra-home` của tổ chức không được dùng cho repo công khai: PR từ fork sẽ chạy được code tuỳ ý trên Home Server.
- **Forgejo (Home Server):** webhook của GitHub đồng bộ mirror `mirror/Lustra` sau mỗi push. `.forgejo/workflows/ci.yml` chạy lint và test (Python 3.11, Debian) với mỗi push lên ba nhánh; `.forgejo/workflows/forgejo-release.yml` chạy với mỗi tag `v*` mà release trên GitHub tạo ra: build lại bản Linux x64 (`.deb`, `.AppImage`, model server chạy độc lập), self-test, ký `SHA256SUMS-linux` bằng minisign, đăng package generic `lustra/<version>` và kho Debian (`stable`, component `main` cho bản chính thức, `rc` cho bản rc), tạo Release trên Forgejo; image model server lên registry của Forgejo, quét Trivy, ký cosign kèm SBOM. Windows, macOS và Linux arm64 chỉ có trên GitHub. Khoá công khai nằm trong `keys/` (khoá bí mật chỉ có trong Vault và secret của mirror): kiểm file Linux bằng `minisign -Vm SHA256SUMS-linux -p keys/minisign.pub` rồi `sha256sum -c SHA256SUMS-linux`, kiểm image bằng `cosign verify --key keys/cosign.pub <image>`. Repo công khai này không ghi tên miền hay tài khoản nội bộ của Forgejo; chúng nằm trong tài liệu của nền tảng.
