# A01–A14 逐项优化与验证

日期：2026-11-09。基于 `dev` 分支 HEAD `faabb99` 及已有未提交修改继续实现，未重建 Git、未提交。对应项目外 `research/parameter-reference-20261109/TV播放项目参数说明与生成规则.md` 第 1.1 节。

本轮只做本地代码修改、既有证据分析和离线验证，没有启动模拟器公网播放、提取代理或向平台提交播放记录。以下“完成”指工程修改及离线验证完成，不代表官方 SDK 已完整复现或新增协议已获公网验证。

## 1. 按原编号逐项处理

| 编号 | 本次实现与决策 | 验证和剩余边界 |
|---|---|---|
| A01 游客来源与刷新 | guest 必须成对，同时提供 `binding` 和 `guest_contract`；校验来源、签发/到期时间、手动更新策略；每次请求前再次检查有效期 | 缺契约、过期、未生效均拒绝。官方获取/刷新接口未知，因此当前 guest 仅允许 demo，声明 `manual_verified` 也不开放 live；没有自动刷新或重放 |
| A02 安装绑定 | 身份库新增持久 `installation_id`；上下文绑定安装、Buvid 的 SHA-256 与 client_slot，并继续校验 profile_id | 跨安装、Buvid、路编号均拒绝；旧身份迁移只补安装标识，保留 Buvid/DID/ftime。非空 live 上下文必须绑定；这是本地一致性检查，不证明真实硬件来源 |
| A03 页面配置 | 页面新增内置离线上下文和 online/history 开关；Inspector、batch、worker 参数贯通，后端独立校验 | 仅 demo 单路单次，关闭详细调试和启动暂停；样例锁定 offline-android9，不接收私有 JSON 路径或游客凭据。切换 live 清空并禁用离线控件；没有开放真实上下文或多路 UI |
| A04 online 调度 | 保留解码进度驱动；周期到期且进度推进才发，暂停/后台状态与结束进度块不发；晚到只发一条，失败后停止 | 单元测试覆盖不推进、暂停、后台和结束。未增加独立 SDK 定时器；真实播放器仍只有顺序解码，未建立 Android 后台规则 |
| A05 history 结束条件 | 正常媒体结束才尝试一次 history；异常退出记录 `skipped / abnormal_exit_unverified`，非法时间记录 `invalid_media_time` | 不重复提交，不把 skipped 当成功。正常顺序结束通过本地样例；官方异常退出、force-stop、后台退出时机仍待核验 |
| A06 播放状态和时间 | 新增纯离线 `PlaybackTimeline`：播放、暂停、缓冲、后台、seek、结束、异常终止；区分位置与已播放时间 | 示例总位置 50 秒，但有效播放 25 秒，暂停/缓冲/后台各 5 秒。此模型没有控制 FFmpeg，也未接入真实请求；没有新增真实暂停按钮 |
| A07 输入约束 | 拒绝重复 JSON 键；64 KiB 有界读取；memory 为正 ASCII 十进制；mode_switch/is_interacted 为 true/false；teenager_mode/child_lock/auto_play 为非负 ASCII 十进制；继续检查资源字段一致性 | 已确认的表示格式严格校验；未知枚举不捏造最大值或意义，3/6 仍可作为显式测试输入，不成为新默认值 |
| A08 extra 转义 | 保留对单引号、反斜线、& 和控制字符的拒绝，并给出转义协议未核验的错误说明 | 完成边界约束，复杂 session_more、广告结构仍不支持；不把 JSON 转义直接当成 SDK 转义 |
| A09 收尾预算 | 启用 lifecycle 时额外为 history 预留 5 秒；结束 heartbeat 最多 5 秒且保留 history 的额度；history 最多 5 秒；请求前检查预算 | 缺预算记录 `skipped / insufficient_budget`，不调用 transport，不补发、不延长租期。未启用 lifecycle 时原预算不增加 |
| A10 稳定身份与复现 | 按用户确认恢复页面默认 random-device，每轮生成新配置；固定设备仍可手动选择；CLI 动态 demo 可指定 32 位小写十六进制 `--profile-seed`，复现同一配置 | 相同种子复现配置，不代表复现播放 session；固定 profile/slot 继续复用身份。无种子动态选择仍每次生成新的合成配置 |
| A11 合成配置用途 | 固定合成与动态配置统一 `offline_only=True`；UI 与后端都拒绝用于 live | live 只接受显式确认的 original-tv；用户确认不是硬件检测或 APK 认证 |
| A12 显式模式 | `tv_playback.py` 除本地 report 外必须指定 `--mode demo|live`；demo 安装本地 transport，拒绝旧 run/observe 入口 | 缺 mode 在开始播放前报错；worker 显式传入 mode；不静默忽略误传的 profile-seed。直接 demo 已完成 3 秒音视频解码 |
| A13 公开输出脱敏 | 新 state.json 经递归字段脱敏；原始恢复状态另存 `.private/state/`；Inspector 读取旧 state 时脱敏，事件存储前脱敏；已知身份、凭据、上下文值、私有路径及 URL query 遮蔽 | 私有恢复副本保留身份连续性。不是对任意自由文本的完整 DLP；不改写历史日志，不保证旧导出或整个 results 目录可直接公开；`.private` 是存储隔离，不是加密 |
| A14 未知 SDK 能力 | 新增 `protocol_capabilities.py`，snapshot 给出 unsupported / offline_model_only / offline_verified 状态 | guest refresh、complex extra escaping、trace、aurora、广告遥测仍未实现；没有生成随机值或复用抓包凭据冒充 SDK |

## 2. 新字段和生成规则

下列字段是**本地元数据**，不作为 TV 公共参数自动发送。

| 字段 | 类型/生成规则 | 生命周期 |
|---|---|---|
| `installation_id` | 新建或迁移身份时生成 UUID4 hex，32 位小写十六进制 | 随本地身份库持久化；不逐请求生成 |
| `binding.installation_id` | 与身份库值完全相同 | context 与安装绑定 |
| `binding.buvid_sha256` | `sha256(buvid.encode('utf-8')).hexdigest()`，64 位小写十六进制 | Buvid 改变则旧绑定失效 |
| `binding.client_slot` | 1–64 位 ASCII 字母、数字、下划线或连字符 | 单次入口默认为 default；worker 可显式指定；页面批次使用其路编号 |
| `guest_contract.source` | `offline_fixture` 或 `manual_verified` | 来源标签；不能单凭标签判定凭据真实 |
| `guest_contract.issued_at` | 正 Unix 秒整数 | `issued_at <= 当前时间` 才生效 |
| `guest_contract.expires_at` | 大于 issued_at 的 Unix 秒整数 | 当前时间必须严格小于 expires_at |
| `guest_contract.refresh` | 当前仅接受 `manual` | 过期后拒绝；不自动请求刷新 |
| `--profile-seed` | demo 动态选择专用，32 位小写十六进制 | 同种子生成同 profile；固定配置或 live 使用该参数报错 |

绑定示例使用虚构安装标识和 Buvid `fixture`，只演示匹配逻辑；不是可重放的官方凭据。程序生成摘要，避免手写错摘要：

```python
import hashlib
from playback_context import PlaybackContext
from device_profiles import resolve_profile

identity = {'installation_id': 'a' * 32, 'buvid': 'fixture'}
context = PlaybackContext({
    'profile_id': 'offline-android9',
    'binding': {
        'installation_id': identity['installation_id'],
        'buvid_sha256': hashlib.sha256(identity['buvid'].encode()).hexdigest(),
        'client_slot': 'example',
    },
    'guest': {'guest_id': 'offline-fixture-guest', 'guest_access_key': 'offline-fixture-key'},
    'guest_contract': {
        'source': 'offline_fixture', 'issued_at': 100, 'expires_at': 200, 'refresh': 'manual',
    },
})
context.bind_runtime(resolve_profile('demo', 'offline-android9'), identity,
                     'example', 'demo', clock=lambda: 150)
context.validate_runtime()  # 本地固定时钟校验通过，不调用接口
# 时钟变为 200 时校验失败；不得通过编造到期时间让真实凭据继续使用。
```

日常离线样例不需要 guest，仍使用 `examples/playback-context.offline.json`。未提供的字段不会随机补齐。安装身份保存在原命名空间，新增 installation_id 不会迁移或重置 Buvid/DID/ftime。

## 3. 可直接运行的离线示例

在项目根目录执行，每次选一个未使用过的输出目录。

固定设备、上下文和生命周期：

```powershell
.\.venv\Scripts\python.exe -B debug_worker.py --mode demo --seconds 3 --device-profile offline-android9 --client-slot review-a01-a14 --context-file examples/playback-context.offline.json --tv-lifecycle --run-dir results/review-offline-01
```

直接入口与固定种子：

```powershell
.\.venv\Scripts\python.exe -B tv_playback.py BV0000000000 --mode demo --seconds 3 --device-profile random-device --profile-seed 0123456789abcdef0123456789abcdef --tv-lifecycle --run-dir results/review-seeded-01
```

这里的 BV 编号是本地 fixture，demo 会替换为本地样片；所有业务响应来自本地 transport，媒体来自 127.0.0.1。样片生成/拆分 FFmpeg 已加 `-nostdin` 和 `stdin=DEVNULL`，避免与 worker 控制线程竞争标准输入。本次页面测试实际发现并验证了该修正。

暂停/seek/后台时间模型（只计算 JSON，不调用接口或启动播放器）：

```powershell
.\.venv\Scripts\python.exe -B playback_timeline.py --scenario examples/timeline-review.offline.json
```

示例最终结果：

```json
{
  "scope": "offline_model",
  "state": "ended",
  "played_seconds": 25.0,
  "paused_seconds": 5.0,
  "buffering_seconds": 5.0,
  "background_seconds": 5.0,
  "position_seconds": 50.0,
  "max_position_seconds": 50.0
}
```

事件时间必须有限、非负、单调不倒退；普通 progress 不允许位置倒退，回退需显式 seek。seek 跳转不计为有效播放，播放增量由观测到的位置推进与墙钟间隔共同约束；结束后不能 resume。该模型保守统计，没有观测到进度就不会凭空增加播放时间。

## 4. 预算、状态和隐私变化

生命周期关闭时最低租期仍为 `目标秒数 + 2安全 + 3启动 + 5结束heartbeat`；开启时再加 `5 history`。例如目标 45 秒时最低值从 55 秒变为 60 秒。这只是准入下限；准备接口同样消耗时间，因此最终是否能开始还需运行时预算检查。

| 场景 | 新结果 | 是否重放 |
|---|---|---|
| 正常结束且所有附加请求整数 code=0 | lifecycle completed | 否 |
| 返回 code=false、非零或传输异常 | unconfirmed，不算成功 | 否 |
| 异常退出后的 history | skipped，原因 abnormal_exit_unverified | 否 |
| 收尾预算不足 | skipped，原因 insufficient_budget | 否 |
| 强制结束进程 | 可能保留发送前的 unconfirmed；不能保证执行 finally | 否 |

媒体完成、接口被接受、完整上报、公开播放量计入仍是不同结果。额外机制未确认时，不覆盖已经完成的 media_status，但 report_status 不会被标为 completed。

新公开 state 将已知敏感字段置为 `<redacted>`；原始可恢复状态路径为 `.private/state/<公开state绝对路径的SHA-256>.json`。`read_runtime_state()` 优先读私有副本，旧输出没有副本时回退原 state。移动输出目录会改变路径摘要，因此旧目录续跑应保留原位置。已有磁盘日志不批量改写；分享前仍应检查自由文本、旧日志和签名媒体地址。私有目录沿用宿主机文件权限，未新增加密或独立 Windows ACL。

## 5. 验证记录与继续优化的依据

- Python：133 项测试全部通过，28.948 秒；覆盖凭据时效与绑定、安装迁移、输入限制、生命周期跳过/去重、预算、身份用途、显式模式、脱敏、状态机和 CLI 场景。
- JavaScript：`node --check debug_ui/app.js` 通过；本次不将历史 6 项 JS 测试记为重新执行。
- 页面：独立 `http://127.0.0.1:8879`；Playwright Chromium 1248，桌面 1440×1080、移动 390×844。本机 Browser plugin not available，使用已有 Playwright。
- 交互：选内置 fixture → 启用生命周期 → 重复 2 次被拒绝 → 改单次 → 完成 3 秒本地 DASH → 检查身份脱敏 → 切换 live 检查选项限制（未启动）→ 移动尺寸无横向溢出。页面身份、内容、无错误覆盖层、控制台和截图均通过。
- 页面样片：75 帧、有音频；media/report/lifecycle 完成；未用代理；浏览器检查未出现外部请求。
- 直接 CLI：独立研究输出目录，指定固定种子 demo；3 秒、75 帧、有音频、生命周期完成。
- 证据：项目外 `research/parameter-reference-20261109/optimization-tests-final.log`、`optimization-ui-check.json`、`optimization-direct-demo-check.json` 和 `optimization-ui-*.png`。先前 UI 验证暴露的拆分超时已修复并复测。

下一轮如果继续协议补齐，应先取得游客获取/刷新、官方正常/异常退出、暂停/seek/后台、复杂 extra 转义及 SDK telemetry 的独立证据，再设计实现。当前 A01/A04/A05/A06/A08/A14 的未知部分已明确保留，不能将本轮工程防护当作这些协议问题全部解决。
