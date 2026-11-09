"""TV 1.8.9 本地 Buvid 字符串算法；随机 GUID 或合成 MAC，离线计算。

仅复现选定来源后的计算步骤，不实现 Android 取值、配置校验和缓存选择。
证据、大小写路径区别及用法见 docs/buvid-generation.md。
"""

import argparse
import hashlib
import re
import secrets
import sys
import uuid


PREFIXES = {"imei": "XZ", "mac": "XY", "android-id": "XX", "drm": "XU", "guid": "XW"}


def generate_mac() -> str:
    """生成本地管理的单播 MAC 字符串；不读取或修改网卡。"""
    octets = bytearray(secrets.token_bytes(6))
    octets[0] = (octets[0] & 0xFC) | 0x02
    return ":".join(f"{octet:02X}" for octet in octets)


def salt(body: str) -> str:
    """复现 BuvidV2Helper.salt；内部输入为 ASCII 十六进制字符串。"""
    try:
        return body[2] + body[12] + body[22]
    except IndexError:
        return "000"


def generate_buvid(source: str, value: str, *, outer_fallback: bool = False) -> str:
    """正常本地路径转大写；BuvidUtils 外层 GUID 兜底保留输入大小写。"""
    if source not in PREFIXES:
        raise ValueError("未知来源类型")
    if not value:
        raise ValueError("输入不能为空")
    if outer_fallback and source != "guid":
        raise ValueError("外层兜底仅适用于 GUID")
    if source == "guid":
        body = value.replace("-", "")
        # 实验工具的输入约束，不代表 APK 完整的 GUID 校验规则。
        if not re.fullmatch(r"[0-9a-fA-F]{32}", body):
            raise ValueError("GUID 去掉连字符后必须为 32 位十六进制字符")
    else:
        # 不删除 MAC 冒号，不改变输入大小写，不去除首尾空白。
        body = hashlib.md5(value.encode("utf-8")).hexdigest()
    result = PREFIXES[source] + salt(body) + body
    return result if outer_fallback else result.upper()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=PREFIXES, default="mac", help="标识来源，默认 mac")
    parser.add_argument("--value", help="选定来源的原始字符串；GUID/MAC 省略时每次随机生成，不缓存")
    parser.add_argument("--show-input", action="store_true", help="在标准错误输出用于计算的 GUID 或其他来源值")
    parser.add_argument("--outer-fallback", action="store_true", help="复现 BuvidUtils 的 GUID 兜底大小写")
    args = parser.parse_args()
    if args.value is None and args.source not in ("guid", "mac"):
        parser.error("IMEI、Android ID、DRM ID 来源需要 --value")
    if args.outer_fallback and args.source != "guid":
        parser.error("外层兜底仅适用于 GUID")
    try:
        value = args.value
        if value is None:
            value = generate_mac() if args.source == "mac" else str(uuid.uuid4())
        result = generate_buvid(args.source, value, outer_fallback=args.outer_fallback)
    except ValueError as exc:
        parser.error(str(exc))
    if args.show_input:
        print(f"{args.source}: {value}", file=sys.stderr)
    print(result)


if __name__ == "__main__":
    main()
