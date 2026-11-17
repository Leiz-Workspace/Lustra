# Checklist review bản nháp

- [ ] robots.txt cho phép mọi đường dẫn extension dùng; `Crawl-delay` đã phản ánh trong interval.
- [ ] ToS / API terms không cấm truy cập tự động; URL ToS và tóm tắt có trong `specs.md` (hoặc ghi "không tìm thấy").
- [ ] `_get_min_request_interval()` là hằng số ≥ 0,5 s (API) / 1,0 s (HTML) và ≥ giới hạn công bố.
- [ ] Không vượt CAPTCHA / Cloudflare / bot-detection; không xoay User-Agent; manifest không khai báo `cf_clearance` hay cookie phiên.
- [ ] Không dựng URL file cho bài site ẩn / giới hạn; chỉ dùng URL site trả về.
- [ ] Không DRM, không HLS / DASH mã hóa; chỉ URL file trực tiếp (`links` đạt).
- [ ] Import tên của Lustra từ `lustra.sdk`; `minSdkVersion` của manifest còn nguyên; `check` không báo vi phạm `policy`.
- [ ] Site không nằm trong danh sách chặn tên miền (`check` không có mục `domain:…`).
- [ ] Hằng số / regex đặt cục bộ; không sửa `lustra/defines.py`, `lustra/rex.py`; regex không có lượng từ lồng nhau (ReDoS).
- [ ] Mỗi post có tag (`tags` đạt); `CAPABILITIES` chỉ khai báo tính năng có thật.
- [ ] `manifest.json` giữ `id`, `abbr`, `$generator`; `media` đúng với site; `auth.apiKey` chỉ khi site có API key chính thức.
- [ ] `record` đạt; `check` (offline) đạt với `replay` và `expected`; `python -m unittest lustra.tests.test_extgen` đạt.
- [ ] Fixture chỉ là text API / HTML, không media, không token (≤ 1 MB mỗi file, ≤ 6 MB tổng).
- [ ] Chưa cài, chưa chép vào `extensions/`, chưa commit (trừ khi người dùng yêu cầu rõ).
