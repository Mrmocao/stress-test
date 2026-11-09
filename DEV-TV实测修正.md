# dev：TV 实测参数修正

依据 2026-11-09 官方云视听小电视 1.8.9（108900）模拟器抓包及 APK 分析。原始证据在项目外 `research/tv-runtime-comparison-20261109`；原始凭据没有复制进代码。

## 本地测试

执行 `./start-debug.ps1 -Port 8767`，打开 <http://127.0.0.1:8767>。页面标题含 dev。默认离线演示、关闭 15 秒试验心跳；切换“真实播放”后可自行测试。原有设备配置仍为测试配置，未将模拟器型号当成实际电视。

## 已修正

| 项目 | dev 行为 |
|---|---|
| 域名 | TV 请求使用 api.snm0516.aisee.tv；公开视频详情仍使用 api.bilibili.com |
| UA / 公共参数 | BiliTV 模板；statistics 随 profile 版本生成；仅 1.8.9 加入抓到的 build_sn=29938492 |
| 请求头 | 加入 App-Key、env、独立 request session；extra.session_id 与该 session 一致 |
| 取流 | qn=80、fnval=131088、force_host=0、fourk=1；使用响应返回的实际质量 |
| 媒体 | 支持 DASH 视频/音频独立输入，两路都设置节流、UA、超时和代理；兼容单段 MP4；拒绝 DRM/试看 |
| 上报字段 | video_duration、internal_track_id；UGC heartbeat 去除 sid，click 保留；part 优先使用对应 CID 的实际分 P 页码，缺失时 -1 |
| DID | 首次省略；服务端 rpdid 保存到后续会话；14013 不立即重发同一 click，也不算上报成功 |
| 时间 | click.stime 来自 /x/click-interface/click/now；ftime 首次使用服务器时间，之后持久复用 |
| 身份隔离 | .private/client-state 保存测试 Buvid、rpdid、ftime；按 demo/live、设备配置、并发路及显式 Buvid 隔离；文件锁和原子替换防止并发写入损坏 |
| 默认节奏 | 开始/结束 heartbeat；15 秒仍为主动启用的实验选项 |
| 离线演示 | 默认实际生成、分离并解码本地 DASH 音视频；TV_DEMO_FORMAT=progressive 可验证 MP4 |

签名与 click 加密算法沿用已通过官方样本离线验证的实现。播放 session 为本地生成的 40 位标识，与请求 session 分开；只对齐用途和长度，未声称复现官方生成算法。

## 当前边界

- 游客初始化/guest_access_key、页面 track/spmid、广告上下文、x-bili-trace-id、完整 SDK 遥测及 online/history 流程尚未实现；这些值不能从一次抓包直接固定复用。
- teenager_mode、auto_play 等保持本项目场景配置，没有把官方首页样本中的 3、6 当成全局常量。UA 的 network/2 与 heartbeat 的 network_type/1 仍是测试配置，没有 Android 实时网络探测。
- 这是 FFmpeg 顺序解码客户端，无电视 seek、暂停、后台恢复状态机。媒体位置取解码进度；played_time 仍是受墙钟约束的解码时长估计；没有播放器暂停操作，因此 paused_time 为 0，连接/等待不再误记为暂停。此时间模型未完整复现官方 SDK。
- 默认 Buvid 仍是本地生成的测试身份，只改为持久保存；并非抓包设备的真实硬件身份。同一个配置和并发路跨批次复用身份；同时启动多个观察服务时，同编号路也会复用，文件锁仅保证存储一致。
- 已完成下述单视频公网回归；尚未覆盖不同账号、内容类型和完整 TV 生命周期，也不保证 TV 服务对尚未实现的游客/场景字段始终采取相同行为。

## 验证记录

- 94 项 Python 回归测试通过；最后的 heartbeat 字段调整又通过对应 14 项协议/心跳测试。
- 6 项 JavaScript 页面会话测试通过。
- Playwright：1440×1000 桌面及 390×844 移动视口；页面标识、内容、无错误覆盖层、控制台、截图、实际操作均通过，移动页面没有横向溢出。
- 页面连续两轮各 3 秒 DASH：各解码 75 帧且有音频；每轮 1 次 click、2 次 heartbeat；首次无 DID，第二轮复用服务端 DID 和 ftime。
- 独立新客户端再验证两次实际解码，确认首次无 DID、后续复用，且播放 session 每次更新。
- 本机未提供 Browser 插件，使用已有 Playwright 和 Chromium；截图和脱敏验证结果位于项目外 `research/dev-validation-20261109`。

`main` 保留修改前源码基线。运行日志、客户端状态、私有请求、截图均不纳入 Git。

## 公网单次验证（2026-11-09 15:32）

用户指定视频 `BV1hcWn6iEcF`，在 `dev` 代码提交 `8fed86e` 上执行一次 45 秒播放。使用原有 `original-tv` 测试参数、直连网络、关闭周期心跳；这不代表核验了实际电视硬件。

- DASH 返回实际质量 64，视频总长 155.875 秒；正常解码 45 秒、1350 帧，音频已解码，FFmpeg 退出码 0，解码墙钟时间 45.172 秒。
- 取流、开始 heartbeat、服务器时间查询、一次 click、结束 heartbeat 全部 `code=0`；播放和上报状态均为 completed。
- 首次 click 不带 DID，服务端返回 rpdid，已保存；本次没有重发 click，也没有再次启动播放。
- 收尾 heartbeat：played_time=45、total_time=45、paused_time=0、video_duration=155、媒体位置=45。字段名与 extra.internal_track_id 已核对。
- 未发现这条视频需要额外代码修正。接口接受不等同于公开播放量计入；未检测播放量变化。

脱敏证据：项目外 `research/dev-validation-20261109/live-BV1hcWn6iEcF.json`。原始运行记录保存在忽略目录 `results/dev-live-BV1hcWn6iEcF-20261109-153255`。
