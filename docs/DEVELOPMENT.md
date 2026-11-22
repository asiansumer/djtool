# DJTOOL 开发路线

更新：2026-11-22。产品需求依据 PRD；实际状态见 PROGRESS。

**0.12.0 起界面层在 `djtool/ui/`，结构与规矩见 CLAUDE.md 的“架构”一节；0.13.0 删除了旧界面和账号 / 本机直连 / 订阅 / Set 导出等遗留模块。本文件以下各节记录的是 0.11 及更早的设计取舍：关于窗口、对话框、主题、官方授权、本机直连和订阅的部分已经没有对应代码，只作历史参考；曲库、去重、下载校验和 Serato 写入的部分仍然有效。**

**0.13.1 起所有代码都在 `djtool/` 包里**（内核模块直接放在包下，界面在 `djtool/ui/`），根目录只剩薄入口 `main.py`；文档除 README / CLAUDE / AGENTS 外都在 `docs/`。选择“原名平移”而不是按领域再分子包：模块名带前缀（`serato_*`、`netease_*`）已经能看出归属，平移只需要机械地改 import，不改任何行为。

0.13.0 的两个取舍：

- 收歌页记住“来源”而不是一次次的链接。歌单的顺序存在 `link_sources.items_json`（指向 `online_jobs.id`），所以现状不用联网就能算出来；“只收前几首”从单独的 JSON 文件搬进了这张表。
- 收歌文件夹默认只提示、不自动导入。应用没有删除音频的功能，自动把整个下载目录收进曲库是收不回来的。

当前 0.11.10（构建号 21）在歌单首选音质无地址时按原曲 ID 尝试其他音质。收歌页继续支持批量加入 Set、自动加入 Serato 与按歌单保存下载范围。此前官方授权、会员设置、本机直连与订阅章节保留为历史兼容代码说明，当前界面不提供账号入口，默认不读取账号。

## 合作方式

用户已把实现工作交给助手。用户主要试用、反馈与决定产品感觉；核心原理在产品稳定后再集中梳理。此前“每轮留函数给用户写”的方式不再适用。

第一张浅色双栏设计是工作起点：左侧导航、中间曲库、右侧当前 Set、底部持续可用的试听区。用户明确允许随着功能增加调整布局，不将效果图当作不可变规格。

## 当前技术选择

- Python 3.13.12，项目独立 `.venv`。
- PySide6 Essentials / Addons 6.11.2：Qt Widgets 窗口与 Qt Multimedia 播放。
- SQLite：歌曲、分组、Set 与顺序；每次操作独立连接，后台导入不占用主界面的连接。
- Mutagen 1.48.1：读取真实 MP3、标签、封面和原标签 BPM。
- NCM 流式提取与 FFmpeg 必要转码已接入。BPM 按用户最新要求完全交给 Serato，不保留分析依赖。

暂时保留现有 Python 栈。先根据真实曲库、任务并发与打包表现确定是否需要替换某一层；没有证据时不以 Rust 的名字作为性能保证。

## 推进顺序

| 阶段 | 当前状态 | 接下来的完成条件 |
| --- | --- | --- |
| M0 环境 | 已完成 | 日常运行与验证使用相同环境 |
| M1 本地工作台 | 已扩展为可试用本地版 | 用户用真实 MP3 检查试听、整理、编排和重开保存 |
| M2 网易云实验 | 14 首详情已补齐；官方扫码、账号验证、钥匙串保存与新进程恢复已实测成功 | 一首完整 320 kbps、封面和歌词实测成功；扩大音源样本，并区分应用侧版权和账号权限 |
| M3 Serato 实验 | 尚未执行写入 | 隔离库中验证目标版本、顺序、重复更新、冲突检测及备份恢复 |
| M4 自动接收 | 本地目录接收、歌单订阅；14 首真实 NCM 转换并对回队列已验证 | 真实会员歌单远端新增自动下载贯通 |
| M5 分类与 Set | 本地部分已实现 | 实际选歌反馈、约 500 首规模、键盘/批量操作与真实同步贯通 |
| M6 SoundCloud 与体验 | 真实样例完整下载、标签/封面、来源与去重通过；已改石墨灰配色 | 增加失败样本、持续维护适配器、实际选歌反馈 |
| M7 Mac 交付 | 本机 Apple Silicon `.app` 构建；运行依赖随包 | 最终自检见 PROGRESS；干净 Mac 安装、公证、真实 Party 使用与恢复检查仍待做 |

官方下载器已用于手动队列和订阅任务；14 首接口无音源的歌曲已通过官方下载文件转换入库。接下来验证真实远端新增后自动入库与隔离 Serato 实验。账号、列表、音频和同步分别验收。

## 实现边界

导入保留源文件，复制到用户选定曲库。文件路径不随分类、改显示名称或 Set 顺序变化。内容摘要去重，多个集合引用同一首歌。每个 Set 的顺序与备注相互独立。

试听的解码器推进检查与实际听感验收分别记录。原 BPM 标签与历史数据库值保留，不再分析或显示算法估计。

SQLite 和音频文件不是同一个事务介质：常规异常清理本次新增文件并回滚记录；强制断电后的孤立临时文件检查与自动恢复仍是后续可靠性工作。

Serato 仅考虑关闭状态下的单向同步；正式写入前先验证隔离库、备份恢复和管理范围。不得为测试覆盖用户现有曲库或表演标记。

## 0.8 数据和发布路径

SQLite schema v4 增加 intake_sources、source_items、watched_files；升级前备份。来源记录、入库歌曲、Set 引用分别持久保存。队列保留等待接入与失败状态，订阅与手动下载均使用官方音频入口，平台明确不可用的任务不自动重试。

Mac 包使用 PyInstaller 6.16.0，FFmpeg 优先使用随包二进制。0.5 移除 `--bpm-worker` 和数值分析依赖；`--check-runtime` 检查合成音频编码/校验和 UI，不播放或联网。

0.8 不是最终 MVP：官方登录、可用音频和 14 首本地 NCM 转换已通过；当前优先验证真实歌单增量自动下载和 Serato 4 隔离更新/恢复。具体门槛见 MVP-ACCEPTANCE.md。

官方下载文件接收使用现有 schema v4：目录 `initial_scope=queue` 表示只匹配未入库且未忽略的网易云任务。扫描只读取有界 NCM 头部；导入稳定副本后再次核对 ID，并进行全段解码。`local_sources` 与队列在同一事务关联，核对歌曲 ID、时长和播放文件存在；本地先导入、歌单后读取也能匹配。普通 MP3 不按歌名匹配。升级时必须结束旧版进程后再启用此接收范围，旧版不识别 `queue` 会按全目录处理。

## 官方授权的数据流

`OnlineDialog` 打开 `OpenAPILoginDialog`，后台 `AuthWorker` 调用 `OpenAPIClient`：从 macOS 钥匙串读取开发者凭据 → RSA_SHA256 签名申请官方匿名令牌 → 生成授权二维码 → 轮询手机确认 → 取得用户令牌 → 官方账号接口验证 → 保存本机授权。匿名令牌仅在内存，不标记为登录。

请求协议依据网易发布的 `@music163/ncm-cli@0.1.7`；原生 Python 实现不依赖 Node 或 CLI 运行时，不执行 CLI 的日志逻辑。请求仅允许固定官方 OpenAPI 域名及列明的授权路径，不跟随重定向。异常只显示有限错误信息，避免原始 URL、令牌或接口响应进入日志。

窗口关闭会停止轮询并等待当前后台请求结束，忽略迟到结果；配置与登录状态分别显示。`main.py --netease-login` 可仅打开授权窗口，不加载曲库。`--check-runtime` 用合成密钥和账号状态验证打包后的 RSA、二维码、授权窗口，不能替代真实登录验收。

## 官方音乐适配器

`netease_music.py` 负责在应用范围内将原始 ID 映射为官方资源 ID。歌单从账号已创建/收藏的列表中按 `originalId` 匹配；分批读取并核对完整数量，完成后才更新订阅基线。旧任务使用标题作搜索提示，但必须精确匹配原始 ID，绝不按歌名替换版本。单曲链接的公开详情仅作搜索提示，不向旧接口发送账号 Cookie。

下载前重新核对音源 originalId、可用权限及实际试听区间。`freeTrailFlag` 是“支持试听”的能力标记，不代表当前已付费音源是试听；实际片段信息为 `freeTrail`。临时播放 URL 不持久化。CDN 文件大小/MD5、完整解码和时长检查通过后才入库。封面按 JPEG/PNG 实际头部识别，兼容 CDN 的非标准 Content-Type。

`unavailable` 状态单独显示平台不可用原因，不纳入自动重试，也不默认勾选。重读歌单可刷新未完成任务的资源 ID 和可用状态，已入库记录和手动整理不覆盖。

## 0.9 本机开源网易云入口

固定上游 Netease_url 提交 da2c3d4cfc11eef982be360bbb89426b30a1fd71；`netease_local.py` 原生适配协议，使用现有 Requests / PyCryptodome，不启动 Flask 或另一套曲库。公开列表先匿名读取，账号相关列表最多再试一次；音源向网易固定接口发送用户主动配置的 MUSIC_U，会话存 macOS 钥匙串。拒绝接口跳转携带账号、错误 ID、重复 / 不完整列表、试听与无效时长。临时媒体地址不持久化。

`netease_backend.py` 保存曲库独立的非敏感入口 / 音质偏好，默认本机解析 / exhigh。手动解析、批量下载和订阅共享工厂及同一个客户端。队列身份保持 provider=netease + 原始歌曲 ID；首次升级、入口切换或更新会话，仅解除未完成的 waiting_auth / unavailable，保留已完成、已忽略、失败、Set 与文件关系。偏好原子替换，队列事务失败恢复旧偏好；损坏偏好可在设置窗口修复。

下载沿用暂存目录、大小 / MD5、MP3 实际时长、FFmpeg 全段解码、封面和歌词标签、内容去重与文件异常清理。媒体重定向逐次校验官方 CDN。原始 FLAC / Hi-Res 转成 MP3，源音质与最终文件参数分开。官方入口依然使用原固定 320 kbps 请求。

`netease_settings_ui.py` 支持私密 Cookie 输入、后台账号验证、成功后保存、移除会话和原官方扫码授权。关闭 / Escape 等待活线程结束，关闭期间不保存新输入；官方与本机成功连接均可解除相应等待队列。测试不读取用户真实会话，不以模拟或合成文件代替会员实测。

2026-10-04 本机公开 DJ 歌单 14 首完整读取成功；匿名样例没有取得完整音源。后续需用户在窗口配置普通会员会话，再验证真实完整音频、真实远端新增自动入库和长期使用。


## 0.10 文件与列表边界

- storage.py 保存未来音频目录；Library.root/SQLite 不搬迁，audio_dir 可外置。连接失败回滚配置，本次新建目录仅在为空时清理；拒绝 Serato 内部、备份和用户软链接路径。
- Library.reference_existing 在选定外层目录内核对真实文件、摘要与稳定状态，只登记原路径和可读标签/封面；read_audio 支持 WAV/FLAC/AIFF/M4A，refresh_metadata 保留人工整理。ReferenceWorker 可停止，扫描跳过 Serato/隐藏/软链接目录。
- OnlineDialog 的 10 项 current_ids 顺序与歌单一致，选择集合跨页保持；状态和操作按实际任务显示。封面 QtNetwork 只取官方 CDN，限制请求时限/大小/跳转与生命周期，不读取账号。
- serato.py 仅写 manifest 持有的 DJTOOL Crate；序列化 UTF-16BE 有序路径，核对后原子替换、留存事务备份，外部修改/同名冲突停止写入。Serato 正在运行则拒绝。
- 原生支持绑定 4.0.10/二进制摘要，activate_native_sync 要求五阶段原生独立快照；SQLite 只用于验证，运行同步不直接写 Serato SQLite。Set UI 不再导出独立 Set 文件夹。
- 当前发布自检硬拒绝网络、钥匙串读取、真实音频加载/播放与真实 Serato 写入；截图覆盖收歌、保存目录、同步预览。旧账号 UI 模块只保留兼容代码，不能算当前用户流程。

## 0.11 站点协议与全歌单队列

netease_site.py 使用固定 nextmusic.toubiec.cn 公开 JSON 接口，IP 与返回媒体 URL 仅留内存，拒绝 API 重定向，丢弃响应 cookie。每个客户端一次 IP 检测；列表 limit=500、offset 递增，必须 songCount 完整一致且无重复，元数据白名单不会记录原始响应。audio 核对原 ID/时长/试听、仅返回官方 CDN、size/md5/br/level 与清理后的元数据，沿用原下载事务。site 不实例化账号存储；偏好版本 3 保留质量且一次迁移旧任务。

OnlineDialog 的整歌单队列与 PAGE_SIZE=10 显示分离。任务按原顺序逐首执行，既有完成/忽略不重下，批次统计覆盖全量。429 停止批次；可选歌词降级后 client.rate_limit 仍阻断后续调用。等待期限保存在 library 内存，重开窗口不会立即重试，不切换 IP/代理。验证区分离线 1001 首分页/833 首整队列与真实 14 首解析/实际媒体下载。

## 0.11.2 外观主题

0.11.2 外观由 theme.py 管理。浅色 CSS 作为原始模板保存在控件 property，按完整颜色映射生成暗色；apply_theme 更新 Qt QPalette/QStyleHints、已有控件、图标缓存和自绘视图。MainWindow.set_theme 只写 appearance_theme 偏好及刷新封面/切换按钮，不重建列表或改变播放器 source/position。收歌 CollectionDelegate 与曲库 TrackDelegate 绘制时读取当前主题。复选框和蓝色文字在暗色下独立提升对比；scripts/qa_theme.py 使用隔离库，禁止网络、账号、播放和 Serato 写入。

## 0.11.1 试听交互

audio_output.py 独立管理 QAudioOutput 路由与 QMediaDevices 热插拔；Qt 设备 ID 以十六进制存于应用 QSettings，默认模式保存空 ID，拔出回退不覆盖用户选择。菜单操作前重新核对设备是否仍连接，输出变更不重新加载歌曲。首次点击前预填菜单，点击时先刷新再 popup，不在原生 aboutToShow 中清空选项。ui.SeekSlider 的用户点击信号连接 QMediaPlayer.setPosition，程序 setValue 不触发 seek；拖动仍在 sliderReleased 提交，防止进度回传循环。scripts/qa_player.py 单独验收合成静音和实际设备 API，runtime 自检继续禁止播放，只检查菜单与组件。旧接收任务默认暂停，启动与切换曲库保留记录但不自动启动任务；明确调用 request_source_check 才恢复兼容检查。

## 0.11.3 歌单下载前去重

Library.enqueue_many 单事务保留输入首次出现顺序；按 provider+remote_id 分块查询和核对。单首 enqueue 沿用该方法且只核对当前 ID，避免千首批次逐首扫描整队列。增量 platform_tracks 保存成功下载的 ID→track 关系，旧完成任务回填；NCM ID、转换时长与实际时长核对，DJTOOL 专用来源文本帧须精确合法且有匹配时长。只核对既有已验收播放记录的文件存在/非空，不为整歌单重新解码；未知来源旧文件不按标题或艺人匹配。

complete 必须有播放记录与存在文件；无关联的错误完成记录恢复 ready，关联文件缺失转 missing_file 并保留原 track_id、路径和成员。原文件恢复后重新核对可完成，缺失文件在 UI/下载入口均阻止自动重下。下载前再次核对，复用不修改 reviewed、分类或 Set；新插入默认 reviewed=0。OnlineDialog.jobs 始终保存当前链接全部任务，visible_jobs 仅显示筛选，分页/筛选不缩短下载范围。inventory 持续显示总数/已有/待下载，默认只看待下载，可切换全部。

tests/test_playlist_dedup.py、tests/test_playlist_dedup_ui.py 与 tests/test_netease_download.py 分别验证数据规划、UI 和合成音频下载闭环。scripts/qa_playlist_dedup.py 明确使用 UI 文件存在性夹具，1000 首中 600 已有、400 待下的截图不作为真实在线下载或音频解码证明。runtime 自检增加去重与筛选检查，继续硬拒绝网络、账号、播放与真实 Serato 写入。

## 0.11.4 在线时长字段兼容

歌单<个人歌单 A>真实返回9条末位秒数60。_duration只允许末位60自然进位，中间分钟60、末位61/99和非正/非有限/超24h仍拒绝。_audio_time_ms严格解析站点time的YYYY/MM/DD HH:MM:SS响应生成时间，合法时间不当作音频时长；数字毫秒保留额外核对，其他未知格式拒绝。歌曲duration仍下载前重新获取，媒体大小、MD5、实际解码/时长与试听检测独立生效。release_date不再从time推断，站点新详情空日期也清掉待下载旧元数据的误填日期；local/official合并行为保持。

scripts/qa_netease_site_live.py用enqueue_many建立完整隔离队列，--song-id可定点验证歌单成员；成功/失败报告不保存账号、服务cookie、IP或短期媒体URL。原始故障字段与全1315列表的脱敏副本在work/netease-site-fix/，真实媒体验收在work/netease-site/playlist-<个人歌单 A>-exhigh/result.json。


## 0.11.5 冻结下载验收与整批故障保护

network_errors.py 不读取或显示原 Requests 异常文本（可能含短期签名地址/代理配置），只提取阶段、白名单异常类别、数字 errno/HTTP 状态。站点 API 与官方 CDN 传输在边界包装 NetworkFailure；可选歌词/封面网络失败只影响元数据，不拒绝已完整校验的音频。DownloadWorker 对共同 TLS/代理/权限/配置故障立即停止，对连续三首连接/超时停止，单曲 HTTP 故障可继续。

网站明确提供不超过300秒的短冷却时，当前任务等待至 retry_after+1秒后重试，等待期间可取消并按秒显示倒计时；同曲最多两次自动冷却。仅清除客户端已到期的 rate_limit，保留同一出口 IP；限流本身不算单曲失败。若已入库但可选歌词触发限流，在下一首之前等候，不重新下载成功音频。未知/长期或重复限流仍结束本批保留剩余队列。

download_check.py 是显式的冻结联网验收入口，最多10个歌曲 ID，目录必须在项目 work 内，使用实际后台 DownloadWorker 与正式校验/入库路径，拒绝读取钥匙串。原生窗口正式库验收独立记录，--check-runtime 的硬拒绝联网行为保持，不能拿其成功替代真实下载。

## 0.11.6 每个歌单独立的下载范围

OnlineDialog 保留完整歌单的 jobs 与原始顺序，范围为 jobs[:N]；0 表示整个歌单。范围按原序截断后再核对已有歌曲，因此前 N 首包含已下载条目，不会补入第 N+1 首以后来凑足 N 次下载。显示筛选、每页10首与下载范围分别控制；范围内才可进入整批、勾选或单曲下载，范围外按钮显示当前限制。后台批次运行时禁用范围修改，必须先停止当前批次。

解析结果附带 `netease:playlist:<ID>` 键；OnlineDialog 兼容原来的二元返回与新三元返回，未识别歌单不写范围设置。`PlaylistRanges(library.root).get(key)` 默认返回 0，`.set(key, limit)` 将独立范围写到 `.djtool/playlist-ranges.json`，格式仅为 version=1 和 limits 映射。键只接受最多19位的正数网易云歌单 ID，范围只接受非 bool 的 0–1000000 整数；读取校验整个配置，损坏时阻止继续应用，绝不默认为全部并悄悄覆盖。保存使用同目录临时文件、flush/fsync 与原子 replace，写失败保留旧配置；不创建账号记录、来源订阅或全局忽略任务。

范围外只是本歌单当前不下载，不能用 library.ignore 或写 state=ignored 实现；相同歌曲出现在其他歌单中，仍依其精确平台 ID 和实际播放文件核对。已有可用歌曲直接跳过且不改变 reviewed、成员关系或备注；成功下载的 ID→track 关联独立于某个歌单和当前收歌队列，跨歌单、重读、重启均复用。12项隔离范围持久化测试覆盖默认只读、歌单隔离、重新打开、边界校验、损坏配置、失败替换和文件无副作用；冻结与原生下载证据见 PROGRESS 的后续独立验收。

历史视图 current_ids=None 时无有效下载范围，只能看记录与试听已有歌曲；选择、单首和批量入口都拒绝下载。用户重新读取具体链接后才能按该歌单保存的范围续跑，避免历史入口把范围恢复为全部。

## 0.11.7 Serato BPM 只读来源

2026-10-04 用户明确新增读取 Serato 已有 BPM 并用于排歌查看，范围只含手动同步与显示。曲库按钮与设置菜单的「读取 Serato BPM」启动后台 SeratoBPMWorker，可停止；主曲库、Set 和详情使用同一来源，缺值「—」，不恢复计算、筛选或人工校正入口。

serato_bpm.py 默认读取本机 `~/Library/Application Support/Serato/Library/root.sqlite`，仅接受已研究的 Serato 4.0/schema 202 及完整必需字段。生产库从未传给 SQLite：以 rb 读取数据库和 WAL，核对复制前后文件签名，SQLite 只打开私有临时副本并执行 query_only、只读事务和 quick_check。源库正在变化、热回滚日志、未知版本、超限、读取异常和取消都不返回可应用的部分映射。

根卷 portable_id 按验证过的卷根规则转换为绝对路径，仅处理 `type=audio`、`third_party_type=0`，排除 missing/corrupt/stale；BPM 必须有限且在 20–400 内。路径只作 Unicode 等价规范化，不按大小写、歌名或艺人模糊匹配；实际播放文件必须存在、大小等于 Serato file_size，读取结束后再核对状态。同路径不同 BPM 冲突跳过；外置卷与流媒体不套用根卷路径规则。

Library 的 schema 4 增量建立 serato_bpm_cache（track_id 外键级联删除、bpm、source_path、synced_at），不增加或覆盖 tracks 的 BPM 字段。tracks()/track() LEFT JOIN 输出 bpm_serato、bpm_serato_source、bpm_serato_synced_at。replace_serato_bpms(values, source_path, cancel) 在事务前验证整份映射，缓存侧只接受精确绝对播放路径；一次成功完整读取会更新值并清除映射中已不存在的旧缓存。无效输入、SQL 错误或提交前取消整批回滚。返回 matched/changed/cleared/missing，changed 不把仅更新时间计为 BPM 改变；线程成功后刷新显示，不改播放源、分组、Set 顺序或备注。

本机只读研究有 700 首路径匹配，195 首 Serato 数值与既有标准 BPM 标签不同，包含 79.68 与 80 的精度差异；显示来源因此明确为 Serato，不能宣称每首都是新分析。实际 reader 接受 698 首，另 2 首 corrupt 标志不为 0 而跳过，源库前后 SHA256 不变。8 项缓存隔离测试和原曲库 7 项测试通过；冻结包与原生窗口验收由 PROGRESS 后续记录单独给出，源码或合成数据不替代实际源库读取证据。


## 0.11.8：收歌自动引用

serato_intake.py 从已提交的 tracks 构建独立的 djtool-auto-intake / 收歌 引用计划，不创建 DJTOOL Set、不改 reviewed，按路径去重，不按标题合并 remix。无效或缺失文件计数后跳过；其他有效播放文件继续。待同步由持久曲库推导，.djtool/serato-intake.json 只记录发布摘要、数量和原生接收状态；写入失败不把歌曲从曲库移除。

MainWindow 的独立 SeratoIntakeWorker 每 15 秒检查，并在本地导入、收歌批次完成后检查，可与已提交的下载并行（SQLite 独立连接）；只有一条同步线程，切换曲库和退出时等待其结束。显式 --library 的隔离会话不自动启动真实 Serato 同步。界面底部保留同步状态；准备好的 Crate 仅表示待下次 Serato 启动接收，root.sqlite/WAL 只读副本核对后才显示已导入。

复用 SeratoCrateAdapter 的专属清单、同名/外部修改拦截、同步锁、备份和失败恢复。增加 Serato 5.0.0/schema 203 的五阶段证据绑定；4.0.10/schema 202 记录继续兼容，版本与可执行文件摘要变化仍拒绝新写入。能力启用仅写 DJTOOL 配置，无需关闭 Serato；实际 Crate 写入始终要求主程序关闭。进程检测按可执行文件名判断，避免崩溃报告/WebEngine 辅助进程父目录含产品名而误判。BPM 读取的版本边界没有在本次修改中放宽。


## 0.11.9：批量 Set 成员事务与选择

Library.add_members(collection_id,track_ids,index=None) 在一次 BEGIN IMMEDIATE 中检查集合和完整 ID 列表，按输入首现顺序去重、跳过已有成员，再执行整批插入。普通追加不更新旧成员行；指定拖入位置时重新编号但保留旧成员相对顺序和备注。参数分400条核对，适配千首输入；缺失 ID、集合被删除或中途 SQL 失败时整批回滚。

主 SongList 使用 ExtendedSelection，Set 列表保持单选与原有拖动排序。populate 保留仍可见的选择，过滤掉已隐藏的选择；选中 ID 按视觉行序取出，不按点击顺序。可见工具条提供全选/取消/计数/加入 Set，右键和多选拖动走同一批量方法。刷新不调用播放器加载、播放或跳转。

OnlineDialog 的 add_to_set_requested 只发出当前 scoped_playlist_jobs 中 complete、有关联 track_id 且文件存在的去重 ID；不依赖当前页或仅待下载筛选。范围行内增加按钮，不增加行高，下载/解析运行时禁用；历史页没有歌单上下文时不批量加入全部历史。MainWindow 将这些 ID 交给相同的事务入口，并在收歌页反馈新增/已有数量。

## 0.11.10 歌单音质回退

站点有时为合法歌曲 ID 返回空 URL，同曲不同音质的可用性也会变化。NeteaseSiteClient.audio 每首只读取一次详情，在同一客户端内优先请求配置档，再依次尝试 lossless、exhigh、hires、higher、standard，每档最多一次。ID 一致且 URL 明确为空，或仅音质端点返回 HTTP 404、JSON code 404 和 data null 时，进入下一档。歌曲详情 404、非结构化 404 仍作为请求错误。协议或身份异常、试听、取消、网络错误和限流沿用原有处理。成功结果保留站点报告的档位和码率，并经过原有下载、时长和解码检查。
