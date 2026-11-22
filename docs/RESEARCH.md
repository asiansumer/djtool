# DJTOOL 初步技术调查

检查日期：2026-09-22。此记录支持 PRD 讨论，不是已经实现或通过测试的声明。

2026-10-04 外观实现依据：本机 Qt 6.11.2 提供 QStyleHints.setColorScheme。采用 [Qt 官方文档](https://doc.qt.io/qt-6/qstylehints.html#colorScheme-prop) 所述应用级色彩方案，加自定义 QPalette/控件模板，不修改 macOS 的全局外观。Qt 自绘曲库/收歌委托仍须显式主题颜色，只有调色板不能覆盖硬编码颜色；已用隔离像素检查和明暗往返确认，详见 work/theme-0112/ 与 PROGRESS.md。

## 1. 两个旧项目实际做了什么

### NCM-Swift：网页项目

- `package.json` 使用 Next.js、React、TypeScript 等；项目名称中的 Swift 不表示使用 Apple Swift 语言。[依赖文件](https://github.com/Asumer14/NCM-Swift/blob/master/package.json)
- 核心流程提取音频、读取格式、补充元数据，并返回结果；`decrypt()` 根据元数据或音频内容识别输出格式，包含 MP3/FLAC 分支。检查到的这条流程没有将所有音频统一编码为 MP3。[核心源码](https://github.com/Asumer14/NCM-Swift/blob/master/src/lib/ncm/ncm.ts)
- 写 MP3 ID3 标签与写 FLAC 元数据是不同分支；改标签与音频转码不是同一件事。[元数据工具](https://github.com/Asumer14/NCM-Swift/blob/master/src/lib/ncm/utils.ts)
- 可借鉴的内容：数据流、格式识别、歌名/艺人/封面处理。网页 UI 和浏览器下载交互不必整体迁入桌面应用。
- 源码存在通过 `albumPic` 获取在线封面的行为；新 App 若要求 NCM 完全离线处理，应优先嵌入封面并让在线补全成为可选功能。

读取到的仓库树 SHA：`01a7af1f8cb0f003d30511035982ed192a8fc356`；`ncm.ts` 文件 blob SHA：`b3eb3da37c26893e5014e4b9b63bc569b639c06b`。只读源码，未运行。

### NCM-Decoder：Python 桌面项目

- 主文件约 1,034 行，使用 PyQt6、QtMultimedia、Mutagen；`ncmdump-py` 用于元数据提取。[主文件](https://github.com/Asumer14/NCM-Decoder/blob/main/main.py)、[依赖文件](https://github.com/Asumer14/NCM-Decoder/blob/main/requirements.txt)
- `convert_file()`（约 156–205 行）实际执行外部 `um` 程序，查找打包资源或 `~/go/bin/um`。仅安装 Python requirements 不足以保证该转换流程可运行。[转换函数](https://github.com/Asumer14/NCM-Decoder/blob/main/main.py#L156)
- `ConversionWorker.run()`（约 280–313 行）在外部命令成功后构造同名 `.mp3` 路径并报告完成。这里没有证明真实输出一定为 MP3，也没有验证完整音频；不能沿用这种成功判定。[工作线程](https://github.com/Asumer14/NCM-Decoder/blob/main/main.py#L280)
- 播放路径中的 FFmpeg 使用 `-c:a copy`，是在复制音频流，并非将任意输入编码为 MP3。[播放处理](https://github.com/Asumer14/NCM-Decoder/blob/main/main.py#L619)
- 值得参考：独立窗口、后台任务、元数据和试听。新项目应先理解并拆分这些职责，再逐个复用；不要把整个主文件当成新项目的核心架构。

读取到的 `main.py` blob SHA：`32979d67b93eb427b16f1c3481244d19443973bd`。上述风险来自静态阅读，未用真实音频复现。

## 2. Serato 分组与同步

Serato 官方说明：一首歌曲可加入多个 crate，crate 内可调整曲目顺序，也支持子分组。这支持“文件稳定存放，分类与顺序以引用表达”的产品模型。[Crates 官方说明](https://support.serato.com/hc/en-us/articles/227561407-Crates-in-Serato-DJ-Pro-Serato-DJ-Lite-4-0)

已从本机 `/Applications/Serato DJ Pro.app/Contents/Info.plist` 的 `CFBundleShortVersionString` 读到 **4.0.9**。未启动应用，也未访问个人曲库内容。

官方备份说明对旧版本与当前版本存在不同描述。不能只根据旧版 `_Serato_/Subcrates` 经验决定写入方式；同步方案需要在安装版本上验证。[Library Auto-Backup](https://support.serato.com/hc/en-us/articles/227561667-Library-Auto-Backup)

本次未找到足以直接承诺完整第三方同步的已验证接口。后续实验至少要回答：怎样可靠创建与更新分组、怎样保留手动曲序、Serato 何时读取变更、如何检测外部修改、如何恢复失败写入。不能把“官方有 crate 功能”当成“外部程序可靠同步已被证明”。

## 3. U 盘加载慢：已知与未知

Serato 官方支持将音乐和 crate 放到外置盘，因而音乐不是必须放在内置硬盘上。[USB 曲库说明](https://support.serato.com/hc/en-us/articles/202304844-Using-a-USB-external-hard-drive-for-your-portable-library)

当前用户提供的对比是“U 盘很慢，Mac 本地很快”；控制器为 Hercules T7，U 盘型号未知，当时直插还是经过扩展坞已记不清。仅凭 USB-C 接口外形，不能判断存储设备实际性能；具体原因仍未知。官方也提示 Hub/扩展坞等连接方式可能导致连接与音频问题，但这并不证明用户的设备就是该原因。[USB Hub 说明](https://support.serato.com/hc/en-us/articles/202552170-Using-USB-Hubs-with-Serato-DJ)

后续诊断建议使用同一首歌曲、同一分析状态，比较本地与 U 盘的加载；记录是文件拷贝慢、库加载慢还是拖入 deck 慢，再检查盘型号、格式、连接方式与是否存在其他后台读取。先做只读观察和对照，不以重新格式化作为第一步。

## 4. SoundCloud 单曲下载

`yt-dlp` 的支持列表包含 SoundCloud 单曲与歌单，可作为候选实现进行验证；“列在支持列表”不保证每首曲目、每种格式都可取得。[支持列表](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)

当前上游存在 SoundCloud 请求失败、部分加密流不支持等问题记录，因此不能在尚无真实样本时承诺任意链接下载，或承诺歌单只是简单增加一个按钮。[问题 16755](https://github.com/yt-dlp/yt-dlp/issues/16755)、[问题 17335](https://github.com/yt-dlp/yt-dlp/issues/17335)

官方 API 区分完整可播放、试听和不可访问的资源；流媒体访问与原文件下载要分开处理。[API 指南](https://developers.soundcloud.com/docs)

官方 API 条款对持久下载/离线保存有限制，不能直接把官方播放 API 当作本产品下载接口的确定方案；面向朋友分发前，还需单独确定下载方式及适用范围。[API 使用条款](https://developers.soundcloud.com/docs/api/terms-of-use)

本次没有下载任何曲目。下一步需要具体样本，核实能取得的格式、完整性、来源音质、失败情况和维护成本。不能绕过样本验证，把功能写成已支持。

## 5. Python 桌面路线

PySide6 是 Qt 官方 Python 绑定，适合作为本项目独立窗口的候选方案。它与旧项目的 PyQt6 属于不同绑定，复用时需检查 API 与打包方式。[Qt for Python](https://doc.qt.io/qtforpython-6/)

选择 Python 的主要理由是与用户基础和既有桌面代码衔接；视觉品质要通过具体布局、交互与真实使用评估，不能由框架名称保证。当前未安装或测试 PySide6、FFmpeg、SQLite 应用层或打包工具。

## 6. 网易云歌单自动接收（PRD 0.3 新增）

原 `Binaryify/NeteaseCloudMusicApi` GitHub 仓库当前为归档只读状态，页面标注归档日期为 2024-04-16；这只能说明该 GitHub 仓库状态，不能据此推断所有衍生包的维护状态。[原仓库](https://github.com/Binaryify/NeteaseCloudMusicApi)

找到的 `NeteaseCloudMusicApiEnhanced/api-enhanced` 明确是第三方 Node.js 项目，不是网易官方 SDK。它提供歌单、登录与下载地址相关模块，可用作可行性调查对象，尚未选择为项目依赖。[项目说明](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)

静态检查发现：

- `playlist_track_all.js` 先取得歌单 `trackIds`，再按 `offset/limit` 取得歌曲详情；默认数量上限不等于已经取得完整歌单，产品同步需要处理分页与完整性。[歌单模块](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/main/module/playlist_track_all.js)
- `song_download_url_v1.js` 向下载地址端点提交歌曲 ID、音质等级和请求选项。存在这段代码仅说明提供了请求入口，不证明任意账号/歌曲能取得可用完整文件。[下载地址模块](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/main/module/song_download_url_v1.js)
- `login_qr_check.js` 展示了扫码登录状态与 Cookie 返回流程。是否可用、会话保存和异常处理都需验证；产品登录由用户自行完成，凭据不进入聊天。[登录模块](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/main/module/login_qr_check.js)
- 项目 README 中有默认启用的其他音源匹配配置。如果选择此类适配器，必须明确关闭自动替换源，并核实返回音频的来源与版本，避免下载到同名不同版本却显示为网易原曲；这不是本项目需要的能力。

本次没有确认适合此用途的网易官方公开下载集成方案。会员状态、实际歌单、单曲权限、地区与完整音频获取范围均待核实；未运行联网接口或登录个人账号。歌单读取成功和音频获取成功必须分别验收。

候选过渡方案是接收用户在官方 App 下载好的本地文件。它能自动接上转换、分析和入库，但仍需用户在官方 App 触发下载，因此不能视为歌单全自动功能已实现。

## 7. 本地 BPM 分析（PRD 0.3 新增）

librosa 的 `beat_track` 提供全局速度估计与节拍位置，输入为音频或起音强度序列；文档说明无可检测起音时可能返回 0 BPM。它的这些返回值不能直接当作“已确认 BPM”或“准确概率”。本次核对 0.11.0 文档是为了确认能力，不代表已选定或安装该版本。[函数文档](https://librosa.org/doc/0.11.0/generated/librosa.beat.beat_track.html)

librosa 官方示例也说明，默认单一全局速度不适合明显变速的音乐。BPM 需要结合用户试听，支持保留手动校正；不能承诺和 Serato 自动分析完全相同。[变速节拍分析示例](https://librosa.org/doc/latest/auto_tutorials/03-advanced/plot_dynamic_beat.html)

节奏估计评估本身允许慢/快两个参考速度及其权重，这说明感知节拍层级需要在评估时讲清楚。本项目的 ×2/÷2 控件是人工确认工具，不代表每首歌都有两个同样适合演出的答案。[mir_eval 速度评估](https://mir-eval.readthedocs.io/latest/api/tempo.html)

还未对用户的 Hip-hop 歌曲运行分析，速度、误差与内存占用均未知。实现阶段先用少量固定节拍和异常样本建立基线，再决定算法、参数与是否需要额外的候选速度提示。

## 2026-09-23 用户样本的只读检查

- 网易云短链接 `https://163cn.tv/<短链>` 解析到歌单 ID `<个人歌单 B>`。后续保留不带分享追踪参数的地址：https://music.163.com/playlist?id=<个人歌单 B> 。
- 从本机以未登录请求访问 `/api/v6/playlist/detail`，返回 code 200，名称为 DJ，`trackCount=14`、`trackIds` 为 14 项，但 `tracks` 首次只有 10 项。后续需要补全曲目详情，不能把这 10 项当完整歌单。摘要保存在未跟踪的 `work/netease-probe.json`。
- 当前只证明公开歌单元数据可读；没有登录、没有验证完整音频下载。用户的会员状态仍未知。
- SoundCloud 用户样本：https://soundcloud.com/past12official/countingedit 。页面标题为 Hamdi - Counting (ft.Princess Superstar) [PAST12 & ST3VN Baile Funk Edit]，作者 PAST12。
- 页面描述提供作者发布的 Free Download Google Drive 文件夹。网页检索工具无法打开该 Drive 文件夹；尚未取得音频或核对版本/时长。因此不能将页面可读标为下载成功。
- 本地播放器验证使用 Qt Multimedia 6.11.2（运行日志显示其 FFmpeg 后端 7.1.5），与本机命令行 FFmpeg 8.0.1 是不同分发，不混用版本证据。

## 2026-09-23 第二轮：直接链接下载与完整元数据

用户明确不使用 SoundCloud 作者描述中的外部下载链接，并确认网易云有会员。

- 搜索并阅读了 [SC Downloader](https://sc-downloader.com/) 的链接处理流程和音质说明。站点未公开后端代码，不能声称已复制其内部实现。
- 参考公开实现 [yt-dlp SoundCloud extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/soundcloud.py) 与 [scdl 的原理说明](https://github.com/imthaghost/scdl)：解析曲目后取得可用音频流，按需要处理分段/转码。当前使用 PyPI yt-dlp 2026.8.19，仅选择完整流，排除 original download 与 preview 格式；不访问作者描述中的外部下载链接。
- 用户提供的 SoundCloud 曲目 ID 为 1614216672。通过新应用适配器实际下载 hls_aac_160k，源流 160 kbps AAC，转为 MP3。源时长 125.584 秒，结果 125.623 秒，封面存在，艺人 PAST12，风格 BaileFunk。再次下载同一来源 ID 跳过重复入库。证据在忽略提交的 work/online-qa/result.json；这不证明所有 SoundCloud 链接都可下载。
- 平台公开的流质量决定上限；转换后的 MP3 码率不等于来源音质，不宣称升码率可恢复原始音质。
- 网易云 playlist/detail 的 trackIds 得到 14 项，再分批通过 song/detail 补齐，实测返回 14 首完整详情。保存原始歌曲对象，主字段映射到标签与曲库。
- 请求封装参考 [yt-dlp 网易云适配器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/neteasemusic.py)；扫码入口参考 [二维码 key](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/main/module/login_qr_key.js) 和 [二维码确认](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/main/module/login_qr_check.js)。没有部署第三方代理或匹配其他平台的同名音源。
- 原生二维码生成成功，轮询返回等待扫码。登录由用户用手机完成，MUSIC_U / __csrf 只通过 keyring 保存到本机钥匙串，不保存到 SQLite、日志、代码或截图文件。
- 未登录测试歌单前两首：下载地址端点要求登录，播放地址未返回完整 URL。需要用户扫码后的真实会话才能验收会员完整下载；有会员不保证每首歌的购买或地区权限。
- 取得音频后检查试听标记与实际时长；缺失地址、试听、下载中断、时长不符均报错，不作为入库成功。按返回格式解码或转码，再写 ID3 和封面。

### 扫码账号实测遇到 8821

用户确认后，本机真实状态按 801 → 802 → 8821 返回。清除旧 Cookie 后仍发生，故不能把 Cookie 调整写成问题已解决。上游[同类问题 #26](https://github.com/chaunsin/netease-cloud-music/issues/26)明确记录 8821 为“需要行为验证码验证”。App 已映射为具体说明并停止轮询，不继续让用户反复扫码。

尝试 Qt WebEngine 承载 music.163.com 官方网页登录，但二维码持续停在加载中，用户也确认无法显示。该备用入口已移除，未取得登录会话，不能记作登录成功。后续浏览器自动检查被工具的站点安全策略禁止；已停止，不通过其他浏览器或间接执行绕过。登录与会员完整音频下载仍未通过验收。

## 10. 2026-09-25 · NCM 本地转换与 BPM 实现

重新读取用户 [NCM-Swift 容器核心](https://github.com/Asumer14/NCM-Swift/blob/master/src/lib/ncm/ncm.ts) 与 [元数据处理](https://github.com/Asumer14/NCM-Swift/blob/master/src/lib/ncm/utils.ts)。Python 实现保留其格式字段 / 掩码规则，增加长度上限、分块提取、源文件稳定性和实际格式验证；不沿用元数据 format 作为最终判断，也不发起 albumPic 网络请求。MP3 只补缺失标签，FLAC 转 MP3 时从原字段恢复多艺人、歌词等信息，完整源字段另存来源记录。

BPM 使用 [librosa 0.11 beat_track](https://librosa.org/doc/0.11.0/generated/librosa.beat.beat_track.html) 对每个片段估计节拍，再从节拍跨度计算候选速度，缓解帧间隔量化误差。对节奏强度、节拍间隔与片段一致性做保守筛选；这些筛选是应用启发式，不是概率校准。[tempo 文档](https://librosa.org/doc/0.11.0/generated/librosa.feature.tempo.html)说明全局估计与逐帧估计有区别，因此当前最多三段采样不能保证检测整首歌每一次变速。

已测试的 Python 3.13 组合：librosa 0.11.0、NumPy 2.5.3、SciPy 1.18.1、Numba 0.67.0。处理进程只写临时 PCM / MP3，不连接音频设备；真实音乐仍须以后由用户确认节拍层级和听感。合成容器不替代真实下载 NCM 兼容性验证。

## 11. 2026-09-25 · Serato 4 本机结构与 Mac 打包

- 通过已安装 App 的 Info.plist 重新核对版本为 4.0.9。本机存在 `~/Library/Application Support/Serato/Library/master.sqlite`、`root.sqlite` 及旧 `_Serato_` 数据，不能直接假设旧版 `.crate` 写入即完成新版同步。
- 仅复制 master/root 的 SQLite、存在的 WAL/SHM 到忽略提交的 `work/serato-research/` 后打开副本；没有修改真实 Serato 库。两份副本 `quick_check=ok`、`user_version=202`，root 与 master 的 Serato Library space ID 不同，存在 revision、portable_id、映射表及触发器。
- 上述逐文件副本适合结构研究，不是运行中跨库一致性备份证明。尚未取得有效的隔离 Serato 写入/重开/恢复证据，因此本轮不实现真实曲库写入按钮。
- Serato 官方[迁移与备份说明](https://support.serato.com/hc/en-us/articles/202305054-Backing-up-and-moving-your-library-to-a-new-computer)要求同时关注音频、`_Serato_` 和 `Application Support/Serato/Library`；官方[自动备份说明](https://support.serato.com/hc/en-us/articles/227561667-Library-Auto-Backup)将新版库备份与旧版本区分。后续适配不能仅备份一个旧 crate 文件。
- 开源 [dub 格式文档](https://github.com/kLOsk/dub/blob/main/docs/spec/LIBRARY-FORMATS.md)记录旧二进制 crate 的标签、UTF-16BE 和条目顺序，可用作旧格式参考，不构成 4.0.9 写入兼容性证明。本轮交付的 M3U8 / CSV / JSON 是通用 Set 导出，未声称 Serato 可导入。
- Mac 打包参考 PyInstaller 的[冻结运行信息](https://pyinstaller.org/en/stable/runtime-information.html)与[Mac bundle 构建](https://pyinstaller.org/en/stable/usage.html#building-macos-app-bundles)。冻结后 `sys.executable` 是应用启动器，因此 BPM 改用显式 `--bpm-worker` 分支；数据路径通过包内资源定位，FFmpeg 优先使用随包二进制。
- 构建输出目前面向本机 Apple Silicon，使用 ad-hoc 签名，不含 Developer ID 公证。打包后的联网账号与其他设备兼容性仍需分别验收。

### 本轮网易云边界

没有新的账号验证证据。先前的 8821 行为验证和工具站点安全禁止仍然有效；本轮没有改用其他浏览器、代理、Cookie 提取、原始请求等方式规避。歌单订阅状态机用注入列表/模拟账号离线测试；只有本地目录自动接收已完成文件到界面的闭环，不能将其标为网易云歌单自动下载已成功。

## 12. 2026-09-26 · 登录上游线索与功能调整

用户明确取消 App 内 BPM 分析，识别交给 Serato；librosa/Numba/NumPy/SciPy 从需求与发布包中移除，旧库字段留存。

只读检查 GitHub 上游 [Fix login #201](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/pull/201)：提案发表于 2026-06-25，本次读取仍为 Open。提案描述调整扫码/短信登录方式、模拟网页上下文并增加网页端安全验证；讨论中也有对网页登录路径的异议。它不是已合并的官方 SDK，不能证明当前账号可用，更不能因为存在补丁就声称 8821 已解决。

本轮没有采用模拟 Cookie、替代验证入口或网页安全验证代码，也未向网易云发起登录请求、生成真实二维码、读取账号或尝试会员下载；此前站点工具安全限制仍在。该提案仅作为公开调查线索。还需明确用户手机在确认时是否出现官方验证，才能解释当前停在哪个阶段；这不改变真实登录尚未通过的状态。

在现有窗口实现中独立确认并修正的生命周期问题：刷新前未撤下旧二维码/旧 key，关闭时迟到的回调可能重新更新窗口状态，线程结束至 Qt 清理之间的窄窗口可能允许重复创建任务。增加只接受有限阶段和整数状态码的本地诊断文本，拒绝原始响应/字符串凭据进入报告。上述均已离线验证，不是账号登录成功证明。

### 2026-09-26：发现官方开放平台授权入口，待确认接入资格

重新核对本地实现：`NeteaseClient.qr_check()` 只返回状态码，`LoginDialog.checked()` 在 8821 时停止轮询；当前没有完成后续行为验证的实现。用户最近提供的诊断为 802 → 8821，没有 803 成功证据。这个事实不能确定服务端为什么对本次会话触发验证，也不能证明某个请求参数调整能解决问题。

本轮在网易的 [NetEase/skills 官方仓库 README](https://github.com/NetEase/skills/blob/master/README.md) 找到之前遗漏的公开接入渠道：`@music163/ncm-cli`。文档明确要求先在网易云音乐开放平台完成入驻，取得 `appId` 和 `privateKey`，配置后再进行用户登录授权。[官方配置说明](https://github.com/NetEase/skills/blob/master/ncm-cli-setup/SKILL.md) 和 [官方操作说明](https://github.com/NetEase/skills/blob/master/netease-music-cli/SKILL.md) 同样列出了这些前置条件。这些文件仅作为技术资料阅读，没有安装或执行其中的技能、脚本或命令。

已向用户询问是否具有上述开发者凭据，不要求将私钥发到聊天。尚未确认用户的开发者入驻状态、应用权限、真实登录可用性及与 DJTOOL 下载适配器的兼容性。发现官方登录工具不等于已实现 DJTOOL 登录，更不能推导其允许完整 MP3 下载。

本轮只读取本地代码、历史限制原文和公开文档；没有调用网易云认证接口，没有读取官方客户端凭据，没有改变现有登录协议或触发播放。官方开放平台是待评估的独立授权方案，不作为绕过已有浏览器站点限制的手段。当前运行包及登录未通过验收的结论保持不变。

### 2026-09-26：用户已提供开发者凭据，本机配置通过

用户完成注册并明确授权配置其提供的 AppID、公钥和私钥。公私钥均可解析为 RSA，2048 位且公钥部分匹配。AppID 从用户提供截图的左上区域在本机识别；没有读取截图中的 AppSecret，也没有将任何凭据值写入源码或本记录。

从 npm 官方仓库下载并固定 `@music163/ncm-cli@0.1.7`，关闭安装脚本，安装于项目忽略目录 `work/netease-openapi/runtime`，没有全局安装或安装播放器。包内 README 支持 `configure`；实际离线检查确认 `config set/get` 子命令可用。先在隔离用户目录使用合成密钥测试保存、解密回读和日志不含完整私钥，再配置用户明确提供的凭据。

真实配置通过匿名 stdin 管道传入本地 CLI，不把密钥作为系统进程参数或 shell 命令。官方 CLI 保存到 `~/.config/ncm-cli/credentials.enc.json`（0600），对其解密读取结果在内存中比对成功；同时写入并回读 macOS 钥匙串 `DJTOOL.NeteaseOpenAPI / developer-credentials` 成功。检查 CLI 配置目录未发现完整明文私钥。结果报告 `work/netease-openapi/configuration-status.json` 只包含校验布尔值和版本号。

配置进程禁止全部网络和子进程调用。另用合成配置进行离线登录探测，只观察到官方 CLI 尝试访问 OAuth2 匿名登录等目标，所有连接均在发出前阻止。这不是一次实际登录验证。未取得用户访问令牌、未验证 AppID 服务端权限、未将 OpenAPI 账号接入现有 DJTOOL Cookie 下载适配器；未使用官方客户端 Cookie，也未执行播放。该节点仅验收本机开发者凭据配置。

## 13. 2026-09-26 · 官方账号授权实测成功

用户明确要求将其已注册的开放平台应用接入 DJTOOL。此轮使用独立的官方 OAuth/OpenAPI 授权流程，网络调用经过工具审批；不访问此前浏览器策略禁止的页面，不读取网易云客户端 Cookie，也不复用旧网页会话。公开开发者文档页面无法由检索工具打开后，没有绕过该错误；协议依据网易发布的 `@music163/ncm-cli@0.1.7` 包和前述官方仓库。

对该公开 npm 包仅进行离线语法树分析，提取参数构造与签名算法：公共参数按名字排序拼接、RSA_SHA256 签名；实际账号令牌仅交给固定 `https://openncm.music.163.com` 的授权接口。以原生 Python 接入，发布包不需要 Node/CLI，也不继承 CLI 输出原始响应的日志行为。

实际接入先遇到两个独立问题：自定义 os/channel/brand 被官方接口以 HTTP 400 明确拒绝，随后按官方 SDK 的协议标识修正；官方二维码返回 HTTPS `163cn.tv` 短链，被初版仅接受 music.163.com 的本地校验拒绝，扩展为接受官方短链后成功显示。DJTOOL 不跟随此短链，只将官方返回内容画成二维码供手机扫描。

真实结果：用户扫码并在手机确认，轮询得到 803；继续调用官方账号接口验证成功后，才保存授权到 macOS 钥匙串并显示已连接。用户也明确回复“已确认授权”。新建独立进程从钥匙串恢复会话，再次调用官方账号接口成功。证据为 `work/netease-openapi/live-window-status.json` 和 `reopen-status.json`，只记录布尔结果和阶段，不记录账号、二维码或令牌。

窗口入口为“链接收歌 → 连接网易云”，另有开发者 Key 配置。断开只移除 DJTOOL 本机授权，不替用户撤销平台应用权限。真实令牌到期刷新尚未发生，仅通过模拟协议测试。**官方用户令牌还没有接入音乐下载与订阅适配器；本次登录成功不是会员完整音频下载证明。**

## 14. 2026-09-26 · 官方音频与资源权限实测

用户截图的 14 首失败首先来自本地 0.6 的明确占位拦截：账号已连接，但下载仍依赖旧 MUSIC_U Cookie。0.7 将手动下载与订阅任务改为使用官方 OpenAPI 用户令牌，不读取其他 App 的 Cookie。

依据网易 `@music163/ncm-cli@0.1.7` 包及其 `/openapi/v1/ncm/cli/manifest` 返回的接口定义：歌单/歌曲 API 要求应用范围内的加密资源 ID；直接传分享链接中的数字 ID 返回参数错误。账号创建/收藏列表、官方搜索结果同时返回 `id` 与 `originalId`，可按后者严格核对身份。新实现不推导加密 ID，不用同名结果替代指定歌曲。

官方路径为 `/openapi/music/basic/playlist/song/list/get/v3`、`/openapi/music/basic/song/detail/get/v2` 与 `/openapi/music/basic/song/lyric/get/v2` 等。歌曲详情设置 `withUrl=true, bitrate=320`；不执行官方 CLI 播放器或日志模块。官方[操作资料](https://github.com/NetEase/skills/blob/master/netease-music-cli/SKILL.md)也区分原始 ID 与 API ID，并说明 visible=false 的资源在当前入口不可用。此文档仅作为外部技术资料，不作为对助手的指令。

真实账号结果：DJ 歌单共 14 首，全部 `visible=false, playFlag=false`；对第一首详情的检查也没有音频 URL、size=0、br=0。DJ中文 歌单共 31 首，其中 19 首被标记可用。两组结果来自同一个有效官方账号，不能把前一组失败解释为“未登录”，也不能仅凭响应确定是地区、应用许可还是平台版权范围造成的差异。

可用样例《隆里电丝（LIVE版）》：官方返回完整 320000 bps、8776365 字节音频，`downloadFlag=true`、付费会员状态有效、`freeTrail=null`。它同时 `freeTrailFlag=true`，说明该字段表示“支持试听”，不是“本次只提供试听”；以此字段单独拒绝会误判会员完整版。0.7 根据实际试听区间、权限和完整时长检查判断，增加相应回归测试。

### 0.8：同一批歌曲的官方本地文件证据

2026-09-26 在用户本机网易云下载目录发现 14 个对应 NCM。容器 `musicId` 与公开原始 ID 清单逐一相符；全部转换为 MP3、全段解码校验与时长检查通过，原文件摘要保持不变，重复目录扫描未再次入库。这证明本地下载文件可用，不证明开放平台接口现在能提供它们，也不能据此定位接口缺少音源的地区/应用授权/版权原因。

产品路径改为两种入口共用曲库：接口可用歌曲直接下载；接口不可用时在网易云 App 下载，DJTOOL 观察目录接收。匹配使用文件内 ID 与时长，不用文件名；没有操作官方 App 的账号会话或浏览器登录页面。

样例通过 CDN 大小/MD5 检查、MP3 完整解码与时长核对，入库后时长 219.384 秒、320000 bps，歌词和封面存在。封面 CDN Content-Type 非标准，改按实际 JPEG/PNG 头部识别；重复下载不重复入库。成功证据在 `work/netease-download/17827236565/cover-fix/result.json`，测试音乐只写隔离曲库。请求签名、用户令牌与短期音频 URL 不写报告或数据库。真实远端歌单新增后自动入库尚未验证。

## 15. 2026-10-04 · Netease_url 本机接入研究

用户提供 [wyapi.toubiec.cn](https://wyapi.toubiec.cn/) 后，核对其链接指向的 [Suxiaoqinx/Netease_url](https://github.com/Suxiaoqinx/Netease_url)，固定检查版本为 [`da2c3d4cfc11eef982be360bbb89426b30a1fd71`](https://github.com/Suxiaoqinx/Netease_url/tree/da2c3d4cfc11eef982be360bbb89426b30a1fd71)，提交时间 2026-09-25 23:17:26 +0800。该版本 [README](https://github.com/Suxiaoqinx/Netease_url/blob/da2c3d4cfc11eef982be360bbb89426b30a1fd71/README.md) 明确区分 MIT 开源仓库与作者独立开发的闭源线上演示站。用户随后明确选择只接入本地开源直连，不使用演示站的远程解析服务。源码改编位置为 `netease_local.py`，上游来源说明与许可证保留在 `third_party/netease_url/NOTICE.md`、`third_party/netease_url/LICENSE`。

依据固定版本 [music_api.py](https://github.com/Suxiaoqinx/Netease_url/blob/da2c3d4cfc11eef982be360bbb89426b30a1fd71/music_api.py)，DJTOOL 以 Python 原生适配器调用网易接口，不运行上游 Flask 服务、不加入其完整旧依赖清单。歌曲详情与歌单使用 `api/v3/song/detail`、`api/v6/playlist/detail`；音频地址使用 `interface3.music.163.com/eapi/song/enhance/player/url/v1`。适配保留上游 EAPI 的 JSON 序列化、MD5 请求摘要与 AES-ECB / PKCS7 参数编码，并使用项目已有 Cryptodome。歌单先取得完整 `trackIds`，再每 100 首请求详情。

上游定义 standard、exhigh、lossless、hires、jyeffect、sky、jymaster、dolby 等等级；本轮界面提供 320 kbps、无损、Hi-Res，默认请求 exhigh。请求等级仅表达期望，实际格式、码率及可用性以网易返回为准；得到音频后仍走现有 MP3 解码或转码流程，不能把较高请求等级当作实际音质证明。歌曲/歌单列表读取与完整音频取得继续分开验收。

上游 [Cookie 与下载实现](https://github.com/Suxiaoqinx/Netease_url/blob/da2c3d4cfc11eef982be360bbb89426b30a1fd71/music_downloader.py) 直接读取 `cookie.txt`，DJTOOL 不采用该方式。仅接受用户主动提供的 MUSIC_U / 可选 __csrf，验证账号后保存至 macOS 钥匙串，沿用 DJTOOL 自身会话位置；不读取其他应用或浏览器会话、不写 Cookie 文件、SQLite 或日志。账号请求只发送至固定网易接口，禁止携带会话跟随跳转；短期音频地址只在内存中使用。

对照上游补充了适合曲库与队列的边界：严格验证歌曲/歌单 ID，不以同名结果替代；按原 trackIds 顺序组装详情；校验 trackCount、详情数量、重复或错位 ID，拒绝把部分响应更新为完整接收基线；检查试听区间与完整时长，拒绝将片段入库。下载复用 DJTOOL 的隔离暂存、大小/MD5 校验、实际时长与全段解码检查、内容及平台 ID 去重、现有入库事务，避免沿用上游按歌手歌名判断文件存在、直接写最终播放文件的方式。失败或取消不应留下半份播放文件，也不应产生成功队列状态。

已连接的官方 OpenAPI OAuth 授权与本次 MUSIC_U 会话属于不同协议：OpenAPI access token 不能当作 MUSIC_U 使用，也不能由本机适配器转换得到。两种会话分别保存，音源选择明确决定使用哪一种；此前官方授权成功不证明本机会员会话已连接。本轮公开协议和本地实现检查已完成，真实账号下的完整音乐取得、入库与试听听感仍待实测；不把匿名列表读取或模拟测试写成会员下载成功。

### 15.6 本机联网结果（2026-10-04）

使用本机适配器、匿名会话、项目内隔离曲库执行 `scripts/qa_netease_local_live.py <个人歌单 B> --download 1`：DJ 歌单 14 首完整读取并保留原顺序；首曲 1900149187 没有返回完整音频 URL，下载结果为 waiting_auth。另六首公开歌曲详情均返回有限正时长，但匿名 player 接口同样未提供完整 URL，包括元数据 fee=0 的样例。这只证明匿名当前接口结果，未能定位账号 / 地区 / 接口限制的具体原因。

本轮没有读取真实 MUSIC_U。会员会话验证与真实完整音频入库待用户在新设置窗口配置后实测；已有官方 OpenAPI 成功不替代该验证。33 项本机协议 mock 测试、真实合成 MP3 / FLAC 解码入库与 UI / 包内自检分别记录在 PROGRESS，不能用它们宣称任意网易云歌曲可完整下载。


## 2026-10-04 · 网页下载与本机开源入口重新核对

用户确认免会员下载成功发生在 wyapi.toubiec.cn 网页，随后再次选择仅本机直连。上游 README 在本机部署说明中要求 Cookie，并明确在线站采用不同闭源实现，不能把两者视作同一音源能力。证据：https://github.com/Suxiaoqinx/Netease_url/tree/da2c3d4cfc11eef982be360bbb89426b30a1fd71 。没有复制上游示例凭据，没有调用在线站 IP 握手。

本机匿名两个样例 1900149187/29722582，标准/极高、数组/字符串 ids、MP3/FLAC 与 interface/interface3 组合返回 per-song 404 且无 URL；资料在 work/netease-local/anonymous-protocol-2026-10-04.json。证明当前样例未取得完整音源，不证明所有歌曲永远不可下载。账号入口移除，默认 use_keyring=False，旧偏好一次迁移为匿名本机，保留音质/已完成任务。

### Serato 4.0.10 当前实际存储

音乐在 Documents/Serato；活动 Crate 在 Music/_Serato_/Subcrates，当前库 schema 202 在 Library/Application Support/Serato/Library。文稿内历史 _Serato_ 与活动库不等同。官方当前说明将 Subcrates 列为当前版本主要列表存储：https://support.serato.com/hc/en-us/articles/204022904-What-is-in-the-Serato-folder 。

使用复制的原生 SQLite/Crate 和三首合成静音，CFFIXED_USER_HOME 指向实验副本，sandbox 限制真实目录和联网。原生创建、独立重启、改序、再次重启、恢复均通过读回 schema 202 的成员/顺序核对；五份快照和日志保存在 work/serato-adapter/native-eda987b9。真实数据库/Crate 摘要持续不变。由于 AX 关闭不可用，用明确所属进程 SIGTERM 后独立重启验证；不计为原生菜单正常退出验证。

初版隔离配置未阻止偏好 daemon 写入，真实 com.serato.seratodj.plist 时间变化；无法基于时间推断具体值或恢复未知旧值，未盲目恢复。增强配置禁 user-preference-write/账号服务访问后，后续启动该文件摘要保持不变。原生 Serato 没有读取/播放真实音频；老副本中的缺失文件标记来自禁止真实文件读取，不污染真实库。


额外原生空 Set 测试：空 .crate 后 Serato 4.0.10 仍保留 3 个成员，phase/empty 快照与 native-empty-report.json 记录失败。生产 sync_set 已明确拒绝空列表，以免把暂未同步的清空报成功；此边界不影响已通过的非空成员改序。

实际文件异常：afro/love nwantiti (ah ah ah) - CKay.flac 的 ffprobe/Mutagen 识别为 320kbps MP3，并含无效帧。严格完整 ffmpeg -xerror 解码失败，宽容解码仍出现多个包错误，未改扩展名/转码或放宽规则；原 SHA256 未变。其余 746 首仅证明原路径读取/索引及文件保全，未据此声称逐首完整解码与听感验收。

## 2026-10-04 · 用户改选在线站

当前 [wyapi](https://wyapi.toubiec.cn/) 的公开请求合同是 nextmusic.toubiec.cn POST JSON：/api/ip 内存握手，之后 timestamp/ip + 参数；playlist_trackall 以 limit=500/offset 分页，数据带 songCount。网页十首打包是 list.slice((page-1)*10,page*10) 后逐首请求并在浏览器 ZIP，接口本身没有十首限制。只据协议原创适配，不把闭源网站当 MIT 源码复制。

真实公开 DJ 歌单<个人歌单 B> 返回14首元数据；1900149187/29722582 的 info/url 返回200，媒体为 HTTPS m801.music.126.net，带 size/md5/br/level。duration 为 M:SS；音源响应包含 cookie，必须直接丢弃且不把原始响应落盘，不携带至 CDN。当前代码还核对原 ID、音频 time、大小/摘要/全段解码，媒体临时地址只在内存。完整媒体尝试被自动审批拒绝（公网IP至具体目的地未明确授权），未绕过，尚不作为下载成功证据。脱敏报告及公开资源摘要：work/online-service-research/INTERFACE.md、public-probe.json。

### 同日故障歌单与真实媒体验收

用户明确允许向nextmusic.toubiec.cn提交歌单/歌曲ID和服务检测的公网IP，用于读取及下载验证；不提交账号/Cookie。歌单<个人歌单 A>「我喜欢的音乐」3页共1315个唯一ID，其中9条duration末位秒为60，例如1310241345「Hit The Road Jack」1:60、2030602005「VVSane」2:60。旧严格秒范围导致整单失败；按进位换算后全量元数据通过。脱敏完整列表/字段证据在work/netease-site-fix/。

实查getSongInfo.time与getSongUrl.time为当前响应生成日期（例如2026/10/05 02:47:47），不是音频时长或发行日期。此前把time当数值毫秒导致下载拒绝，也不能把该日期写发行标签。0.11.4严格识别此日期格式，完整音频仍对照fresh duration，并核对实际文件大小/MD5/解码时长。两首在隔离库真实下载exhigh/320kbps，解码时长119.876/179.664秒，封面和歌词存在、重复同ID不再次下载；result.json passed=true。未播放、未读账号、未写用户Serato，也未把两首成功扩大为1315首全部音源可用的结论。


### 2026-10-04 原生整批下载与站点冷却

正式库原生窗口已实际新增截图前11首歌曲，前5首全部完成320kbps完整音频校验/解码/待整理入库；随后 /api/getSongInfo 触发 HTTP429/code429，站点给出约37秒 retryAfter。这是短请求窗口限流，不能把余下1300首当全部无音源。采取给定冷却结束后续跑、同一出口、可取消且有反复限流上限；没有换IP或跳过等待。并行冻结隔离的 service HTTP404 曾出现在2166679150与2652856010，但原生稍后取得二者，故不将HTTP404永久归类为无音源或推断改音质即可解决。旧1312次通用错误没有保存原始类型，历史根因保持未知；重启后的实际下载与受限运行环境的即时拒绝对照分别存证。
# 2026-10-04 Serato BPM 只读接入

用户希望把 Serato 已有的 BPM 读回 DJTOOL 排歌。本机 Serato 4.0.10 的原生 schema 202 `root.sqlite.asset.bpm` 已实测可读，与现代主库相同记录一致；700 首现有播放路径与大小对应，运行 reader 接受698首，跳过2条 Serato corrupt 标记。195首与原标准标签数值不同，包含数据库79.68/标签80的小数精度差异。BPM 来源标为 Serato，不能据此推断每首都刚被 Serato 计算。

读取默认根卷库 `~/Library/Application Support/Serato/Library/root.sqlite`，只复制 DB/WAL 到私有副本后查询，不打开真实库作 SQLite 写入。按原播放路径、音频类型、实际大小及有效状态匹配，拒绝未知结构、热日志和不稳定快照；仅缓存到DJTOOL。外置卷、流媒体和旧 database V2 不套用本机根卷路径规则。实际证据在 `work/serato-bpm/readonly-live-evidence.json` 与 `reader-live.json`，读前后真实 Serato 库 SHA256 保持一致。

官方资料：[Analyzing Files](https://support.serato.com/hc/en-us/articles/14361068095759-Analyzing-Files)说明 Serato 分析会产生 BPM/Beatgrid；[What is in the _Serato_ folder?](https://support.serato.com/hc/en-us/articles/204022904-What-is-in-the-Serato-folder)把 database V2 标为旧版并说明其保存 BPM 元数据；[Backing up and moving your library](https://support.serato.com/hc/en-us/articles/202305054-Backing-up-and-moving-your-library-to-a-new-computer)说明现代 Application Support/Serato/Library 目录。字段和精度以本机数据库实际读取为依据。


# 2026-10-23：Serato 5 自动收歌兼容性

本机已安装 Serato DJ Pro 5.0.0，根卷 schema 为 203。既有 4.0.10 的能力记录已因安装指纹变化失效，不能直接沿用。隔离实验仅写项目 work/serato-auto-native/run-20549d05，拒绝真实 Home 读写、偏好写入、钥匙串访问和网络，三首合成静音仅用于引用，不播放。

实时副本带有生产进程 lock.owner_process_id，master.connection 还包含真实绝对路径；首次启动因此拒绝持锁或读到沙盒拒绝的连接。第二次仅在私有副本清除旧锁、将连接指向同一隔离目录，未修改源数据库。新建、重开、更新、再次重开和恢复的五个独立原生 SQLite 快照均验证三首自定义顺序，schema 203，安装摘要 b57da87e7d2b462964142be155258feb3e501350b7b7611741cdca9434089564。证据 evidence.json 与 qa-second.log 保留，不将 SIGTERM 结束所属测试进程当成正常菜单退出验收。

官方 [Adding files to the Serato DJ Pro Library](https://support.serato.com/hc/en-us/articles/223446528-Adding-files-to-the-Serato-DJ-Pro-Library) 说明 Serato 引用原文件位置；[_Serato_ folder](https://support.serato.com/hc/en-us/articles/204022904-What-is-in-the-Serato-folder) 将 Auto Import 和 Imported 标为旧流程，后者会移动文件，因此本次不把稳定音频路径改成 Auto Import 目录。实际兼容性以本机五阶段原生接收证据为准。

## 2026-11-21 · GTA 6 Rap Radio 缺歌复查

现用站点完整返回歌单 18623651533 的 77 个唯一歌曲 ID。正式库初查对应 59 个现存播放文件，另 18 首记录为空音源，当前范围为全部。对同一站点、同一歌曲 ID 的实测中，SICKO MODE（1298432425）在 exhigh、higher 返回空地址，在 lossless 返回 34,159,257 字节、873,581 bps，在 standard 返回 5,006,359 字节、128,000 bps。Don’t Chart（3463218077）此前在正式库无源，本次 exhigh 返回了地址，lossless 却为空。由此只能判断各档与不同时刻的可用性不同。返回地址与最终播放文件的验收分别记录。

补测最后三首时，详情接口返回正确 ID，但相同站点的音质接口可能以 HTTP 404、JSON code 404、data null 表示当前无结果。对其他音质或稍后再取可返回音源。程序仅对音质端点的这一结构化响应换档。最终在正式库完成全部 77 首的逐文件解码复核，见 PROGRESS。
