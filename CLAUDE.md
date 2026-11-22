# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

DJTOOL 是一个 Mac 本地的 DJ 曲库整理与 Set 编排桌面应用：收歌（本地文件 / NCM / 网易云链接 / SoundCloud 单曲）→ 待整理 → 分组与 Set → 单向同步到 Serato DJ Pro。Python 3.13 + PySide6（Qt Widgets / Multimedia）+ SQLite，界面与文档均为中文。

## 工作方式

- 用户（唯一使用者，也是 DJ）负责试用和产品感觉，助手负责实现。**直接在 `main` 上优化和重构，不要另开分支**（用户 2026-11-21 明确要求；`main` 是公开仓库的唯一分支）。
- 当前目标：改善日常使用体验、重构代码。仓库已经公开在 `github.com/asiansumer/djtool`，作为作品集（2026-11-22 用户的决定：公开；MIT，署名用 GitHub 用户名；NCM 解密和第三方解析的代码一起公开；公开的历史从一个干净的首次提交开始）。公开之前的开发历史只留在本机的 `codex/mvp-intake` 和 `old-main` 两个分支上，**永远不要推送这两个分支**（里面有个人邮箱、已删除的第三方解析器副本和账号登录代码）。这个仓库的提交邮箱在本地设成了 GitHub 的隐私邮箱，不要改回去。推送是对外动作：用户说推才推，推之前跑完测试和 lint。
- 用户对产品的定义（2026-11-22）：**扩曲库时把歌方便地收进来，NCM 转成 MP3，曲库保持整齐，Serato 里直接能用。** 他明确嫌旧界面“劣质、入口乱”，要求界面好看。新功能先问“这是不是在帮他收歌和保持整齐”，不是就不加。
- 用户实际没用过“已整理”标记、阶段、能量、备注这些整理字段（1908 首全为空），新界面不再展示它们。
- 只使用项目的 `.venv`，不改全局 Python。需要本机 FFmpeg（`/opt/homebrew/bin/ffmpeg`）。
- 代码里保留说明数据流、边界和取舍的中文注释；改动到里程碑时同步 `README.md` 和 `docs/` 里的 PROGRESS / DEVELOPMENT。

## 常用命令

```bash
# 运行（日常曲库，位置记在 QSettings 里）
.venv/bin/python main.py
# 用隔离曲库运行：不改日常设置，也不会启动真实 Serato 自动同步
.venv/bin/python main.py --library /绝对路径/测试曲库

# 全部测试（unittest，不是 pytest；UI 测试需要 offscreen）
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
# 单个文件 / 单个用例
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest tests.test_library -v
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest tests.test_library.类名.test_方法名

# 构建独立 .app（PyInstaller，Apple Silicon，输出 dist/DJTOOL.app）
.venv/bin/python -m pip install -r requirements-build.txt
scripts/build_app.sh
# 冻结包离线自检：合成音频 + 临时曲库，硬拒绝联网 / 播放 / 钥匙串 / 真实 Serato 写入
env PATH=/usr/bin:/bin QT_QPA_PLATFORM=offscreen dist/DJTOOL.app/Contents/MacOS/DJTOOL --check-runtime work/app-check
```

提交前跑 `.venv/bin/python -m ruff check .`（只查错误，不查风格；配置在 `pyproject.toml`，ruff 在 `requirements-dev.txt`）和 `git diff --check`。没有 formatter。版本号在 `DJTOOL.spec` 的 `info_plist`（同时出现在 `docs/PROGRESS.md` / `docs/DEVELOPMENT.md` 开头）。`.github/workflows/tests.yml` 在 macOS 上跑 lint 和全部测试。

改界面后用 `scripts/qa_redesign.py <曲库目录> [输出目录]` 出截图自查（离屏渲染，不联网、不播放、不碰 Serato）。两个可用的曲库：`work/dev-library` 是日常曲库数据库的一份私有副本，封面和路径都是真的，**截图只能留在 `work/` 里**；`scripts/demo_library.py <新目录>` 生成的演示曲库（虚构歌名、程序画的封面、静音音频）是 README 截图的来源，`docs/screenshots/` 里只放用它出的图。

`scripts/` 里其余的脚本：`qa_serato_isolated.py`（隔离目录里的 Serato 写入实验）、`connect_serato_library.py`；`qa_netease_site_live.py`、`qa_online.py`、`qa_storage_live.py` 和 `main.py --check-download <work 下的目录> <歌曲ID…>` **会真的联网或动真实目录，只能在用户明确要求时运行**。

## 架构

所有代码都在 `djtool/` 包里：内核模块直接放在包下（`djtool/library.py` 等），界面在 `djtool/ui/`；根目录的 `main.py` 只是从源码启动的薄入口，真正的入口是 `djtool/app.py`（也可以 `python -m djtool`）。模块之间一律写 `from djtool.xxx import …` 或 `from djtool import xxx`。`pyproject.toml` 只放工具配置，应用不作为 wheel 安装（`assets/` 在包外，按仓库根目录或 PyInstaller 的 `_MEIPASS` 找）。旧界面（`window.py`、`ui.py`、`dialogs.py`、`online_ui.py`、根目录 `theme.py`）和遗留的网易云账号 / 本机直连 / 订阅 / Set 导出模块已在 0.13.0 全部删除；需要参考时看本机 `codex/mvp-intake` 分支上的 `2ce7d4c`（公开仓库里没有这段历史）。

- **界面（用户选定的“唱片墙”样式）**：`djtool/ui/main_window.py`（没有侧栏：顶部切换 收歌 / 曲库 / Sets，中间一个收歌入口，底部一整行试听栏；选中后的操作条、拖动时的 Set 目标、提示是悬浮在 `shell` 上的层，由 `_place_overlays()` 定位）、`library_view.py`（封面墙 `WallView` 和列表 `TrackTable` 共用一个模型和代理，`TrackViewMixin` 里是共用的选择与拖动）、`covers.py`（封面后台加载与缩略图）、`set_view.py`（Sets 拼图墙 + 可拖动排序的歌单）、`intake_view.py`（收歌页）、`player.py`、`dialogs.py`（曲库位置、音乐保存目录、完整歌曲信息）、`widgets.py`、`theme.py`（设计变量）。
- **收歌调度**：`djtool/intake_queue.py`（批次队列 + 记住的歌单）、`djtool/watch.py`（收歌文件夹）。
- **内核**（下面的文件都在 `djtool/` 下）：
  - 数据：`library.py` 的 `Library` 是唯一的 SQLite 与曲库文件边界；界面不直接写 SQL、不直接复制音频。
  - 收歌管线：`intake.py` → `ncm.py` / `audio_tasks.py`（本地文件）；`online.py` → `netease_backend.py` → `netease_site.py`（网易云链接），`online.py` 里的 `soundcloud_*`（SoundCloud，经 yt-dlp）；`metadata.py` / `tracks.py`（标签读写）；`netease_common.py`（错误类型与 CDN 地址检查）。
  - Serato 适配：`serato.py`、`serato_intake.py`、`serato_bpm.py`。
  - 后台线程：`workers.py`（`ImportWorker`、`ReferenceWorker`、`DownloadWorker`）。

### 界面的几条规矩

设计依据是用户的一句话：“打开它，怎样用得最舒服、最流畅、最爽”。改界面前先按这个问一遍。

- **一个入口**：链接、文件、文件夹都交给 `IntakeQueue.submit()` / `add_files()`，变成一个 `Batch`，排队依次运行（同一时间只有一个后台任务）。不要再加并列的“添加音乐”入口。粘贴即开始；回到应用时剪贴板里有能收的链接就在收歌栏下提示（只提示，不自动联网）。
- **收歌页记住的是“来源”，不是一次次的链接**（用户 2026-11-22 嫌每次复制链接麻烦）：
  - *歌单*（`Library.link_sources`）：读过的歌单链接自动留下，带名字、上次读到的顺序、只收前几首的范围、`mode`（`all` 全收 / `pick` 挑着收）。现状（多少首、缺几首）只读数据库算出来，不联网。「检查新歌」重读一个，「一键收新歌」重读所有 `all` 的并直接下载缺的；`download_source()` 按上次的列表下载、不重读。SoundCloud 某个人的上传 / 转发页默认是 `pick`。
  - *文件夹*（`djtool/watch.py`，设置在 `.djtool/watch-folders.json`）：常用下载目录里没处理过的 MP3 / NCM，回到应用时只数数量，点一下全收；每个文件夹可以单独打开“自动收”。**默认不自动收**，因为应用没有删除音频的功能，收错了收不回去。日常模式下网易云客户端的 `~/Music/网易云音乐` 存在就默认列出；`--library` 的隔离会话不带这个默认。
  - 批次（`Batch`）只是这一次打开应用期间的工作记录，显示在页面最上面，不保存。
- **选中不挪版面**：选中歌曲只出现底部的悬浮操作条（试听 / 加入 Set / 详情）；详情面板是盖在曲库上的层，要用户主动打开。不要做“选中后右侧挤出一栏”这类会让封面墙重排的设计。
- **试听栏占满窗口底部**（用户要求：进度条要长、好拖）：进度条拿走所有剩余宽度，整个高度都能点，悬停显示时间。不要改回居中的胶囊。
- **封面不在绘制时解码**：列表和封面墙一律通过 `covers.loader()` 取图（`large_pixmap` / `small_pixmap`，没准备好返回 None，画占位，`ready` 信号后重绘）。缩略图存在曲库的 `.djtool/thumbs/`。只有单张大图（详情面板、播放器）可以用同步的 `theme.cover()`。
- **颜色只在 `djtool/ui/theme.py` 的 `THEMES` 里定义**，样式表用 `$token` 占位，自绘控件用 `theme.qcolor('token')`。不要写十六进制颜色。需要随主题变化的控件实现 `retheme()`。QSS 的圆角不能超过控件高度的一半，否则整条圆角规则失效。
- **刷新不重建控件**：`LibraryPage.set_tracks()` 在同一批歌时只发 `dataChanged`，选择和滚动位置保留；文件是否存在在装载数据时一次查完。收歌页的完整刷新（重读数据库）合并到 250 ms 一次，下载过程中密集的信号只改文字。
- **`QListView` 图标模式排格子时会再给滚动条留一段宽度**：`WallView._fit()` 先减掉它再算列数，否则最后一列会掉到下一行。
- **拖动都自己处理鼠标事件**（曲库拖到 Set、Set 内排序），不走系统拖放循环；拖动开始时主窗口浮出 `tray`，里面每个 `DropChip` 实现 `accept_tracks(ids)`。
- **Serato 没有按钮**：`SeratoWorker` 每 15 秒以及每次曲库变化后，依次同步收歌 Crate、同步内容有变化的 Set（只在 Serato 关闭时写，同一份内容失败过不重试）、在 Serato 数据库变化时重读 BPM 和调性。状态只显示在右上角的标记和 Set 页的一行字里。
- **后台线程结束后用 `worker.setParent(None)` 交给 Python 回收，不要 `deleteLater()`**：父对象先销毁时，排队中的延迟删除会重复删除并崩溃。
- 反馈统一走 `MainWindow.notify()`（悬浮提示，可带“撤销”），不要再加常驻的说明文字。

### 曲库与数据模型

一个“曲库”是用户选择的文件夹：`.djtool/library.sqlite3`（schema `user_version=4`，之后的新表用 `CREATE TABLE IF NOT EXISTS` 增量添加而不升版本号）加上若干 JSON 侧文件——`storage.json`（新音频保存目录，可指向曲库外的 `文稿/Serato/DJTOOL Audio`）、`netease-settings.json`（下载音质）、`watch-folders.json`（收歌文件夹）、`serato-intake.json`、`serato-sync/`（manifest、备份、启用记录）。旧版留下的 `playlist-ranges.json` 只在第一次建 `link_sources` 表时读一次。JSON 侧文件一律“同目录临时文件 + fsync + `os.replace`”，读到损坏内容时报错而不是悄悄重置。

`Library.connection()` 每次操作开一个新连接（WAL），所以后台线程可以各自持有 `Library` 引用并发读写；多步写入用 `BEGIN IMMEDIATE`。核心表：`tracks`（`digest` 唯一 = 整文件 SHA-256）、`collections`（`kind` 为 `group` 或 `set`）、`members`（带 `position` 和每个 Set 独立的 `note`）、`online_jobs`（每首链接歌曲一条任务，`(provider, remote_id)` 唯一）、`link_sources`（记住的歌单：`items_json` 是按歌单顺序排的 `online_jobs.id`）、`platform_tracks`（平台歌曲 ID → track，下载前去重的依据）、`local_sources`（NCM 来源）、`serato_bpm_cache`。老曲库里还留着旧版订阅功能的 `intake_sources` / `source_items` / `watched_files` 三张表，只在迁移时读，不再创建。

分组和 Set 只是对同一份播放文件的引用：分类、改标题、排序都只改数据库，不移动 / 重命名音频。音频与 SQLite 不在同一个事务介质里，所以 `import_file` / `reference_existing` 自己记录 `created` 列表并在异常时清理本次新建的文件。

### 链接收歌的数据流

`IntakeQueue._run_read` → `online.normalize_link`（只认网易云歌曲 / 歌单，SoundCloud 歌曲 / 歌单 / 某人的上传、点赞、转发）→ `online.fetch_listing`：网易云走 `netease_backend.create_music_client(library)`（只有一种取歌方式：`netease_site.NeteaseSiteClient`，`netease-settings.json` 只剩音质）；SoundCloud 走 `soundcloud_listing`（yt-dlp 平铺读取列表，再按 50 首一批补标题、作者、时长、封面，最多读最新的 500 首）→ `Library.enqueue_many`（整份歌单一次事务，同时调用 `_reconcile_online_jobs` 用平台 ID 认出已入库的歌）→ 歌单再 `Library.save_link_source`。

下载：`workers.DownloadWorker` 逐首调用 `online.download_track`：解析音源 → 网易云只从官方 CDN 下载并校验大小 / MD5，SoundCloud 用 yt-dlp 取完整音频流（排除试听片段和作者开放的原文件下载，也不访问描述里的外部下载链接；用户 2026-11-22 确认保持这样，不要改成“优先拿原文件”，也不用再问）→ 按需转 MP3 → 核对时长并用 FFmpeg 全段解码 → 写标签（含 `TXXX:DJTOOL_SOURCE(_ID)`）→ `Library.import_file` → 任务置为 `complete`。测试里替换下载用 `patch('djtool.online.download_track')` 和 `patch('djtool.netease_backend.create_music_client')`（`DownloadWorker.run` 在运行时才导入它们）。

`Batch.scope()` 是“只收前 N 首”：先按歌单原始顺序截取，再排除已有的歌，不是“取前 N 首缺少的”。限流（`SiteRateLimit`）不算单曲失败：短冷却自动等待续跑，长冷却停批并保留队列。

### 三条 Serato 通路（各自独立）

1. **Set → Crate**（`serato.SeratoCrateAdapter`，由 `SeratoWorker.sync_sets` 自动调用）：只写 `~/Music/_Serato_/Subcrates/DJTOOL%%名称.crate`，只管理 manifest 里登记过的文件；写入前要求 Serato 已关闭、有同步锁、有备份、检测外部修改。真实写入还要求 `native-validation.json` 里的“五阶段原生验收”记录与当前安装的 Serato 版本 + 可执行文件摘要一致，Serato 升级后会自动失效。
2. **收歌自动同步**（`serato_intake.sync_library`）：`SeratoWorker` 每 15 秒（以及每次导入 / 下载完成后）把曲库里所有可用歌曲同步成一个固定的托管 Crate `DJTOOL / 收歌`。“Crate 已写好”和“Serato 原生库已接收”是两个状态，分别显示。用 `--library` 打开的隔离会话不会启动它。
3. **读取 BPM 和调性**（`serato_bpm.py`）：把 Serato 的 `root.sqlite` + WAL 以只读方式复制到临时目录后在副本上查询，按完整路径 + 文件大小精确匹配，结果只写进 `serato_bpm_cache`（`bpm` 和 `musical_key`）。支持 schema 202（Serato 4.0）和 203（Serato 5.0）。由 `SeratoWorker` 在 Serato 数据库发生变化时自动重读，不需要按钮。

任何时候都不直接写 Serato 的 SQLite / `database V2`，也不碰用户自己的 Crate。

### 线程与“忙”状态

只有两条后台线程：`IntakeQueue` 的当前任务（读链接 / 下载 / 导入 / 登记已有音乐，排队串行），和 `MainWindow.serato_worker`（同步收歌 Crate、同步 Set、读 BPM）；另有 `covers.py` 的小线程池只做图片解码。`closeEvent` 会请求中断后用 300 ms 定时器重试关闭，直到线程都结束；切换曲库（`attach_library`）前也要求两者空闲。

`MainWindow.reload()` 重新读 tracks 和 collections 并交给各页面；下载过程中的刷新经 `schedule_reload()` 合并到 400 ms 一次。

### 主题

见上面“界面的几条规矩”：`djtool/ui/theme.py` 里暗色、浅色各一组同名变量。底色接近纯黑，强调色是 Cue 垫的黄色，只用于下一步动作、选中和进度；专辑封面是界面里唯一的彩色。四个候选方向的对比页在 `design/style-options.html`（未入库），用户选的是 A。

### 打包自检

`djtool/runtime_check.py`（`--check-runtime`）针对现在的窗口：合成静音 + 临时曲库，硬拒绝联网、播放和真实 Serato 读写，走一遍入库、Serato 夹具读 BPM / 调性、隔离 Crate、记住的歌单与范围、收歌文件夹导入、各页面截图和三个对话框。改了主窗口或收歌流程后要同步改它。

## 不能破坏的产品边界

这些来自 `docs/PRD.md` 和旧协作约定（`docs/AGENTS-2026-10.md`）的用户决定，优先于“看起来更合理”的实现：

- **App 不计算 BPM 或调性**，也不提供手动校正；只显示从 Serato 读到的值，缺值显示“—”。不要用文件标签或历史估计值替补。（2026-11-22 用户同意：读取改为后台自动进行，并允许按 BPM 排序。）
- **去重只按整文件内容或经过验证的平台歌曲 ID**，绝不按歌名 / 艺人合并（remix、不同版本必须并存）。
- 导入保留源文件；本版本不提供删除音频文件的功能；`_Serato_` 和 `_Serato_Backup` 目录不扫描、不写入音频。
- 未完成当前 Serato 版本的隔离验收时，不写用户真实的 Serato 目录；实验只在项目 `work/` 或系统临时目录里做（`SeratoCrateAdapter(isolated=True)`）。
- 应用不读取也不保存任何账号会话（相关代码已删除，`keyring` 不再是依赖）；临时媒体 URL、服务返回的 Cookie、检测到的公网 IP 不写入 SQLite、日志或文档。错误信息不转述 Requests 的原始异常文本（见 `network_errors.py`）。
- 验收记录要区分“合成静音 / 模拟数据 / 离线夹具”和“真实音乐 / 真实联网 / 原生 Serato 窗口”，不能用前者宣称后者通过。

## 文档分工

`README.md` 是作品集页面：项目是什么、截图、设计取舍、怎么运行；截图只用演示曲库出。其余文档在 `docs/`：`PRD.md` 是需求基线，**文末较新的需求变更优先于前文的历史方案**；`DEVELOPMENT.md` 按版本记录设计取舍；`PROGRESS.md` 是验证日志；`RESEARCH.md` 是外部接口与 Serato 格式的实验依据；`MVP-ACCEPTANCE.md` 是验收门槛；`FEATURE-NOTES.md` 是 0.11 及之前按版本累积的功能说明（原来的 README）；`AGENTS-2026-10.md` 是此前给 Codex 的协作约定。根目录的 `AGENTS.md` 只是指向本文件的一页。**文档里不写用户自己的歌单 ID、平台昵称和本机路径**（公开前已经脱敏成 `<个人歌单 A>` 这样的占位）。
