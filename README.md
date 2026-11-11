# TV 视频播放脚本：简单使用说明

2026-11-11 更新：当前规则以 [TV 构建与独立安装](DEV-TV构建与独立安装-20261111.md) 为准；动态配置已升级 v2，种子仅留在私有状态，未核验协议构建仅用于离线对照。

多版本安全测试新增独立入口 `python -m safety_lab.runner`：每轮全新 Android 模拟器数据盘、安装状态、缓存及 Linux 网络命名空间，本地故障服务与日志归档/清理。准备真实镜像及测试 APK 后，按 [安全测试运行说明](安全测试运行说明.md) 配置版本矩阵；该隔离不由原调试页面自动启用。

`dev` 分支已按 TV 1.8.9 实测修正请求参数、身份复用和 DASH 播放；本地测试入口及已知边界见 [DEV-TV实测修正](DEV-TV实测修正.md)。

运行观察台现已集成五份已核验 TV APK 的构建目录、独立安装生成 v2 和公开输出脱敏；可在离线模式选择 1.6.9 master / 1.8.9 dangbei 等构建对照。运行 `powershell -File .\start-debug.ps1` 后打开 `http://127.0.0.1:8765`，详见 [构建与独立安装](DEV-TV构建与独立安装-20261111.md)。

设备选项已支持每轮动态生成关联一致的测试配置；实测对比、固定字段和随机规则见 [设备参数生成规律](设备参数生成规律.md)。

游戏事件 CSV 可通过独立工具 `game_device_library.py` 建立硬件/应用渠道观测库并生成本地兼容性样例，见 [游戏设备库使用说明](游戏设备库使用说明.md)。游戏库按设备去重并保留参数关联，不替换 TV APK 固定参数。

新增 click 元数据、显式播放上下文和可选 online/history 机制；离线使用方法及未覆盖范围见 [上下文与生命周期补齐](DEV-上下文与生命周期补齐.md)。

2026-11-09 参数审阅后的 A01–A14 修改、字段契约与示例见 [逐项优化记录](DEV-A01-A14逐项优化.md)。直接入口现在必须显式指定模式；观察台的离线演示和真实播放共用同一套设备方案，合成参数用于接口兼容测试，不代表真实硬件核验。页面默认每轮动态生成新的设备配置，可选内置上下文和单次生命周期验证。

独立的只读播放量监控已放在 [view_monitor](view_monitor/README.md)，入口为 `view_monitor/启动播放量监控.cmd`，默认地址 `http://127.0.0.1:8766`。该模块可单独使用，只查询公开播放量，不运行下面的播放流程。

## 1. 准备环境

适用于 macOS / Linux；本次已增加 Windows 原生兼容。Windows 实时调试环境请先阅读 [调试环境使用说明](调试环境使用说明.md)，详细代码解读见 [代码能力与运行过程](代码能力与运行过程.md)。
需要 Python（建议 3.11 或更高版本）和 FFmpeg，并能够访问哔哩哔哩及其视频 CDN。

macOS 已安装 Homebrew 时：

```bash
brew install python ffmpeg
```

Ubuntu / Debian / WSL Ubuntu：

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip ffmpeg tzdata ca-certificates
```

## 2. 安装依赖（首次使用）

解压 ZIP，在终端进入解压出来的 bilibili-tv-playback 文件夹，再执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
ffmpeg -version
```

## 3. 播放视频

先做本地离线验证（生成样片，不访问公网）：

```powershell
.\.venv\Scripts\python.exe -B tv_playback.py BV0000000000 --mode demo --device-profile offline-android9 --seconds 3
```

下面是显式 live 用法。选择 `original-tv` 时仍应核对配置并传入 `--device-confirmed`；其他设备方案可复用观察台的合成兼容性参数，但不会自动验证真实硬件。

把下面的 BV1PQ4y1T7LY 换成你要播放的视频 BVID，不要传完整网页网址。
例如网页 https://www.bilibili.com/video/BV1PQ4y1T7LY 中的视频 ID 是 BV1PQ4y1T7LY。

```bash
.venv/bin/python tv_playback.py BV1PQ4y1T7LY --mode live --device-profile original-tv --device-confirmed
```

默认随机播放 45～60 秒；视频更短时播放到结束。每次命令只播放一次。
也支持 av ID 或数字 AID。

指定播放时长，例如 60 秒：

```bash
.venv/bin/python tv_playback.py BV1PQ4y1T7LY --mode live --device-profile original-tv --device-confirmed --seconds 60
```

查看所有参数：

```bash
.venv/bin/python tv_playback.py --help
```

这是后台按正常速度解码音视频并发送播放记录的脚本，不会弹出播放器窗口，也不会通过扬声器出声。
不需要连接电视、不需要 ADB，不需要登录账号或提供 Cookie。
播放成功不保证公开播放量一定增加。

## 4. 查看结果

终端出现 completed 表示本次播放流程完成，不等于平台一定计入公开播放量。
结果自动保存到脚本目录下的 results/；每次运行使用一个新目录，其中 state.json 保存播放状态，events.jsonl 保存接口返回码。
详细请求和错误信息自动保存到 .private/。这两个目录不需要提前创建；脚本目录需要有写权限。
新 state.json 对已知身份/凭据字段脱敏；原始恢复副本保存在 .private/state/。旧日志和任意自由文本不保证已脱敏，不应直接分享整个输出目录。
遇到 failed 时先查看日志，避免无意中反复播放。原始日志可能包含带签名的媒体地址，分享时只提供必要片段。

## 包内文件

- tv_playback.py：主程序
- local_signing.py：请求签名
- buvid_lab.py：Buvid 生成
- did_lab.py：初始 DID 生成
- requirements.txt：Python 依赖
- README.md：本说明

请保持上述文件在同一文件夹。本包面向单视频播放，不包含 run / observe / report 批量实验所需的选样文件和历史记录。
