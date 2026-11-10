# dev：播放上下文和生命周期补齐

2026-11-09 后续修订：以下当前行为已随 [A01–A14 逐项优化](DEV-A01-A14逐项优化.md) 更新；验证记录保留各轮独立口径。

依据 2026-11-09 18:33～18:36 官方 TV 单次采集（项目外 `research/tv-single-playback-20261109`）。本次只修改本地代码并做离线验证，没有启动项目公网播放、提取代理或重放官方抓包。

## 自动生效的修正

- click 增加 `statistics`，来自本轮 client.common，与该客户端的 build 一致。
- heartbeat 增加 `fourk=1`，与本项目取流能力配置一致；它不代表实际视频质量为 4K，quality 仍使用响应返回值。
- extra 的 teenager_mode 使用本轮实际配置，不再独立写死为 0。
- Inspector 将 HTTP 200 / code=14013 计入接口错误；保存身份更新不等于 click 已被接受。历史 summary 不重写。
- 客户端在创建时保存本轮 UA 和公共参数；显式上下文不会修改全局设备模板或另一个客户端。

## 显式上下文

新增 `--context-file <JSON文件>`。建议将真实上下文文件放在忽略目录 `.private/`。非空上下文必须指定与设备配置完全一致的 `profile_id`，不匹配会在开始请求前拒绝。

除 profile_id 外，现在支持 binding 校验安装、Buvid 摘要和 client_slot；非空 live 上下文必须绑定。guest 必须有来源、有效期和手动更新契约，且目前只允许 demo；官方刷新协议仍未知。校验通过不证明用户输入来自真实设备。没有来源的字段继续省略，不把抓包值固化为默认值。

| 分组 | 可传入内容 | 使用位置 |
|---|---|---|
| device | cpu、memory（正整数 MiB）、tv_brand、mpi_model、mpi_id、mpi_type | TV GET/FORM 公共字段 |
| scene | teenager_mode、child_lock、mode_switch、first_start、from_out、explor_attr、top_speed、product_top_speed、resource_from、resource_id | 公共字段；适用字段同步到 extra |
| guest | guest_id 与 guest_access_key，必须成对且非空 | GET/FORM 公共字段；guest_id 同步到 extra |
| playurl | ad_type/ad_extra、disable_ad、drm_tech_type、from_outside、is_global_preview_end、is_portrait、is_switch_qn、ogv_aid、play_model、play_scene、preview、preview_end、preview_from_guest | 仅取流 |
| report | spmid、from_spmid、track_id、polaris_action_id、auto_play | click/heartbeat；取流将 from_spmid 映射到该接口的 fromspmid |
| extra | internal_track_id、ott_track_id、session_more、is_continuous_play、is_preview、ogv_copyright_index，以及资源/分区/权益字段 | 各接口的 extra；click 不加入 heartbeat 专用的 from_spmid 和 ogv_copyright_index |
| history | scene、from_scene、is_interacted、rtime、history_ab_184 | 仅 history/add，必须按实际场景提供 |

extra 的 session_id 来自本轮请求 session，不能通过配置覆盖；build、statistics、aid/cid、播放时长和身份字段也不能由这些分组覆盖。scene/extra 同名资源字段不一致时拒绝。

输入最多 64 KiB，单值最多 4096 字符，拒绝未知字段、控制字符，以及会破坏当前 click 原始键值对或单引号 extra 结构的 `&`、单引号、反斜线。复杂上下文若需要这些字符，必须先确认官方转义规则，不能自行猜测后发送。

公开 state 只保存已配置的字段名，不保存上下文值；真实发送记录仍在原有 `.private/` 日志中。携带 context-file 的 debug_worker 不接受逐行 trace/单步，避免字符串中间值绕过字段脱敏。

固定配置继续复用其既有 Buvid、DID 和 ftime；新播放 session 每次更新。动态生成配置仍代表新合成设备，不能套用其他 profile_id 的上下文。没有新增“轮换身份以绕过限制”的行为。

## 可选 online/history 机制

新增 `--tv-lifecycle`，用于一次播放调用，默认关闭。`tv_playback.py` 的 play/直接视频 ID 入口及 `debug_worker.py` 支持这两个开关；页面现支持 demo 单路单次内置上下文和生命周期，后端同步限制，不开放任意私有路径。直接入口除本地 report 外必须显式指定 mode。

启用后：

1. 首个有效解码进度后提交一次 `/x/tv/playurl/online`，随后执行既有开始 heartbeat、服务器时间查询、click。
2. 解码进度处理过程中，每约 30 秒提交一次新的 online；晚到的进度不会补发一串遗漏周期，结束块不再提交周期 online。该调度依赖进度事件，等待/阻塞期间不是独立后台计时器。
3. online 虽是 GET，但视为状态更新，只做一次传输尝试。一次失败或未确认后停止本轮后续 online；不回放该请求。
4. 首帧已出现且解码清理结束后，优先执行原有结束 heartbeat；仅正常完成再提交一次 `/x/tv/history/add`，异常退出记为 skipped。`play_duration` 使用受墙钟约束的解码时长估计，`pro` 使用媒体进度；两者独立传入，不能一概当成同一时间。history 不重试。
5. 每条附加事务在发送前落盘为 unconfirmed，收到 code=0 后才变为 accepted；强制结束可能留下未确认记录，不会自动重放。
6. `lifecycle_events`、`lifecycle_status` 单独保存；附加事务失败会让 report_status 保持 unconfirmed、整轮标为 failed，但保留已完成的 media_status。主要媒体/原有上报错误优先，不会被附加事务错误覆盖。

history 当前实现顺序 UGC 解码的结束快照，自动加入 aid/cid、epid/sid=0、business=archive、tp=3、fourk=1、play_duration/pro；未确定语义的 rtime、history_ab_184、live_room 等不会照抄抓包值。可明确配置的场景项见上表，live_room 未提供覆盖入口。

启用 lifecycle 时 history 单独预留 5 秒，结束 heartbeat 最多使用自己的 5 秒并保留 history 预算；请求前检查预算，不足记 skipped/insufficient_budget。未开启时沿用原预算；不延长租期或额外提取代理补发。

## 本地离线用法

下例使用明确标注的合成上下文、生成的音视频和本机媒体服务；协议请求由离线 transport 接管，不访问公网。每次选择一个尚不存在的 run-dir。

```powershell
.\.venv\Scripts\python.exe -B debug_worker.py --mode demo --seconds 3 --device-profile offline-android9 --client-slot context-check --context-file examples/playback-context.offline.json --tv-lifecycle --run-dir results/context-offline-check
```

示例文件中的 `offline.fixture.*` 与 `offline-fixture-track` 仅用于离线验证，不是官方页面来源或官方跟踪标识。真实上下文入口必须使用实际来源的数据，不能复用这个示例冒充官方场景。

## 尚未覆盖

- 游客凭据的官方初始化与刷新；已增加本地有效期和安装契约校验，但不代表实现官方凭据生命周期，没有自动提取模拟器凭据。
- x-bili-trace-id、aurora、广告 SDK 和二进制遥测的生成流程。
- 官方页面导航、真实播放器暂停/seek/后台恢复。已有独立离线时间模型，尚未接入 FFmpeg；调试器“暂停派发/逐行暂停”不等于播放器暂停，真实顺序路径 paused_time 仍为 0。
- 正常退出、后台、恢复时官方 history/heartbeat 的完整异步规则。当前 finally 不是 Android 生命周期的等价实现，force-stop 无法保证收尾提交。
- 公网接受性与公开播放量。离线通过不等于这些新增可选事务已经通过服务端验证，也不能据此声称此前网络失败已修复。

## 验证记录

- Python 回归覆盖上下文路由与隔离、拒绝协议身份覆盖、错误脱敏、online GET 不重试、延迟周期不补发、结束 history 不重复、身份持久化和失败统计。
- 6 项 JavaScript 会话测试通过。
- 完整本地 DASH 运行：3 秒、75 帧、有音频，media/report/lifecycle 均 completed；一次开始 online、一次 click、两次 heartbeat、一次结束 history。
- 验证材料在项目外 `research/dev-context-validation-20261109`。完整 Python 测试主进程装有仅允许本机地址的 socket 审计钩子；离线 worker 另有其禁止外部 Python 网络的 transport/审计保护，FFmpeg 只读取本地生成媒体。
- 源码与文档留在现有 dev 工作区，未提交；没有重启旧观察服务或更改其当前任务。
