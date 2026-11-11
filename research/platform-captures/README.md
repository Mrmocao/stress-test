# 平台接口与参数采集（2026-11-11）

本目录是从 REA MCP 证据提取的公共参数目录，不是原始抓包，也不是可导入 REA 的 canonical Evidence bundle。只保存接口路径模板、参数名称、HTTP 状态、采集次数及证据引用；不保存参数值、原始用户/设备 ID、IP、Cookie、签名和完整媒体 URL。

| 平台 | 本次状态 | 来源 | 播放工作进程 |
|---|---|---|---|
| TV | 44 组历史接口对照 | REA 重新解析既有脱敏 HAR | 保留现有 TV 流程 |
| APK | 等待确认手机版或 TV，未取得目标接口 | REA MCP 已检查在线模拟器，未安装上述两种包 | 未接入 |
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

## APK 待解决项

需确认新平台是安卓手机版 `tv.danmaku.bili` 还是 TV `com.xiaodianshi.tv.yst`。REA MCP 的 ADB 提供器已通过为独立 stdio MCP 进程设置已有 `REA_ADB_PATH` 恢复；无需修改全局注册。实际 `list_adb_packages` 证据为 `ev_954cb3674ec3fd0b9b9ebafde9028b494e402f2c54bb72a31b711ff5e4894f2e`，当前在线模拟器无这两个第三方应用。

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
