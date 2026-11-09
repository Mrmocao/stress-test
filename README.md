# TV 视频播放脚本：简单使用说明

`dev` 分支已按 TV 1.8.9 实测修正请求参数、身份复用和 DASH 播放；本地测试入口及已知边界见 [DEV-TV实测修正](DEV-TV实测修正.md)。

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

把下面的 BV1PQ4y1T7LY 换成你要播放的视频 BVID，不要传完整网页网址。
例如网页 https://www.bilibili.com/video/BV1PQ4y1T7LY 中的视频 ID 是 BV1PQ4y1T7LY。

```bash
.venv/bin/python tv_playback.py BV1PQ4y1T7LY
```

默认随机播放 45～60 秒；视频更短时播放到结束。每次命令只播放一次。
也支持 av ID 或数字 AID。

指定播放时长，例如 60 秒：

```bash
.venv/bin/python tv_playback.py BV1PQ4y1T7LY --seconds 60
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
遇到 failed 时先查看日志，避免无意中反复播放。原始日志可能包含带签名的媒体地址，分享时只提供必要片段。

## 包内文件

- tv_playback.py：主程序
- local_signing.py：请求签名
- buvid_lab.py：Buvid 生成
- did_lab.py：初始 DID 生成
- requirements.txt：Python 依赖
- README.md：本说明

请保持上述文件在同一文件夹。本包面向单视频播放，不包含 run / observe / report 批量实验所需的选样文件和历史记录。
