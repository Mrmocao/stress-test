# 云视听小电视真实版本目录（REA/静态证据）

本目录只收录已经拿到 APK 文件并完成包名、Manifest、渠道文件、SHA-256 和签名核对的样本。下载页标题不是证据；例如 962 页面标 1.8.7，但 APK 实际为 1.8.9。

|来源|版本|versionCode|build_sn|渠道|APK SHA-256|签名|状态|
|---|---:|---:|---:|---|---|---|---|
|哔哩哔哩官网|1.8.9|108900|29938492|master|73229ae195f45cdbfd4577b81962a4b4c9345a36abcd846e00dbc0647c127ef2|93ba270f…5910e|已核实|
|沙发管家|1.8.9|108900|29938502|shafa|541058b22798c3d5863b4f257840fb348b38233645dd88899829d4fd36234fb1|93ba270f…5910e|已核实|
|历史包（明月高歌）|1.6.6|106600|13482127|master|24a28ef769bd4b5c2c9819fe1971803f272f09bdcd388ab83cea25eb672615bb|93ba270f…5910e|已核实|
|QQ下载站镜像|1.6.9|106900|14500739|master|e6d7cb3999bf95c44c3551f6e7f160d311205df5a9c21aed21e0aae3a1539493|93ba270f…5910e|已核实|
|962 镜像（页面标 1.8.7）|1.8.9|108900|29674886|dangbei|8ed6446a3258f899394a14ff22be505708a7784fb4c478a630b1a05c9bfa6ba3|93ba270f…5910e|已核实|

`build_sn` 是 APK Manifest 中 `BUILD_SN` 的十六进制整数转十进制：例如 `0x1c8d33 = 29938492`，`0xdd4383 = 14500739`，`0x1c4cd86 = 29674886`。目前证据只能说明它是发布构建标识；没有足够样本证明可由 versionCode、日期或渠道推导，缺失时必须保留为空。

## 来源页

- 官网：<https://dl.hdslb.com/mobile/latest/android_tv_yst/iBiliTV-master.apk>
- 沙发：<https://app.shafa.com/apk/yunshitingxiaodianshi.html>
- 历史包：<https://m.myygc.com/yybf/1078.html>
- 1.6.9：<https://www.qqtf.com/azrj/102296.html>
- 962：<https://www.962.net/azsoft/554527.html>

## 可复现核验

```powershell
python audit_tv_apk.py path\to\sample.apk -o result.json
& "$env:LOCALAPPDATA\Android\Sdk\build-tools\37.0.0\apksigner.bat" verify --print-certs path\to\sample.apk
```

`audit_tv_apk.py` 只读 APK，不安装、不启动、不访问设备，也不会输出用户 ID、设备 ID、IP、Cookie 或 Token。`*.rea.json` 是 REA 静态签名证据；`apksigner` 输出用于实际签名验证。

历史公开目录 `titleDB_bilibili_TV版` 目前只作为候选清单（34 个文件名，1.0.7–1.2.7.2），下载端有验证码/挑战，未把未成功下载的文件列入真实目录。
