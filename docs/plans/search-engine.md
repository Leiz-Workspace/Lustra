# Plan — Cải thiện search engine của Lustra

> 2026-11-16 · Trạng thái: Giai đoạn 1 **đã làm** trong 2.0 r1, Giai đoạn 2–3 là đề xuất.
> Lustra không có index riêng: "search engine" là **bộ biên dịch truy vấn + bộ lọc cục bộ** đặt trước search engine của từng website. Cải thiện vì vậy tập trung vào bốn hướng:
> - nhập truy vấn dễ hơn;
> - gửi lên server phần chọn lọc nhất của truy vấn;
> - lọc cục bộ phần còn lại;
> - dữ liệu tag luôn mới.

## 1. Hiện trạng sau 2.0 r1 (đã làm)

| Hạng mục | Chi tiết | Nơi trong code |
|---|---|---|
| Autocomplete đúng thứ tự | Sửa lỗi xếp theo loại tag; giờ xếp theo số post. Danh sách khoá được sắp xếp lại khi nạp (file gốc có vài cặp sai thứ tự làm tìm kiếm nhị phân bỏ sót) | `tagsdb.py` |
| Gợi ý ở dạng loại trừ | Gõ `-pre…` sẽ gợi ý `-tag`; Shift+Enter thêm dạng loại trừ | `server.m_tags_autocomplete`, `app.js` |
| Chọn tag A–Z | Chữ cái, `#`, tiền tố, loại tag, số post tối thiểu, sắp theo phổ biến hoặc A→Z, cuộn vô hạn | `TagsDB.browse`, `server.m_tags_browse`, `createAZ()` |
| Chọn phức hợp | Chip có màu theo loại (tag, meta, OR, nhóm loại trừ, phủ định); gộp OR, gộp nhóm loại trừ, đảo, tách, sửa | `app.js` (chips) |
| Giới hạn tag thông minh | Metatag miễn phí không bị tính; tag dương thừa thì gửi **tag hiếm nhất** lên server, phần còn lại lọc cục bộ; tag phủ định thừa thành nhóm loại trừ cục bộ | `task.py` (`FREE_META_PREFIXES`, `LOCAL_POSITIVE_OVERFLOW`, `_pick_local_required_tags`), `download_base._filter_items_missing_required_tags` |
| Lọc ngày phía server | Danbooru chuyển khoảng ngày thành `date:a..b` (không tốn lượt tag), bỏ bước chia đôi tìm trang theo ngày | `download_db._consume_custom_module_tags`, `_supports_native_date_filter` |
| Preview | Trang kết quả có thumbnail, đánh dấu post bị lọc cục bộ, chọn post để tải, lightbox thêm/loại trừ tag | `server.m_search_preview`, `app.js` |
| Dữ liệu tag mới | Updater cho rx, db, xb, bb, en; cron/systemd; cập nhật trong app; chọn file mới nhất cho mỗi module | `tags_updater.py`, `tools/` |
| Đếm nhanh | Danbooru dùng `counts/posts.json` (1 request) | `download_db._get_items_query_size_or_html` |

## 2. Giai đoạn 2 — đề xuất (ưu tiên cao → thấp)

1. **Lịch sử và truy vấn đã lưu**: lưu 50 truy vấn gần nhất theo module, cho phép ghim. Truy vấn đã lưu có thể đặt lịch tải lại định kỳ, chỉ tải post mới nhờ `id:>last_max_id`. Lõi đã có `-get_maxid`.
2. **Tag alias & implication**: dùng `all_tag_aliases.json` và API alias (Danbooru `tag_aliases.json`) để chuẩn hoá chip; cảnh báo khi tag đã bị đổi tên. Bộ lọc nhóm loại trừ cục bộ hiện **không** áp alias (giới hạn có từ Ruxx) — cần ánh xạ alias trước khi so khớp.
3. **Gợi ý tag liên quan**: sau khi Preview, đếm tần suất tag trong các post trả về và hiện "Tag hay đi kèm" (top 20) với nút + và −. Không tốn thêm request vì preview đã có tags.
4. **Fuzzy autocomplete**: khi không có kết quả theo tiền tố, thử khớp chuỗi con hoặc khoảng cách sửa ≤ 1 trên các tag phổ biến (top 200k), trigram index được dựng lazy.
5. **Đếm theo từng tag và gợi ý tách truy vấn**: hiện số post của từng chip (lấy từ TagsDB) để người dùng biết tag nào đang thu hẹp kết quả; cảnh báo khi vượt giới hạn độ sâu (Danbooru 1000 trang, Rule34 200k) và đề xuất thêm `id:` để chia đoạn.
6. **Phân trang Danbooru vượt 1000 trang**: dùng `page=b<id>` thay vì số trang (cần một hook phân trang theo con trỏ trong `Downloader._fetch_task_items`).
7. **Preview nhiều task**: với truy vấn có nhóm OR được tách task, hiện tab theo từng task hoặc gộp có khử trùng.

## 3. Giai đoạn 3 — dài hạn

- **Bộ lọc nội dung theo model** (xem [gender-filter-model.md](gender-filter-model.md)) cắm vào pipeline như một filter mới.
- **Index cục bộ các lần tải**: SQLite lưu `ItemInfo` (tags, score, md5) của các post đã tải để tìm lại offline, tránh tải trùng giữa các module (md5) và hỗ trợ tool re-tag.
- **Truy vấn đa site**: một truy vấn chạy trên nhiều module, khử trùng theo md5.

## 4. Rủi ro
- Tag list lớn (db ~0,77 triệu tag, rx ~0,61 triệu) tốn khoảng 150–250 MB RAM mỗi module khi nạp → cân nhắc chuyển sang SQLite FTS cho autocomplete và A–Z.
- Lọc cục bộ làm số "≈ kết quả" lớn hơn số file thực tải → UI đã ghi chú; cần ước lượng tỉ lệ lọc dựa trên preview.
- Gợi ý tag không được làm tăng số request tới website (tôn trọng rate limit).
