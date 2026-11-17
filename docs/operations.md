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
- `LUSTRA_SECRET_ARGS`: danh sách JSON các tham số bí mật (`["-api_key", "KEY,USER"]`) do app truyền cho CLI; CLI đọc xong thì xoá khỏi môi trường;
- `LUSTRA_SELFTEST_SECURITY=1`: thêm kiểm tra bảo mật vào chế độ tự kiểm thử;
- `LUSTRA_SELFTEST_DIR`: chạy tự kiểm thử Electron. Bước `generator` kiểm: khoá chỉ ghi (không lộ ra renderer, không có dạng chữ thường), phiên bị từ chối (R10), không cài được bản nháp chưa đạt, cài cần xác nhận, dọn dẹp.

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

## 4. Test
```bash
.venv/bin/python -m unittest lustra.tests.test_lustra
.venv/bin/python -m unittest lustra.tests.tests.DataStructureIntegrityTests lustra.tests.tests.FileCheckTests lustra.tests.tests.ArgParseTests lustra.tests.tests.TagParseTests lustra.tests.tests.LoggerTests lustra.tests.tests.DownloaderBaseTests lustra.tests.tests.ItemFilterTests
.venv/bin/ruff check lustra extensions tools
cd app && npm run check
LUSTRA_USER_DATA=/tmp/lustra-ud LUSTRA_SELFTEST_DIR=/tmp/lustra-shots xvfb-run -a ./node_modules/.bin/electron . --no-sandbox --ozone-platform=x11
```
`ConnTests` và `RealDownloadTests` gọi mạng thật. `lustra.tests.test_extgen` chạy offline; test nối SDK `anthropic` tự bỏ qua nếu chưa cài gói.

## 5. Build
- CLI exe: `python lustra/release` (PyInstaller, đã kèm manifest builtin và hidden-import `download_rx`/`download_db`). Thư mục `extensions/` cần được đặt cạnh file exe để làm catalog.
- App desktop:
  1. Build lõi Python đóng băng: `python -m pip install -r requirements.txt 'pyinstaller>=6.21' 'anthropic>=2.9' && python tools/build_core.py` → `app/build/core/lustra-core/`, khoảng 40 MB (khoảng 51 MB khi kèm `anthropic`; bỏ gói này thì bản build chỉ có chế độ "Không dùng AI"). Mặc định không kèm danh sách tag; dùng `--tags all` hoặc `--tags rx,db` để kèm. Smoke test kiểm `server`, `cli` và `aigen`.
  2. Build app: `cd app && npm ci && npm run dist` (Linux AppImage; Windows nsis/zip; macOS dmg/zip), hoặc `npm run dist:dir` để chỉ tạo thư mục chưa nén.
  - App đã đóng gói dùng `<resources>/core/lustra-core`. Biến `LUSTRA_CORE` trỏ app dev sang một lõi đóng băng.
  - Self-test chạy được trên file đã đóng gói với cùng các biến `LUSTRA_SELFTEST_*`; thêm `LUSTRA_SELFTEST_QUICK=1` để bỏ các bước cần mạng. `LUSTRA_SELFTEST_ICONS=1` kiểm việc đồng bộ launcher khi đổi icon; nó **ghi vào `~/.local/share/applications`**, nên chỉ chạy kèm `HOME=<thư mục tạm>`.
  - CI: job `desktop` trong `.github/workflows/build.yml`.

## 6. Nhánh git
`production` (nhánh mặc định, phát hành) ← `staging` (kiểm thử) ← `develop` (phát triển). Làm tính năng trên `develop`, merge lên `staging` để kiểm thử, rồi lên `production` để phát hành. Khi đẩy lên GitHub, đặt default branch = `production`.
