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
- `LUSTRA_IPC=1`: CLI chạy ở chế độ báo tiến độ;
- `LUSTRA_CATALOG_AUTOLOAD=0`: chỉ dùng extension builtin và đã cài (app đặt cho sidecar và job). Mặc định CLI và GUI Tk vẫn nạp extension trong catalog khi cần;
- `LUSTRA_CORE`: đường dẫn lõi đóng băng (`lustra-core`) cho app dev;
- `LUSTRA_STREAMING=0`: tắt pipeline streaming (quay lại kiểu quét hết rồi mới tải, dùng để so sánh / gỡ lỗi);
- `LUSTRA_DLINDEX=0`: không ghi file tải xong vào index các lần tải (mục 3b). Index vẫn đọc được, `-skip_known` vẫn chạy. Các test đặt sẵn biến này (`lustra/tests/__init__.py`);
- `LUSTRA_GENDER_MODEL_URL`, `LUSTRA_GENDER_MODEL_TOKEN`: địa chỉ và token model server cho CLI (mục 3c; app dùng Cài đặt). Model server đọc `LUSTRA_GENDER_MODEL_TOKEN` làm token của nó;
- `LUSTRA_GENDER_MODEL_DIR`: thư mục model (model server hoặc model trong tiến trình) thay cho `<data>/models/wd-vit-tagger-v3`. Đặt biến này khi chạy test thì có thêm test với model thật;
- `LUSTRA_GENDER_MODEL_DEVICE`: `auto` (mặc định: GPU nếu onnxruntime có provider GPU, nếu không thì CPU), `cpu`, `gpu` (không có GPU thì báo lỗi thay vì chạy CPU); model server có `--device` tương ứng;
- `LUSTRA_SECRET_ARGS`: danh sách JSON các tham số bí mật (`["-api_key", "KEY,USER"]`) do app truyền cho CLI; CLI đọc xong thì xoá khỏi môi trường;
- `LUSTRA_SECRET_MODULE_ARGS`: object JSON `{"db": ["-api_key", "KEY,LOGIN"]}`, khoá API theo module cho job nhập thư viện (`dlindex_tools import`); đọc xong thì xoá khỏi môi trường;
- `LUSTRA_SELFTEST_SECURITY=1`: thêm kiểm tra bảo mật vào chế độ tự kiểm thử;
- `LUSTRA_SELFTEST_DIR`: chạy tự kiểm thử Electron. Bước `generator` kiểm: khoá chỉ ghi (không lộ ra renderer, không có dạng chữ thường), phiên bị từ chối (R10), không cài được bản nháp chưa đạt, cài cần xác nhận, dọn dẹp.
  Bước `indexTools` kiểm: `index.clean` / `index.cleanCancel` không gọi trực tiếp được qua `rpc`, dọn cần xác nhận (từ chối thì không chạy, chạy thử thì không hỏi), nhập thư viện chỉ với thư mục chọn từ hộp thoại và module hợp lệ, một job nhập chạy thử thật (lõi `index import --dry-run`) và thẻ job của nó, nhóm tải nhiều site (2–8 module, ép `-skip_known` / `-prefix`, site sau chờ site trước, renderer không tự tạo nhóm), giao diện (Kiểm tra / Dọn index ở Cài đặt, hộp nhập thư viện, chọn site thêm, khung nhóm trong hàng đợi; ảnh `20-…` tới `24-…`).

Chế độ AI của trình tạo extension cần gói `anthropic`: `uv pip install --python .venv/bin/python 'anthropic>=2.9'`, hoặc `pip install "lustra[ai]"`. Khoá Claude API nhập trong app (Cài đặt → AI tạo extension). Khoá được mã hoá bằng keyring của hệ điều hành; không có keyring thì app không lưu. Chế độ "Không dùng AI" không cần gói hay khoá này. Bản nháp nằm ở `<data>/extgen/drafts/`.

Exit code của CLI: `0` thành công, `1` lỗi, `2` sai tham số, `3` một số file lỗi, `130` bị huỷ. Script hoặc cron có thể dựa vào mã này.

## 3. Cronjob cập nhật danh sách tag
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
- `LUSTRA_API_KEY_RX` (`KEY,USER_ID`);
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
- **Bản chạy độc lập** (cho người dùng app không có mã nguồn hay Python): `python tools/build_model_server.py [--variant cpu|directml]` tạo `dist-model-server/lustra-model-server/` và file nén `lustra-model-server-<phiên bản>-<os>-<arch>-<variant>.tar.gz` (`.zip` trên Windows). Script tự dựng môi trường build riêng (uv, hoặc venv + pip) với numpy, onnxruntime bản CPU (hoặc `onnxruntime-directml` trên Windows), Pillow, requests, yarl, PyInstaller, nên không phụ thuộc onnxruntime-gpu của `.venv`. Bản đóng gói không kèm model (lần đầu chạy tự tải), có `LICENSE`, `THIRD_PARTY_NOTICES.md` và script chạy nền của hệ điều hành build ra. Smoke test: `--help`; có model trên máy thì chạy server, nhận diện một ảnh trắng, giải phóng; không có model thì kiểm việc báo thiếu model. Đo trên Linux x86_64: 162 MiB giải nén, 47 MiB nén; model nạp trên CPU trong 0,7 s. CI: job `model-server` trong `.github/workflows/build.yml` (Linux, macOS, Windows CPU, Windows DirectML).
  Chạy nền mỗi khi đăng nhập:
  - Linux: giải nén vào `~/.local/share/lustra-model-server`, chép `lustra-model-server-bundle.service` (trong bản đóng gói, hoặc `tools/systemd/`) thành `~/.config/systemd/user/lustra-model-server.service`, rồi `systemctl --user daemon-reload && systemctl --user enable --now lustra-model-server`. Đã thử bản đóng gói dưới `systemd-run --user`.
  - macOS: `sh install-model-server.sh` trong thư mục bản đóng gói (LaunchAgent `local.lustra.model-server`, log `~/Library/Logs/lustra-model-server.log`); gỡ: `--uninstall`. Mới thử phần sinh plist trên Linux với `launchctl` giả; chưa chạy trên macOS.
  - Windows: `powershell -ExecutionPolicy Bypass -File install-model-server.ps1` trong thư mục bản đóng gói (Scheduled Task "Lustra model server" lúc đăng nhập, ẩn cửa sổ bằng `conhost.exe --headless`); gỡ: `-Uninstall`. Chưa chạy trên Windows.
- **Docker** (máy này hoặc máy chủ; lắng nghe ngoài loopback bắt buộc có token):
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

## 4. Test
```bash
.venv/bin/python -m unittest lustra.tests.test_lustra
.venv/bin/python -m unittest lustra.tests.tests.DataStructureIntegrityTests lustra.tests.tests.FileCheckTests lustra.tests.tests.ArgParseTests lustra.tests.tests.TagParseTests lustra.tests.tests.LoggerTests lustra.tests.tests.DownloaderBaseTests lustra.tests.tests.ItemFilterTests
.venv/bin/ruff check lustra extensions tools
cd app && npm run check
LUSTRA_USER_DATA=/tmp/lustra-ud LUSTRA_SELFTEST_DIR=/tmp/lustra-shots xvfb-run -a ./node_modules/.bin/electron . --no-sandbox --ozone-platform=x11
```
`ConnTests` và `RealDownloadTests` gọi mạng thật. `test_gender.test_real_model_on_the_app_icon` chỉ chạy khi có `LUSTRA_GENDER_MODEL_DIR` (thư mục đã tải model, mục 3c). `test_model_server` chạy server thật trên cổng ngẫu nhiên với model giả (và onnxruntime giả cho phần chọn GPU / CPU), không cần numpy / onnxruntime hay GPU. `test_media_verify` (`-gender_verify_media`, `/v1/frames`) cũng dùng model giả; phần đọc khung thật chỉ chạy khi có Pillow (ảnh động) và `ffmpeg` trên PATH (video), thiếu thì tự bỏ qua. `lustra.tests.test_extgen` chạy offline; test nối SDK `anthropic` tự bỏ qua nếu chưa cài gói. `lustra.tests.test_dlindex_tools` chạy offline (site giả, thư mục tạm).

Toàn bộ test offline như CI (`.github/workflows/run-tests.yml`), với thư mục dữ liệu tạm:
```bash
LUSTRA_DATA_DIR=$(mktemp -d) .venv/bin/python -W ignore -m unittest lustra.tests.test_lustra lustra.tests.test_gender lustra.tests.test_security lustra.tests.test_batch_b lustra.tests.test_batch_c lustra.tests.test_batch_c_tags lustra.tests.test_batch_d_core lustra.tests.test_batch_d_search lustra.tests.test_batch_d_app lustra.tests.test_extgen lustra.tests.test_dlindex lustra.tests.test_dlindex_tools lustra.tests.test_model_server lustra.tests.test_media_verify
```

## 5. Build
- CLI exe: `python lustra/release` (PyInstaller, đã kèm manifest builtin và hidden-import `download_rx`/`download_db`, icon `images/lustra.ico`: icon mặt nạ cam của app ở 16–256 px). Thư mục `extensions/` cần được đặt cạnh file exe để làm catalog.
- App desktop:
  1. Build lõi Python đóng băng: `python -m pip install -r requirements.txt 'pyinstaller>=6.21' 'anthropic>=2.9' && python tools/build_core.py` → `app/build/core/lustra-core/`, khoảng 40 MB (khoảng 51 MB khi kèm `anthropic`; bỏ gói này thì bản build chỉ có chế độ "Không dùng AI"). Mặc định không kèm danh sách tag; dùng `--tags all` hoặc `--tags rx,db` để kèm. Smoke test kiểm `server`, client model server (`gender.modelStatus`), `cli`, `index` (nhập thư viện chạy thử) và `aigen`. numpy, onnxruntime, Pillow luôn bị loại khỏi bản build kể cả khi môi trường build có (model chạy ở model server, mục 3c): đo được 51,4 MiB khi kèm `anthropic`; trước đó kèm onnxruntime thì thêm ~110 MB.
  2. Build app: `cd app && npm ci && npm run dist` (Linux AppImage; Windows nsis/zip; macOS dmg/zip), hoặc `npm run dist:dir` để chỉ tạo thư mục chưa nén.
  3. Model server chạy độc lập (tuỳ chọn, phát hành riêng với app): `python tools/build_model_server.py`, xem mục 3c.
  - App đã đóng gói dùng `<resources>/core/lustra-core`. Biến `LUSTRA_CORE` trỏ app dev sang một lõi đóng băng.
  - Self-test chạy được trên file đã đóng gói với cùng các biến `LUSTRA_SELFTEST_*`; thêm `LUSTRA_SELFTEST_QUICK=1` để bỏ các bước cần mạng. `LUSTRA_SELFTEST_ICONS=1` kiểm việc đồng bộ launcher khi đổi icon; nó **ghi vào `~/.local/share/applications`**, nên chỉ chạy kèm `HOME=<thư mục tạm>`.
  - CI: job `desktop` trong `.github/workflows/build.yml`.

## 6. Nhánh git
`production` (nhánh mặc định, phát hành) ← `staging` (kiểm thử) ← `develop` (phát triển). Làm tính năng trên `develop`, merge lên `staging` để kiểm thử, rồi lên `production` để phát hành. Khi đẩy lên GitHub, đặt default branch = `production`.
