# DJTOOL

Mac 本地的 DJ 收歌与曲库工具：把歌收进来，NCM 转成 MP3，整齐地放好，打开 Serato 就能用。

**当前版本 0.13.0（构建号 23）。** 界面是一面“唱片墙”，旧界面和不再使用的账号、订阅、导出代码已经全部删除。

- **收歌**：窗口顶部只有一个入口——粘贴网易云或 SoundCloud 的链接，或拖入 MP3、NCM、文件夹。读过的歌单会被记在收歌页上，带着现状（多少首、缺几首）；以后不用再复制链接，点「一键收新歌」就把所有歌单里新加的歌收下来。常用的下载文件夹（比如网易云客户端的下载目录）也列在这一页，里面有没收过的文件时点一下全收。
- **SoundCloud**：除了单曲，还能读歌单、某个人的点赞页（`soundcloud.com/用户名/likes`）和上传页。把自己的点赞页贴进来一次，以后在 SoundCloud 上点个赞就等于把歌加进了待收清单。
- **曲库**：封面墙和列表两种视图，按最近添加、BPM、调、艺人、标题排序。选中歌曲后底部浮出操作条（试听、加入 Set、详情）；拖动歌曲时 Set 会浮出来当目标。
- **Sets**：封面拼图墙，点进去拖动行排序。
- **试听**：底部一整行是试听栏，进度条占满剩余宽度，整个高度都能点，悬停显示时间。
- **Serato**：BPM 和调性由 Serato 分析，DJTOOL 在后台自动读回来（支持 Serato 4 和 5）；收进来的歌和 Set 的改动在 Serato 关闭时自动写过去，没有同步按钮。

下面按版本累积的说明里，“链接收歌窗口”“暗夜模式按钮”“读取 Serato BPM 按钮”“批量加入 Set 的右侧面板”等描述的是 0.11 的旧界面；功能规则（去重、只收前几首、下载校验、Serato 写入边界）没有变。

## 音乐与 Set 的结构

- 现有 DJTOOL 曲库记录继续保存在原 `.djtool/library.sqlite3`，保留已有歌曲、分组、Set、顺序和备注。
- 新歌统一保存到「文稿/Serato/DJTOOL Audio」。路径配置保存在原曲库的 `.djtool/storage.json`，切换保存目录只影响以后导入的音乐。
- Serato 目录里的 MP3、WAV、FLAC、AIFF、M4A 可以按原路径登记进 DJTOOL；不复制、不改标签、不移动源文件。`_Serato_` 和备份目录不会作为音乐扫描。
- 同一首歌可属于多个分组或 Set；Set 只保存引用和顺序，不在系统里创建对应的音频文件夹。
- 设置菜单的「连接 Serato 音乐目录」调整以后音乐位置；「读取 Serato 目录里的已有音乐」刷新原路径索引。取消不会改变配置。

## 链接收歌

粘贴网易云单曲或歌单链接，点击「读取链接」。页面自动核对本地曲库，显示歌单总数、已入库与待下载数量，默认「只看待下载」；取消该筛选可查看全部歌曲。

歌单的「下载范围」填写原始顺序的截止序号，0 表示全部。例如填 940，范围是原歌单第 1–940 首，包含其中已经下载的歌曲；不会因为筛掉已有歌曲而继续补下载第 941 首以后的内容。范围按歌单 ID 单独记住，重新读取或重启后继续使用；切换到其他歌单采用该歌单自己的范围。范围外歌曲不会下载，也不会被全局忽略，之后可在其他歌单里正常下载。修改范围前先停止当前批次，运行期间范围控件禁用。

「收歌记录」只查看历史和试听已有歌曲；继续下载时重新读取歌曲或歌单链接，恢复该链接的范围设置。

点击「下载缺少的歌曲（N 首）」一次处理范围内全部页面，保留歌单顺序，跳过已有和已忽略歌曲。单曲下载与勾选下载也受当前范围限制。只有新入库的歌曲进入「待整理」，已有歌曲的整理状态、分组、Set 和备注不变。单曲、勾选、停止、失败重试与整批统计保留，收歌历史不会误当作一个歌单。账号、会员会话、网页接入设置和官方下载接收按钮已移除。网站返回短暂冷却时间时，整批会显示倒计时并在等待结束后自动继续；可以随时停止。没有提供等待时间、长期限额或同一首反复限流时，会保留队列并暂停。TLS、代理、请求配置和网络权限故障会停止整批；连续三首连接或超时失败也会先停止，避免共同故障把千首歌曲全部标失败。

下载前去重按精确平台歌曲 ID：成功下载记录、已核对完整时长的 NCM 来源、DJTOOL 写入的来源 ID 标签。跨歌单、重新读取和重启均可复用；队列记录以外也保留成功的 ID 与曲库关联。普通旧 MP3 没有可靠来源 ID 时无法提前认出，不能按同名合并 remix 或不同版本。已关联播放文件丢失会单独显示「文件缺失」，不算已有可用歌曲，也不自动重复下载；恢复原文件后重新核对即可。

**用户已改为选择 [wyapi 在线解析站](https://wyapi.toubiec.cn/)。** 请求发送至 `nextmusic.toubiec.cn`，提交歌单/歌曲 ID，以及服务检测到的公网 IP；不读取或发送用户网易云账号、Cookie。服务返回的 cookie 字段无条件丢弃，媒体只从已验证的网易官方 CDN 获取。线上站本身是独立的闭源服务；以前的本机直连适配器已在 0.13.0 删除。

站点 14 首公开 DJ 歌单与两首单曲的元数据、音源解析已真实返回。500 首接口分页会读取到 songCount 完整一致；离线 1001 首接口分页和 833 首整队列验证通过。网页十首 ZIP 只是前端切片，DJTOOL 不走该打包流程。站点限额仍然适用：429 立即停止新请求并保留队列，在给出的等待期限内禁止再次下载；明确的短冷却到期后按前述规则续跑。2026-10-04 用户已明确允许歌单/歌曲 ID 与检测到的公网 IP 提交到该服务，继续开展真实下载验证；完整歌单读入与全部音频下载分别验收。SoundCloud 单曲仍通过平台音频流获取，不访问作者描述的外部下载链接。

优先请求所选音质。如果站点为该档返回空地址或明确回复未找到，DJTOOL 会为同一歌曲 ID 自动尝试其他音质，优先无损，最后尝试较低码率。服务限流时遵守等待时间，返回试听或歌曲 ID 不符时停止处理该音源。批次结束会显示本次仍缺多少首，再点「下载缺少的歌曲」只补未入库的曲目。

下载会核对服务提供的文件大小和 MD5，并检查完整时长与全段解码，再写标签和入库。需要时会转为 MP3，源音质按服务实际返回的档位和码率记录。临时音源 URL 不持久保存。

0.11.4 已真实读取歌单 `<个人歌单 A>`「我喜欢的音乐」的 1315 首；兼容站点 `1:60` / `2:60` 秒数未进位格式。站点 `time` 为结果生成时间，不当作音频时长或发行日期。两首原故障曲《Hit The Road Jack》《VVSane》已在项目隔离库完成真实 320 kbps 下载、封面/歌词、解码入库和重复跳过验证；未据此宣称全部 1315 首音频均可下载。

## 批量加入 Set

右侧先选目标 Set。在曲库或待整理中用 Shift 连选、⌘ 点选，或点击「全选当前列表」，再点「加入 Set（数量）」。全选只包含当前搜索/风格筛选后的歌曲；新成员按当前列表顺序接在 Set 末尾，已有歌曲自动跳过，原顺序和备注保留。多选后也可整批拖入右侧指定位置。右键菜单保留批量加入入口。

如果歌曲来自同一歌单，在「链接收歌」读取这个歌单后，点击「已下载歌曲加入 Set（数量）」即可。它覆盖当前范围内的全部页面，按原歌单顺序加入；「只看待下载」隐藏已有歌曲时也能使用。不重新下载音频，不改变待整理状态、分类或其他 Set。只有已成功入库且文件仍在的歌曲会被采用。

## 收歌后自动加入 Serato

本地导入或链接下载完成的歌曲保留在 DJTOOL 待整理，同时自动同步到 Serato 的「DJTOOL / 收歌」。已有的可用歌曲也会补同步，不必先分类、标记整理完成或加入 Set。音乐仍在原路径，不再复制一份音频。

Serato 运行时等待关闭，DJTOOL 保持打开后会自动继续（最长约 15 秒检查一次）；下次启动 Serato 时接收新增歌曲。曲库底部分别显示待同步、已准备导入和 Serato 已接收，不能把 Crate 文件写好误当成原生程序已导入。退出 DJTOOL 后，待同步内容保留在曲库记录里，下次打开继续。

缺失的播放文件单独提示，其他歌曲照常同步。仅维护 DJTOOL 创建的收歌列表，不改 DJTOOL 的待整理标记、分组、Set 或备注；已有同名列表、外部修改、版本变动和写入失败都会保留原数据并提示，不覆盖用户自己的列表。每次写入有独立备份。自动导入适配已完成本机 Serato 5.0.0 的隔离原生五阶段验证。

## Set 同步到 Serato

右侧选中 Set，点击「同步到 Serato」查看有序歌曲和目标位置。关闭 Serato 后同步，再打开 Serato；列表显示在「DJTOOL / Set 名称」。只写 DJTOOL 创建且持有记录的 `.crate`；不直接编辑 Serato 的 SQLite、database V2、其他列表或音频。每次写入保留备份；已有同名列表或外部修改会阻止覆盖。

本机 Serato **4.0.10 / 5.0.0** 分别通过隔离原生创建、重启、改序、再次重启和恢复五阶段验证。同步启用记录绑定安装版本与可执行文件摘要；Serato 升级后需要重新验证。外置盘路径尚未验证，暂不自动同步。空 Set 暂不推送清空；4.0.10 原生测试发现 Serato 会保留原列表，App 因此阻止此操作。原生实验使用合成静音，不作为真实音乐听感验收。

## 查看 Serato BPM

曲库页面点击「读取 Serato BPM」，将 Serato 当前保存的数值更新到 DJTOOL；设置菜单也保留这个入口。主曲库、当前 Set 和歌曲详情显示同一份 Serato BPM；缺少数据时显示「—」。数值保留小数精度，例如 Serato 的 79.68 不会用原文件标准 BPM 标签中的 80 替代。没有自动计算、BPM 筛选或手动校正入口。

新歌需要先在 Serato 中导入并完成分析，再回到 DJTOOL 点击「读取 Serato BPM」。这不是实时同步；Serato 内改变节拍后，也需手动更新。显示来源标为 Serato，其当前数值可能来自分析、既有标签或用户在 Serato 内的修改。

当前支持本机 Serato 4.0 的 schema 202 根卷库，默认读取 `~/Library/Application Support/Serato/Library/root.sqlite`。按完整播放路径、实际文件大小和有效状态核对，不按歌名或艺人猜测；外置卷、流媒体、缺失、损坏、过期或冲突记录不填入 BPM。未分析或还没同步的新歌保留空值。

读取只复制 Serato 数据库及其 WAL 到私有临时目录，在副本中查询；不通过 SQLite 打开真实源库，不改 Serato、音频标签、Cue 点或 Beatgrid。结果保存在 DJTOOL 自己的独立缓存，原标签、历史 BPM、整理信息和 Set 不覆盖。完整成功读取会清除已不再有效的缓存值；未知数据库版本、读取失败或停止时保留上次结果。

## 其他可用功能

本地 MP3/NCM 导入保留源文件，按内容去重；NCM 先提取/按需转换再校验。曲库搜索、分组、拖拽编排、逐首备注、撤销移除、试听与持久保存保留。修改整理信息只改本地记录。BPM 识别交给 Serato，App 不启动分析。

旧订阅/官方下载目录的历史配置与兼容模块保留；本次精简界面未重新展示这些入口，启动或切换曲库时不恢复旧后台接收任务。自动接收与普通匿名解析的完整下载分别验收，不把历史官方账号结果作为匿名能力证明。

## 打开

本机已构建的独立应用在 `dist/DJTOOL.app`。也可以双击项目中的 `Start DJTOOL.command`，或在终端运行：

```bash
cd /path/to/DJTOOL
.venv/bin/python main.py
```

`.app` 内置运行依赖，面向本机 Apple Silicon；尚未完成 Developer ID 签名、公证及其他 Mac 安装验收。不要同时运行旧窗口与新 App 管理同一曲库。修改代码后需要重建 `.app` 或重启源码入口。

重新构建（使用项目环境，不更改全局 Python）：

```bash
.venv/bin/python -m pip install -r requirements-build.txt
scripts/build_app.sh
env PATH=/usr/bin:/bin QT_QPA_PLATFORM=offscreen dist/DJTOOL.app/Contents/MacOS/DJTOOL --check-runtime work/app-check
```

发布包自检只处理合成文件、在临时曲库截图；禁止播放器加载和播放，不联网、不读取账号，并检查本机解析、新收歌表格、音乐目录和 Set 同步预览。尚未完成适合公开分发的签名安装器验收。

第一次添加音乐时选择曲库存放位置，默认建议 `~/Documents/Serato`；确认之前不会创建这个曲库。可以在设置中打开其他已有 DJTOOL 曲库。切换位置不会自动搬迁文件。

## 先这样试用

1. 点击“添加音乐”，选几首 MP3 / NCM。旁边的下拉按钮可以添加整个文件夹。
2. 双击一首歌试听；右键“整理歌曲”填写自己觉得有用的信息。底部铅笔也能整理正在试听的歌。
3. 在右侧“当前 SET”旁点击加号，新建一个 Set；把曲库里的歌曲拖过去，再上下拖动排序。
4. 右键 Set 中的歌曲可以写衔接备注；点击行末移除后，左下方可撤销。
5. 关闭并重开，检查自己的排列是否保留。

快捷键：`⌘O` 添加文件，`⌘F` 搜索；列表获得焦点时空格播放/暂停。删除分组或 Set 不会删除音乐文件。

## 文件保存

```text
你选择的曲库/
  Audio/                  # 稳定的播放副本，文件名带唯一标识
  Artwork/                # 音频中原有的封面
  .djtool/library.sqlite3  # 歌曲、分组、Set、顺序和备注
  .djtool/playlist-ranges.json # 每个歌单记住的下载范围，0 表示全部
```

手动备份时先关闭 App，再复制整个曲库文件夹。不要单独移动 Audio 内的文件。原始导入文件独立保留；本版本不提供音频文件删除功能。

本地文件导入按整文件内容去重；仅修改过标签的两个文件可能仍被当作不同文件。链接收歌另按经过验证的平台歌曲 ID 在下载前去重，未知来源的旧文件不按歌名推测。

## 代码组织

| 文件 | 职责 |
| --- | --- |
| `main.py` | 启动与应用级设置 |
| `window.py` | 双栏工作台、播放器、按钮与页面切换 |
| `ui.py` | 样式、Tabler 图标、歌曲行和拖拽 |
| `dialogs.py` | 曲库位置、歌曲整理、同步预览 |
| `library.py` | SQLite、稳定文件副本、内容去重、事务和独立 Serato BPM 缓存 |
| `tracks.py` | 只读 MP3 元数据与时长/BPM 的显示规则 |
| `workers.py` | 后台文件导入与停止 |
| `metadata.py` | 完整文本标签与技术信息、下载文件标签写入 |
| `online.py` | 平台解析、旧下载适配器、下载/转码/完整性检查 |
| `online_ui.py` | 收歌队列、进度、选择、重试与官方授权入口 |
| `playlist_ranges.py` | 按歌单独立保存下载范围，原子写入与损坏配置保护 |
| `serato_bpm.py` | 只读复制本机 Serato 库，校验播放路径、文件大小和状态，再刷新 DJTOOL BPM 缓存 |
| `netease_openapi.py` / `netease_auth_ui.py` | 官方 RSA 签名、扫码授权、账号验证、钥匙串与 Key 设置窗口 |
| `netease_music.py` | 官方歌单资源映射、完整音源权限、原始 ID 校验和歌词 |
| `netease_local.py` | Netease_url 本机协议、完整歌单、EAPI 音源、歌词和钥匙串会员会话 |
| `netease_backend.py` / `netease_settings_ui.py` | 曲库独立音源 / 音质设置、入口切换和会话验证窗口 |
| `intake.py` | 本地文件稳定副本、转换与入库流程 |
| `ncm.py` | 有界 NCM 提取、实际格式判断与转换元数据 |
| `sources.py` / `source_ui.py` | 持久歌单/目录来源、后台接收和管理窗口 |
| `set_export.py` | Set 有序播放列表、选曲单、独立导出与失败清理 |
| `runtime_check.py` / `DJTOOL.spec` | 独立 Mac 构建及无播放的发布包自检 |
| `audio_tasks.py` | 可停止的文件解码 / 转码进程；不调用播放器 |

第一轮用户练习代码的原样副本留在未跟踪的 `work/learning-snapshot/`，方便以后讲解。

## 开发与验证

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

测试需要本机 FFmpeg。桌面检查脚本会在 `work/desktop-qa/` 中创建合成静音 MP3 和隔离曲库，检查真实解码器、界面逻辑并截图。它不会使用日常曲库；每次运行会重置该测试 Set 的顺序。

需要安静验收时，使用以下脚本。它禁止调用播放器播放或加载音乐，检查 NCM、元数据、旧值保留、无 BPM 分析/筛选/校正控件和无分析依赖，截图写入 `work/intake-no-bpm/`：

```bash
.venv/bin/python scripts/qa_intake.py
.venv/bin/python scripts/qa_sources.py
.venv/bin/python scripts/qa_login_status.py
.venv/bin/python scripts/qa_scale.py
```

下面的旧桌面脚本**包含播放操作**，即使样本为合成静音，要求不播放的场合也不要运行：

```bash
.venv/bin/python scripts/qa_desktop.py
```

在线下载需要本机 FFmpeg。显式联网验收使用用户已提供的两个链接，只写项目内隔离曲库：

```bash
.venv/bin/python scripts/qa_online.py
```

`scripts/qa_online_ui.py` 检查上述验收结果的界面并截图，需要先完成联网验收。

开发时可显式指定一个隔离曲库，不覆盖日常位置设置：

```bash
.venv/bin/python main.py --library /绝对路径/测试曲库
```

图标来自 [Tabler Icons](https://github.com/tabler/tabler-icons)，MIT 许可证保存在 `assets/icons/LICENSE`。音频标签使用 [Mutagen](https://mutagen.readthedocs.io/)，播放使用 [Qt Multimedia](https://doc.qt.io/qtforpython-6/PySide6/QtMultimedia/QMediaPlayer.html)。

需求见 [PRD](PRD.md)，后续路线见 [DEVELOPMENT](DEVELOPMENT.md)，验证记录见 [PROGRESS](PROGRESS.md)。

网易云离线验证：`scripts/qa_netease_ui.py`。显式联网验收：`.venv/bin/python scripts/qa_netease_live.py <数字歌单ID> --download 1`，只写 `work/netease-download/` 隔离曲库，不播放音乐；`--download 0` 只检查列表与权限。

本机开源入口的离线窗口验证：`.venv/bin/python scripts/qa_netease_local_ui.py`。显式联网验证：`.venv/bin/python scripts/qa_netease_local_live.py <个人歌单 B> --download 1`；当前仅匿名直连，不读取账号。结果只写 `work/netease-local/`，不播放、不输出签名 URL。

站点协议与队列离线验证：`.venv/bin/python -m unittest tests.test_netease_site tests.test_full_playlist_ui`。明确授权后的站点联网验收：`.venv/bin/python scripts/qa_netease_site_live.py <个人歌单 B> --download 1`，先核对一首；`--download-all` 覆盖该隔离库完整歌单，不限制十首。结果在 `work/netease-site/`，不写用户 Serato 曲库、不播放、不保存 IP/Cookie/临时 URL。

故障歌单定点真实验收：`.venv/bin/python scripts/qa_netease_site_live.py <个人歌单 A> --song-id 1310241345 --song-id 2030602005`。先读取完整歌单，再只下载指定成员；与下载数量参数互斥，不会自动下载整个歌单。

冻结 App 的显式联网验收（须事先授权站点检测 IP 与歌曲 ID 请求）：`dist/DJTOOL.app/Contents/MacOS/DJTOOL --check-download work/frozen-live-check 3443223112`。与窗口共用后台 DownloadWorker，只写项目 work 隔离库、拒绝读取钥匙串；必须在允许网络的运行环境执行。`--check-runtime` 继续只做离线自检，不能代替真实下载验收。
