# Quy tắc cứng (bản rút gọn của Phần C, docs/plans/site-extension-skill-and-ai-generator.md)

Mỗi rule được thực thi bằng code, không chỉ bằng lời dặn. Cột "Ở đâu" cho biết chỗ chặn.

| Mã | Quy tắc | Ở đâu |
|---|---|---|
| R1 | Chỉ API / trang công khai cho khách ẩn danh, hoặc API chính thức bằng key của chính người dùng (`auth.apiKey`). Không form đăng nhập, không cookie phiên / challenge (`cf_clearance`, `pass_hash`…), không tài khoản người khác | `SafeFetcher` (không cookie, chặn user:pass trong URL); `policy.py` cảnh báo chuỗi cookie đăng nhập |
| R2 | Tôn trọng robots.txt cho mọi đường dẫn; `Crawl-delay` thành interval. robots.txt 4xx thì coi như cho phép, 5xx thì coi như cấm (RFC 9309) | `SafeFetcher.fetch` |
| R3 | ToS / API terms cấm truy cập tự động thì từ chối. Không rõ thì hỏi người dùng, ghi URL + tóm tắt vào `specs.md` | bạn (người lái) |
| R4 | Interval ≥ max(giới hạn công bố, Crawl-delay, 0,5 s API / 1,0 s HTML); tôn trọng `Retry-After`; ngân sách request mỗi phiên | `policy.py` (hằng ≥ 0,5); lõi `_respect_rate_limit`; `SafeFetcher` (1 req/s, budget) |
| R5 | Không vượt CAPTCHA, bot-detection, Cloudflare challenge: không headless browser, solver, giả TLS fingerprint, chạy JS, xoay User-Agent | `SafeFetcher` dừng với R5 khi gặp `cf-mitigated: challenge`; `policy.py` cấm import |
| R6 | Không dựng URL file cho bài mà site ẩn / giới hạn (bị xóa, trả phí, blacklist mặc định); chỉ dùng URL site trả về | review |
| R7 | Không gỡ DRM, không site streaming có DRM, không HLS / DASH mã hóa; chỉ URL file trực tiếp | sandbox kiểm `links` (https + đuôi media đã biết) |
| R8 | Từ chối site có nội dung bất hợp pháp | bạn + người dùng |
| R9 | Bộ lọc an toàn của lõi luôn áp dụng; mỗi post phải mang tag; không gỡ / đổi tên tag | sandbox kiểm `tags` (≥ 80 % post có tag); lõi |
| R10 | Code chạy trong process riêng có giới hạn; chỉ import cho phép; không eval / exec / compile / open / `__import__` / getattr động / dunder; không override hook mạng / tải / filter của lõi; chỉ https, địa chỉ công khai, đúng host của site | `policy.py`; `sandbox_boot.py` (audit hook chặn socket, subprocess, ctypes, ghi file ngoài tmp; rlimit) |
| R11 | Không bao giờ tự cài. Bản nháp → người dùng xem lại → người dùng tự cài (zip + sha256, hoặc nút "Cài vào app…" có hộp thoại xác nhận) | CLI không có lệnh cài; app cài sau khi kiểm tra lại và hỏi xác nhận |
| R12 | Không rò bí mật: không gửi cookie / key khi khảo sát; fixture và report không chứa token | `SafeFetcher` không cookie; `redact_url` |
| R13 | Minh bạch nguồn gốc: manifest giữ `$generator` (tool, engine, nguồn, thời điểm, model nếu có) | `make_manifest`; AI không được đổi khóa này |

## Import được phép trong `downloader.py`

- `lustra.defines`, `download`, `download_base`, `download_gelbooru`, `download_db`, `logger`, `rex`, `utils`, `tags`, `vcs`.
- Từ `lustra.network` chỉ `HTMLContent`, `thread_exit`, `DownloadInterruptException`, `LUSTRA_USER_AGENT`.
- Từ `requests` chỉ `exceptions`; từ `multiprocessing.dummy` chỉ `current_process`.
- Stdlib: `re`, `json`, `datetime`, `time`, `math`, `html`, `base64`, `itertools`, `collections`, `typing`, `dataclasses`, `functools`, `enum`, `string`, `urllib.parse`.
- Thư viện ngoài: `bs4`.

`python -m lustra.extgen check` (hoặc `record`) báo mọi vi phạm kèm số dòng.
