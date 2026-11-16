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

Biến môi trường:
- `LUSTRA_PYTHON`: đường dẫn python cho app;
- `LUSTRA_DATA_DIR`: thư mục dữ liệu;
- `LUSTRA_USER_DATA`: userData của Electron;
- `LUSTRA_TAGS_DIR`: thư mục tag list ưu tiên;
- `LUSTRA_CATALOG_DIR`, `LUSTRA_CATALOG_URL`: catalog extension;
- `LUSTRA_IPC=1`: CLI chạy ở chế độ báo tiến độ;
- `LUSTRA_SECRET_ARGS`: danh sách JSON các tham số bí mật (`["-api_key", "KEY,USER"]`) do app truyền cho CLI; CLI đọc xong thì xoá khỏi môi trường;
- `LUSTRA_SELFTEST_SECURITY=1`: thêm kiểm tra bảo mật vào chế độ tự kiểm thử;
- `LUSTRA_SELFTEST_DIR`: chạy tự kiểm thử Electron.

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
`ConnTests` và `RealDownloadTests` gọi mạng thật.

## 5. Build
- CLI exe: `python lustra/release` (PyInstaller, đã kèm manifest builtin và hidden-import `download_rx`/`download_db`). Thư mục `extensions/` cần được đặt cạnh file exe để làm catalog.
- App desktop: chưa cấu hình electron-builder. Hướng đề xuất: đóng gói `app/` cùng sidecar build bằng PyInstaller (`lustra.server` + `lustra` CLI), rồi đặt `LUSTRA_PYTHON` trỏ tới binary đó.

## 6. Nhánh git
`main` (ổn định) ← `staging` (kiểm thử) ← `develop` (phát triển). Làm tính năng trên `develop`, merge lên `staging` để kiểm thử, rồi lên `main` để phát hành.
