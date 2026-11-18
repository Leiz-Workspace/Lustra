# Tag syntax

> Inherited from the original README (English). It describes the query language of the Lustra core: the desktop app's tag field, the command line (`python -m lustra … -- <tags>`) and the legacy Tk GUI all pass tags through it. Module abbreviations: `rx` Rule34, `db` Danbooru (built in); `rn` Rule34 Hentai, `rs` Rule34.us, `rp` Rule34 Paheal, `en` e621, `xb` Xbooru, `bb` Bbooru (extensions). The desktop app adds chips, OR / exclusion grouping and autocomplete on top (docs/specs.md F-01, F-05).

Lustra normally allows most symbols for tags search, there are some specifics though:  
1. Wildcards
- Most modules support asterisk symbol `*` as wildcard in tags (any number of any symbols). You can use any number of wildcards in tags in any place: `b*m*e_cit*` instead of `baltimore_city`.
  - Note that there is a bug in RX / XB / BB search engine which breaks frontal wildcards: `*_city` will work for RN, RS, RP and EN, but RX will return default result (all)
2. Meta tags
- Meta tags describe not the posted artwork but the post itself. RX, RN, RS, RP, EN, XB and BB all support meta tags:
  - RX syntax: _name_**:**_value_ OR _name_**:=**_value_
  - RN syntax: _name_**=**_value_
  - RS syntax: _name_**:**_value_
  - RP syntax: _name_**=**_value_
  - EN syntax: _name_**:**_value_
  - XB syntax: _name_**:**_value_ OR _name_**:=**_value_
  - BB syntax: _name_**:**_value_ OR _name_**:=**_value_
- Some meta `-tags` can be used for exclusion: `-rating:explicit`
- Some meta tags support wildcards. Rules are very strict so this feature is yet to be enabled
- Some meta tags support inequality. These metatags can be used to set a range, ex. `id:>X id:<Y`. See below for more syntax
  - Meta `-tags` cannot be used with inequality, like `-score:<0`. Flip the comparison instead: `score:>=0`
  - Meta `-tags` cannot be used with sort: `-sort:score`, this syntax won't cause an error but its behavior is undefined. Please use common sense
- Although 'sorting' meta tags are fully supported (`sort` and `order` for RX / RS / XB / BB and RN / RP respectively), you can only use them if they don't conflict with other parameters (ex. date filters)
- RX meta tags:
  - **id**: `id:X` (OR `id:=X`), `id:>X`, `id:<Y`, `id:>=X`, `id:<=Y`. `X`,`Y` = `<post ID>`
  - **score**: `score:X` (OR `score:=X`), `score:>X`, `score:<Y`, `score:>=X`, `score:<=Y`. `X`,`Y` = `<number>`
  - Rarely used ones:
    - parent: `parent:X` (OR `parent:=X`). `X` = `<post ID>`
    - width: `width:X` (OR `width:=X`), `width:>X`, `width:<Y`, `width:>=X`, `width:<=Y`. `X`,`Y` = `<number>`
    - height: `height:X` (OR `height:=X`), `height:>X`, `height:<Y`, `height:>=X`, `height:<=Y`. `X`,`Y` = `<number>`
    - user: `user:X`. `X` = `<uploader name>`
    - rating: `rating:X`. `X` = `<rating name>`, ex. `safe`, `questionable`, `explicit`.
    - md5: `md5:X`, `X` = `<MD5 hash>`
    - source:
    - updated:
    - sort: `sort:X[:Y]`. `X` = `<sort type>`, ex. `score`, `id` (default). `Y` = `<sort direction>` (optional), `asc` or `desc` (default)
- RN meta tags:
  - **id**: `id=X`, `id>X`, `id<Y`, `id>=X`, `id<=Y`. `X`,`Y` = `<post ID>`
  - **score**: `score=X`, `score>X`, `score<Y`, `score>=X`, `score<=Y`. `X`,`Y` = `<number>`
  - **favorited_by**: `favorited_by=X`. `X` = `<user name>`
  - Rarely used ones:
    - width: `width=X`, `width>X`, `width<Y`, `width>=X`, `width<=Y`. `X`,`Y` = `<number>`
    - height: `height=X`, `height>X`, `height<Y`, `height>=X`, `height<=Y`. `X`,`Y` = `<number>`
    - user: `user=X`. `X` = `<uploader name>`
    - rating: `rating:X`. `X` = `<rating letter>`, ex. `q`, `s`, etc.
    - order: `order=X`. `X` = `<sort type>`, `id_desc` or `score_desc`
- RS meta tags:
  - **id**: `id:X` (OR `id:=X`), `id:>X`, `id:<Y`, `id:>=X`, `id:<=Y`. `X`,`Y` = `<post ID>`
  - **score**: `score:X` (OR `score:=X`), `score:>X`, `score:<Y`, `score:>=X`, `score:<=Y`. `X`,`Y` = `<number>`
  - Rarely used ones:
    - width: `width:X` (OR `width:=X`), `width:>X`, `width:<Y`, `width:>=X`, `width:<=Y`. `X`,`Y` = `<number>`
    - height: `height:X` (OR `height:=X`), `height:>X`, `height:<Y`, `height:>=X`, `height:<=Y`. `X`,`Y` = `<number>`
    - user: `user:X`. `X` = `<uploader name>`
    - rating: `rating:X`. `X` = `<rating name>`, ex. `safe`, `questionable`, `explicit`.
    - sort: `sort:X[:Y]`. `X` = `<sort type>`, ex. `score`, `id` (default). `Y` = `<sort direction>` (optional), `asc` or `desc` (default)
- RP meta tags:
  - **id**: `id=X`, `id>X`, `id<Y`, `id>=X`, `id<=Y`. `X`,`Y` = `<post ID>`
  - **score**: `score=X`, `score>X`, `score<Y`, `score>=X`, `score<=Y`. `X`,`Y` = `<number>`
  - **favorited_by**: `favorited_by=X`. `X` = `<user name>`
  - Rarely used ones:
    - width: `width=X`, `width>X`, `width<Y`, `width>=X`, `width<=Y`. `X`,`Y` = `<number>`
    - height: `height=X`, `height>X`, `height<Y`, `height>=X`, `height<=Y`. `X`,`Y` = `<number>`
    - poster: `poster=X`. `X` = `<uploader name>`
    - order: `order=X`. `X` = `<sort type>`, `id_desc` or `score_desc`
- EN meta tags:
  - **id**: `id:X`, `id:>X`, `id:<Y`, `id:>=X`, `id:<=Y`. `X`,`Y` = `<post ID>`
  - **score**: `score:X`, `score:>X`, `score:<X`, `score:>=X`, `score:<=X`. `X`,`Y` = `<number>`.
  - **favorited_by**: `favorited_by:X`, `favoritedby:X` or `fav:X`. `X` = `<user name>`
  - Rarely used ones:
    - parent: `parent:X`. `X` = `<post ID>`
    - width: `width:X`, `width:>X`, `width:<Y`, `width:>=X`, `width:<=Y`. `X`,`Y` = `<number>`
    - height: `height:X`, `height:>X`, `height:<Y`, `height:>=X`, `height:<=Y`. `X`,`Y` = `<number>`
    - user: `user:X`. `X` = `<uploader name>`
    - rating: `rating:X`. `X` = `<rating name>`, ex. `safe`, `questionable`, `explicit`
    - md5: `md5:X`, `X` = `<MD5 hash>`
    - source:
    - updated:
    - order: `order:X[_asc|_desc]`. `X` = `<sort type>`, ex. `score`, `id` (default `id_desc`)
  - EN tags with range also support following range syntax:
    - `<type>:X..` (ex. `score:5000..` is equal to `score:>=5000`)
    - `<type>:..X` (ex. `score:..-500` <=> `score:<=-500`)
    - `<type>:X..` (ex. `id:5000000..` <=> `id:>=5000000`)
    - `<type>:X..Y` (ex. `score:90..99` <=> `score:>=90 score:<=99`)
- XB meta tags:
  - **id**: `id:X` (OR `id:=X`), `id:>X`, `id:<Y`, `id:>=X`, `id:<=Y`. `X`,`Y` = `<post ID>`
  - **score**: `score:X` (OR `score:=X`), `score:>X`, `score:<Y`, `score:>=X`, `score:<=Y`. `X`,`Y` = `<number>`
  - Rarely used ones:
    - parent: `parent:X` (OR `parent:=X`). `X` = `<post ID>`
    - width: `width:X` (OR `width:=X`), `width:>X`, `width:<Y`, `width:>=X`, `width:<=Y`. `X`,`Y` = `<number>`
    - height: `height:X` (OR `height:=X`), `height:>X`, `height:<Y`, `height:>=X`, `height:<=Y`. `X`,`Y` = `<number>`
    - user: `user:X`. `X` = `<uploader name>`
    - rating: `rating:X`. `X` = `<rating name>`, ex. `safe`, `questionable`, `explicit`.
    - md5: `md5:X`, `X` = `<MD5 hash>`
    - source:
    - updated:
    - sort: `sort:X[:Y]`. `X` = `<sort type>`, ex. `score`, `id` (default). `Y` = `<sort direction>` (optional), `asc` or `desc` (default)
- BB meta tags:
  - **id**: `id:X` (OR `id:=X`), `id:>X`, `id:<Y`, `id:>=X`, `id:<=Y`. `X`,`Y` = `<post ID>`
  - **score**: `score:X` (OR `score:=X`), `score:>X`, `score:<Y`, `score:>=X`, `score:<=Y`. `X`,`Y` = `<number>`
  - Rarely used ones:
    - parent: `parent:X` (OR `parent:=X`). `X` = `<post ID>`
    - width: `width:X` (OR `width:=X`), `width:>X`, `width:<Y`, `width:>=X`, `width:<=Y`. `X`,`Y` = `<number>`
    - height: `height:X` (OR `height:=X`), `height:>X`, `height:<Y`, `height:>=X`, `height:<=Y`. `X`,`Y` = `<number>`
    - user: `user:X`. `X` = `<uploader name>`
    - rating: `rating:X`. `X` = `<rating name>`, ex. `safe`, `questionable`, `explicit`.
    - md5: `md5:X`, `X` = `<MD5 hash>`
    - source:
    - updated:
    - sort: `sort:X[:Y]`. `X` = `<sort type>`, ex. `score`, `id` (default). `Y` = `<sort direction>` (optional), `asc` or `desc` (default)
3. `OR` groups
- Lustra syntax for `OR` group is simplified compared to what you would normally use for RX: `(tag1~tag2~...~tagN)` instead of `( tag1 ~ tag2 ~ ... ~ tagN )`
- Lustra allows using `OR` groups with any module, regardless of whether website supports it natively or not
- The syntax is also the same for all modules, don't use curly brackets for RS
- `OR` group can't be negative and needs to be unwrapped:
  - `-(tag1~tag2~tag3)` => `-tag1 -tag2 -tag3`
- Since using meta tags in `OR` groups `(id:=X~score:=Y)` is broken (RX), not always reliable (EN) or straight impossible (RS, RN, RP), Lustra will always unwrap such groups to process them properly
4. Negative groups
- Syntax: `-(tag1,tag2,...,tagN)`. Lustra allows to filter out tag combinations (posts where all tags in group are present), which you can't normally do using any of the supported website search engines. In addition to normal tag symbols, in negative group tags you can use wildcard symbols `?` and `*` for `any symbol` and `any number of any symbols` repectively. You can also use pipe symbol `|` for direct regex `OR` group composition. Example: `-(tag?1,ta*g2|tag3)` will be effectively converted to regular expressions `"^tag.1$"` and `"^ta.*g2|tag3$"` to check for, posts with tags matching both will get filtered out
    - Important note: unlike normal `-tags`, negative group will not check tag aliases
5. Tag limits
- Any valid search query requires at least one positive non-sorting tag to search for. Search query cannot be formed using just `sort:...` tag or `-tags` only
- A very long search query string will cause website to return empty result. Generally this happens when trying to add too many `-tags` to narrow down the search. If resulting query is too long Lustra will automatically create a specific negative group from excessive `-tags` and use them as additional filter (locally). The message will be given as follows: `<X> 'excluded tags combination' custom filter(s) parsed`
- Some websites also put a limit on the number of tags used. While most of the time this is a soft limit (web interface), sometimes they also apply a hard limit (api internals), namely:
  - `RP`: max `3` `tags & -tags`, `3` `total`
  - `EN`: max `40` `tags & -tags`, `40` `total`, max `1` `wildcard`
- In that case all extra `-tags` will be converted into a negative group and used locally as an internal filter (and mess up 'check tags' results). Note that this only applies to `-tags`, exceeding positive tag limit will result in an error
- It is recommended to manually convert all wildcarded `-t*ags` into a single negative group to prevent unwanted tag expansion (see below) resulting in too many `-tags`, it's simple really: `'-a -b -c -d* -f*g*h*j' -> '-a -b -c -(*,d*|f*g*h*j)'`
6. Tag parsing
- All tags you try to search for are first parsed and (optionally) converted to a proper search strings. This is exactly what allows custom tag types and other tags related features
- There are a few tag syntax features available:
  - surround tag with percent sign `%` to disable tag validation if Lustra falsely marks your tags as invalid:
    - `1+2=paradise` (invalid) `->` `%1+2=paradise%` (valid)

## Tag autocompletion
Lustra provides a list of known tags for each module, which can also be used to attempt to complete whatever word typed in **Tags** field
- Enable this feature by selecting **Tools -> Enable autocompletion**. You will be asked for a folder location - the folder containing tag list files. Once selected the following message will be logged (or similar):
  ```shell script
  Found 7 tag lists:
   - <full path to folder>/rx_tags.json
   - <full path to folder>/rn_tags.json
   - <full path to folder>/rs_tags.json
   - <full path to folder>/rp_tags.json
   - <full path to folder>/en_tags.json
   - <full path to folder>/xb_tags.json
   - <full path to folder>/bb_tags.json
  ```
  Notes:
  - This can also be a parent folder if tag lists folder is default-named (`2tags/` or just `tags/`)
  - If tag lists exist inside current directory, or default-named tag lists folder inside current directory, tag lists will be found automatically
- Once autocompletion is enabled a new hotkey will become available `<Ctrl+Space>`, the first time you use it Lustra will attempt to load current module tags into storage - this will require a little extra memory, that memory is also freed if autocompletion feature get disabled again
- If you prefer autocompletion to be enabled by default it is strongly recommended to save setting to *autoconfig* file

## User credentials
Lustra doesn't provide a method of authentication natively on either of supported sites. To use your identity during search you need to follow 3 simple steps:
- Log in normally using web browser
- Open `Web Developer tools -> Network` and reload the page, look for `request headers`
- Open `Headers / Cookies` window `<F3>` and fill Lustra connection tables accordingly:
  - Headers: `User-Agent` (remove existing value first)
  - Cookies:
    - RX: `cf_clearance`, `user_id`, `pass_hash`
    - RN: `cf_clearance`, `shm_user`, `shm_session`
    - RS: `user_id`, `pass_hash`
    - RP: ?? (sign ups disabled)
    - EN: `_danbooru_session`, `remember`
    - XB: `cf_clearance`, `user_id`, `pass_hash`
  - Notes:
    - RN `cf_clearance` cookie duration is **15 minutes**
- Desktop app: Settings → Kết nối → "Headers theo site (JSON)" / "Cookies theo site (JSON)", keyed by module abbreviation, for example `{"rn": {"User-Agent": "…"}}` and `{"rn": {"cf_clearance": "…"}}`. Only that website gets them; they replace shared entries of the same name. The shared "Headers (JSON)" / "Cookies (JSON)" go to every website: a browser `User-Agent` there would also replace the one e621 requires

## API authentication
Some modules (RX only actually) require authentication info provided in order to use their download API (dapi).  
It's strongly recommended to use your own API key and not rely on a default one. Visit module's respective website, sign in and generate yourself a key, you can then save it in config.

## Favorites
Downloading user's favorites using native tags search functionality is only available with RN, RP and EN (see meta tags above), other websites don't implement that neither through tags nor through API. In order to enable users to download one's favorites Lustra implements `favorited_by` tag for other modules as well
- Syntax: `favorited_by:X`. `X` = `<user ID>`. User ID you can get from user's favorites page, it's a part of its web address. Note: this syntax is not invalid as RN / RP / EN tag either but it won't do anything there
- Downloading from RX favorites pages requires `cf_clearance` cookie (see above) as it isn't a part of dapi
- XB / BB refuse `favorited_by`: favorites are html pages, and the terms of these sites allow automated access through the API only
- While searching favorites you can use normal filtering as well. Date filter, additional required / excluded tags, etc.
- Downloading favorites isn't particulary fast, Lustra will need to fetch info for every item in the list in order to enable filtering

## Pools
Downloading post pool using native tags search functionality is not possible and only RX and EN implement pool functionality (XB and BB refuse `pool`: pools are html pages, and their terms allow the API only).  
To download a pool use special `pool` tag:
- Syntax: `pool:X`. `X` = `<pool ID>`. Pool ID you can get from pool page, it's a part of its web address
- EN module also supports pool name syntax: `pool:Y`. `Y` = `<pool name>`. Pool name must be in lower case and have all spaces replaced with underscores, ex. `'Long Night' -> 'pool:long_night'`
- Downloading RX pool pages requires `cf_clearance` cookie (see above) as it isn't a part of dapi
- Pool posts can be filtered as well. Date filter, additional required / excluded tags, etc.
- Same as favorites, downloading using custom tags isn't particulary fast (RX), Lustra will need to fetch info for every item in the list in order to enable filtering

##### Sets
EN module also allows creating post sets. Essentially they are no different from pools:
- Syntax: `set:X`, `X` = `<set ID>`. Set ID can be extracted from set page address
- EN also supports set name syntax: `set:Y`. `Y` = `<set shortname>`. Important: set's short name isn't equal to its name in set list and is only listed on its own page!
- This is a native meta tag so:
  - posts can be filtered further if needed
  - download process suffers no speed penalty
