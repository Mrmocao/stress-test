# 平台接口与参数采集（2026-11-11）

本目录是从 REA MCP 证据提取的公共参数目录，不是原始抓包，也不是可导入 REA 的 canonical Evidence bundle。只保存接口路径模板、参数名称、HTTP 状态、采集次数及证据引用；不保存参数值、原始用户/设备 ID、IP、Cookie、签名和完整媒体 URL。

| 平台 | 本次状态 | 来源 | 播放工作进程 |
|---|---|---|---|
| TV | 44 组历史接口对照 | REA 重新解析既有脱敏 HAR | 保留现有 TV 流程 |
| APK | 17 组手机版初始化接口，播放 RPC 仍未确认 | REA APK Manifest、签名、ADB 安装检查和独立雷电模拟器网络记录 | 未接入 |
| 网页 | 29 组接口，124 次选定 API/媒体请求及其 HTTP 响应 | REA 启动独立 Chromium，匿名页面加载及自动播放启动 | 未接入 |

观察台新增“平台采集证据”选择器。切换只读证据不会更改正在运行的 TV 任务。API 显式拒绝把 APK 或网页送入 TV worker，包含离线模式。

## 网页实际观察

浏览器为 Chromium 156.0.8078.4；页面 `https://www.bilibili.com/video/BV1Gq486CEK4/`，启动后额外观察 10 秒。REA 保留 916 条事件，丢弃 0 条；其中所选 API 域及媒体模板覆盖 124 次请求。资源文件、页面、其他域不在接口目录中。REA 完整性仍为 `incomplete`，原因是部分选定请求内容不可读取。响应体未采集，HTTP 200 不等同于业务成功。

核心接口及其参数位置：

| 方法 | 路径 | 观察结果 |
|---|---|---|
| GET | `/x/player/wbi/v2` | Query 包含 aid、cid、w_rid、wts、web_location 及设备环境字段；HTTP 200 |
| POST | `/x/click-interface/click/web/h5` | Query 有 w_aid、w_ftime、w_stime、w_part、w_type、w_rid、wts；Body 有 aid、cid、ftime、stime、session、statistics 等；HTTP 200 |
| POST | `/x/click-interface/web/heartbeat` | Query 有 w_played_time、w_real_played_time、w_start_ts 等；Body 有 played_time、real_played_time、start_ts、video_duration、session 等；HTTP 200 |
| GET | `/x/v2/dm/web/view`、`/x/v2/dm/wbi/web/seg.so` | 弹幕配置与分段请求 |
| GET | `/x/v2/subtitle/web/view` | 字幕信息请求 |
| GET | `*.bilivideo.com/{media-object}.m4s` | 33 次媒体分片请求均观察到 HTTP 206；签名与媒体对象路径已移除 |

完整字段在 `web.json`。Query 与 Body 不合并成一套“公共参数”；字段并集不证明必填规则。此次未观察到独立 `/x/player/wbi/playurl` 请求，不能把它补写成实测接口，也不能断言播放地址来源于 HTML。没有从网络出现的字段反推签名、设备 ID 或票据生成算法。

两个上报表单本次均观察到非身份字段 `platform=web`、`mobi_app=web`、`device=web`、`type=3`、`sub_type=0`、`play_mode=1`；心跳另有 `dt=2`、`play_type=1`。这些是该匿名场景的观察值，不是跨版本通用常量，不应套用 TV 的 `android_tv_yst`、versionCode 或 build_sn。相关 Query 的 `w_*` 与 Body 的同名业务字段需要分别保持一致；尚未验证其全部签名参与规则。

`data.bilibili.com/log/web` 的查询部分是旧日志载荷，内部嵌套的 URL 不是该接口的参数定义。`/v2/log/web` 的不透明请求体未解码。JSON 请求体目前仅提取顶层字段名，不将嵌套值当作字段。

## 安卓手机版 APK 实际观察

官方客户端下载页的 64 位链接为 `android64/iBiliPlayer-bili.apk`，页面当日标注 9.19.0；下载文件 SHA-256 为 `33b302b285bdbb5ee1e3030c5d65a88b8229ff3d7bbf78df105600e968109bfc`，REA Manifest 读取结果为：包名 `tv.danmaku.bili`、版本 `9.18.0`、versionCode `9180300`、`BUILD_SN=30591258`、`MOBI_APP=android`、渠道为空、minSdk 24、targetSdk 35。官网文字与 APK 内容不一致，以实际文件 Manifest 为准。

同一官方页面的 32 位链接实际下载为版本 8.91.1、versionCode `8911100`，SHA-256 为 `e6f1adee9b96ce9eb79001f27ab0688b5c1712f3c635bc9d6e2db82d1a9c30c8`，本地 aapt 读取到 `BUILD_SN=24160889`，但该 32 位样本的 REA Manifest 调用因 Java 提供器启动超时未形成完整 Evidence，因此该值只作为待复核线索，不进入手机版公共构建主记录；该包在独立 API 30 x86 模拟器启动阶段崩溃，REA logcat 记录 `lateinit property crash has not been initialized`。

64 位 APK 在新建 Android 11 x86_64 模拟器上进入访客协议页但确认后无响应；在雷电独立实例中接受协议后进入推荐页和视频详情页。REA ADB 证据确认安装包版本为 9.18.0 / 9180300；独立网络记录经 REA HAR 解码保留 60 条，其中 17 组公共接口：

| 类别 | 已观察路径 | 说明 |
|---|---|---|
| 参数初始化 | `/x/report/click/now`、`/x/offline/manifest`、`/x/share/clipboardRules` | Query 出现 `appkey`、`build`、`channel`、`mobi_app`、`platform`、`statistics`、`sign`、`ts` 等名称；请求值不公开 |
| 资源与风控 | `/x/resource/fingerprint`、`/x/v2/anti_addiction/aggregation/status`、`/x/v2/version/fawkes/tribe3` | 指纹、反成瘾和灰度资源接口；Body 仅保留字段名 |
| 模块与配置 | `i0.hdslb.com/bfs/fawkes/...`、`/x/resource/peak/download`、`/x/resource/show/skin` | 配置、资源包和皮肤下载；动态发布号被模板化 |
| 日志 | `data.bilibili.com/log/mobile` | 21 次 POST；仅保留 `android` 这一公开表单名，载荷不解码 |

本次未确认独立 playurl HTTP 请求或手机版播放 RPC 的实际调用。REA 静态类证据表明手机版使用 `com.bapis.bilibili.app.playerunite.v1.PlayerMoss.playViewUnite`，请求 `PlayViewUniteReq` 含 `vod`、`bvid`、`spmid`、`from_spmid`、`from_scene`、`ad_extra`、`extra_content`、`fragment`、`play_ctrl`；其中 `vod` 的字段包括 `aid`、`cid`、`qn`、`fnver`、`fnval`、`fourk`、`force_host`、`prefer_codec_type`、`download` 等。该结果是 APK 静态声明，不能当作运行时请求已发生，也不能转写为 TV 的 `/x/tv/playurl`。

## APK 待解决项

REA MCP 的 ADB 提供器已通过为独立 stdio MCP 进程设置已有 `REA_ADB_PATH` 恢复；无需修改全局注册。早期在线模拟器的空检查证据为 `ev_954cb3674ec3fd0b9b9ebafde9028b494e402f2c54bb72a31b711ff5e4894f2e`；手机版实际安装检查使用独立雷电实例，Evidence 由 `apk.json` 的 `environment_check.evidence_id` 引用。

APK 反编译调用缺少 `REA_JADX_MCP_JAR`；签名检查虽可用，但不能替代接口运行时采集。APK 目标确认后，需要获取实际安装包、核验包名与 SHA-256、读取 Manifest 版本/渠道，再在专用测试环境采集。`versionCode`、`build_sn` 未读到时为空，绝不从版本字符串或 TV 样本填充手机版。

## 证据与复现

- 网页：`ev_5b16a22ea440a12a336f4dc50d08aca7e05cd692d4ec753376e82784c1aa25e1`。
- 历史 TV HAR：`ev_ffa9a4e4c5d7cdae63bba587d4b3a145db66b0d848e0544ce40eadf286aa66b9`。
- 原始 REA bundle 仅保留在本机私有实验目录 `E:\rea-tv-lab-20261111\platform-captures\rea-evidence-bundle.json`，不要提交或发布。
- 每个公共接口的 `observations` 指向原始事件序号或 HAR 记录序号。HTTP 状态按请求 transaction_id 对应，而不是按事件时间猜配对。

使用仓库根目录的 Python 环境重新提取：

```powershell
.venv\Scripts\python.exe tools\extract_platform_evidence.py E:\rea-tv-lab-20261111\platform-captures\rea-evidence-bundle.json --web-evidence ev_5b16a22ea440a12a336f4dc50d08aca7e05cd692d4ec753376e82784c1aa25e1 --tv-evidence ev_ffa9a4e4c5d7cdae63bba587d4b3a145db66b0d848e0544ce40eadf286aa66b9 --output research\platform-captures
```

三个平台各自维护证据；TV 的 APK 常量、Android 环境、身份生成器不应用到网页。网页采集使用浏览器自身产生的会话和请求，公共目录只提取结构。仅凭本次单场景证据，不提供跨版本稳定性或全平台完整性承诺。
