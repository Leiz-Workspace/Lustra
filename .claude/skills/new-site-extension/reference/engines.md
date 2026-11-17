# Engine → dấu hiệu → cách làm

`survey` tự chạy các probe theo thứ tự trong bảng (mỗi probe 1 request, đã qua robots.txt) và dừng ở dấu hiệu chắc chắn đầu tiên.

| Engine (`--engine`) | Dấu hiệu | Phân trang / đếm | Cách làm | Mẫu |
|---|---|---|---|---|
| `gelbooru_dapi` | `index.php?page=dapi&s=post&q=index&limit=1` trả XML `<posts count="N">` | `pid=n`; count ở thuộc tính `count` | **template**: lớp con của `DownloaderGelbooru`, chỉ đổi site / abbr / interval | `extensions/xbooru`, `lustra/download_gelbooru.py` |
| `danbooru_json` | `posts.json?limit=1` trả mảng có `tag_string` | `page=n+1`; giới hạn số tag cho khách | **template**: lớp con của `DownloaderDb` | `lustra/download_db.py` |
| `e621_json` | `posts.json?limit=1` trả `{"posts":[{"file":…,"tags":{…}}]}` | `page`; không có endpoint đếm rẻ | viết tay theo mẫu | `extensions/e621` |
| `moebooru_json` | `post.json?limit=1` trả mảng có `jpeg_url` / `sample_url` | `page=n+1`; đếm qua `post.xml?limit=0` | viết tay theo `download_db.py` | `lustra/download_db.py` |
| `philomena_json` | `api/v1/json/search/images?q=*&per_page=1` trả `{"images":[…],"total":N}` | `page`, `per_page`; tag nối bằng dấu phẩy | viết tay theo `download_db.py` | `lustra/download_db.py` |
| `szurubooru_json` | `api/posts/?limit=1` với `Accept: application/json` trả `{"results":[…],"total":N}` | `offset=n*limit` | viết tay theo `download_db.py` | `lustra/download_db.py` |
| `shimmie` | chân trang có "Shimmie", `/post/list/<tags>/<page>` | HTML theo trang, hoặc `/api/danbooru/find_posts` | viết tay | `extensions/paheal` (API), `extensions/rule34hentai` (HTML) |
| `custom` | không khớp mục nào | theo trang listing / docs API | viết tay | `extensions/rule34us` (HTML), `lustra/download_db.py` (JSON) |

## Hook cần chú ý khi viết tay (`lustra/download_base.py`)

- Dựng địa chỉ: `_form_tags_search_address(tags, maxlim)`, `_form_page_num_address(n)`, `_get_sitename()`, `_get_tags_concat_char()`, `_get_idval_equal_seaparator()`.
- Đếm và trang: `_get_items_query_size_or_html(url)` (trả số bài, hoặc trang HTML để lõi tự đếm), `_get_items_per_page()`, `_get_max_search_depth()`, `_is_search_overload_page(page)`.
- Item: `_get_all_post_tags(page)` (danh sách post của một trang), `_extract_id(h)`, `_is_video(h)`, `_get_image_address(h)` / `_get_video_address(h)` (trả `(url, ext)`), `_extract_post_date(h)` (`dd-mm-yyyy`), `_extract_item_info(item)` (`ItemInfo`, có `tags`).
- Với JSON: `_parses_raw_pages()` trả `True`; chuyển mỗi post thành chuỗi `<post_id="…" file_url="…" preview_url="…" created_at="…" tags="…">` (thay `"` trong giá trị bằng `'`), như `DownloaderDb._get_all_post_tags`.
- Tốc độ: `_get_min_request_interval()` trả một **hằng số** (bộ kiểm tra tĩnh đọc giá trị này).
- Không override: `fetch_html`, `wrap_request`, `download_file`, `_download`, `_respect_rate_limit`, `_parse_args`, các filter.
