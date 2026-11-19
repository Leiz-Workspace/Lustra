# Plan: tag list và job cập nhật tag của extension

> 2026-11-19 · Trạng thái: **đã chốt với người dùng; phần lõi và app đã làm** (nhánh `feat/ext-tag-jobs`, dev log 7at). Extension có job do các session tạo extension làm · Nhánh tham chiếu: `develop`
> Yêu cầu gốc: extension mang sẵn tag list và job cập nhật tag list của riêng nó. Lustra chỉ việc lấy danh sách đó, và chạy job khi người dùng bấm nút. Job không chạy nền liên tục, chỉ chạy khi được yêu cầu.

## Quyết định (người dùng chọn, 2026-11-19)

| Câu hỏi | Chọn | Không chọn |
|---|---|---|
| Job chạy ở đâu | **Trong app, trên máy người dùng**, khi bấm "Cập nhật" ở Tags A–Z, bằng khoá của chính họ | Job theo lịch trên server (Forgejo) ký và đăng danh sách, app chỉ tải về |
| Bộ dò tag theo engine của lõi (Gelbooru, Danbooru, Moebooru, e621, Philomena, Szurubooru, Shimmie) | **Giữ làm dự phòng** cho ext chưa có `tagUpdate`; ext đã cài không bị hỏng | Bỏ khỏi lõi, chỉ giữ rx / db |
| Cách tự chạy đang có: lịch hệ thống (crontab / Task Scheduler), tự cập nhật khi mở app, tải ngay sau khi cài | **Không bao giờ chạy job của ext**; các tuỳ chọn này giữ nguyên cho rx / db và ext cũ | Bỏ hẳn mọi cách tự chạy |

## Hợp đồng cho extension

`manifest.json`:
```json
{
  "tagList": "<abbr>_tags.json",
  "tagUpdate": {"entry": "tags.py:read_tags"},
  "minSdkVersion": "1.1"
}
```
- `tagList`: file danh sách nằm cạnh `manifest.json`, cùng định dạng các danh sách hiện có (`{"tag": "N posts (type)", ...}`, mỗi dòng một tag, đã sắp xếp). Lustra dùng file này ngay khi ext được cài.
- `tagUpdate.entry`: `<file>.py:<hàm>`. File phải nằm ở thư mục gốc của ext. Ngoài `entry` không có khoá nào khác.
- `minSdkVersion: "1.1"`: Lustra ≤ 2.0.7 không biết field `tagUpdate` và từ chối manifest có nó, nên ext cần khai báo SDK 1.1 để có thông báo rõ ràng.

`tags.py`:
```python
def read_tags(job):  # job: lustra.sdk.TagJob
    page = 0
    while rows := job.get_json(f'{job.site}api/tags?page={page}'):
        for r in rows:
            yield r['name'], r['count'], r.get('type', 'general')
        page += 1
```
- `job.site` (`https://host/` của manifest), `job.api_key` (khoá của người dùng, hoặc `''`), `job.min_posts`.
- `job.get_text(url, accept='')`, `job.get_json(url)`: chỉ dùng URL https của site đó hoặc tên miền con. Mọi request đi qua `PoliteClient` của lõi: nhịp tối thiểu 1 giây, `Crawl-delay`, robots.txt (áp chặt cho ext), Retry-After, giới hạn của khoá (`auth.rateLimit`), ngân sách request, không cookie, dừng khi gặp bot challenge.
- `job.log(text)`: một dòng tiến độ, hiện trong app.
- Mỗi tag là `(name, posts)` hoặc `(name, posts, type)`. Lõi đổi khoảng trắng trong tên thành `_`, bỏ dòng sai định dạng, đưa type lạ về `general`, lọc theo `min_posts`, rồi ghi `<data dir>/tags/<abbr>_tags.json`. Danh sách mới nhất thắng danh sách đi kèm ext. Ghi đè chỉ khi danh sách mới không ít hơn một nửa danh sách đang dùng.
- `tags.py` chịu cùng luật tĩnh R10 như `downloader.py`, chỉ khác là không cần lớp Downloader. Không được import `requests` (trừ `exceptions`), `urllib.request`, `socket`... Trình tạo extension kiểm cả file này và đóng gói nó vào zip.

## Ai chạy job, khi nào

| Đường chạy | Job của ext (`tagUpdate`) | Ext cũ / rx / db |
|---|---|---|
| Nút "Cập nhật" ở Tags A–Z | chạy | chạy |
| `lustra-core tags --modules <abbr>` (nêu tên module) | chạy | chạy |
| `lustra-core tags --modules auto` (lịch hệ thống của app) | **không** | chạy (module không cần khoá) |
| Tự cập nhật khi mở app (tuỳ chọn, mặc định tắt) | **không** | rx / db |
| Tải ngay sau khi cài ext | **không**: ext đã mang sẵn danh sách | chạy khi ext không mang danh sách |
| Trình tạo extension (extgen / aigen) với bản nháp | **không**: code của bản nháp chỉ chạy trong sandbox | bộ dò theo engine |

Danh sách đi kèm của ext trong catalog được tạo trên máy người bảo trì:
```bash
lustra-core tags --modules <abbr> --out <thư mục tạm>
```
(ext nằm trong `extensions/` của repo). Sau đó chép `<abbr>_tags.json` vào cạnh `manifest.json`.

## Phân công giữa các session

- **Lõi và app** (session "Repository overview and components", nhánh `feat/ext-tag-jobs`): `lustra/tags_updater.py` (`TagJob`, `EXTENSION_JOB`, `TagSource.on_demand`), `lustra/modspec.py` (`tagUpdate`), `lustra/sdk.py` (SDK 1.1), `lustra/extgen/policy.py`, `sandbox.py`, `taglist.py`, `app/main.js`, `app/renderer/`, schema manifest, tài liệu.
- **Extension và skill** (các session tạo ext): `tags.py`, manifest, danh sách đi kèm của từng ext; skill `new-site-extension`. Không sửa các file của lõi và app ở trên.
- Bộ đọc riêng cho một site (không phải engine) nằm trong `tags.py` của ext đó, không nằm trong lõi.
