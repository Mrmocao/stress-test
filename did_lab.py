"""复现 TV 1.8.9 SDK 的本地 DID 计算；不生成服务端 rpdid。"""

import argparse
import base64
import re
import sys

from buvid_lab import generate_buvid, generate_mac


def encode_did(plaintext: str) -> str:
    """HwIdHelper.encode：链式异或和 URL-safe Base64，无换行、无填充。"""
    data = bytearray(plaintext.encode('utf-8'))
    if not data:
        raise ValueError('DID 编码输入不能为空')
    data[0] ^= len(data) & 0xFF
    for i in range(1, len(data)):
        data[i] ^= data[i - 1]
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def normalize_mac(value: str) -> str:
    value = re.sub(r'[^0-9A-Fa-f]', '', value).lower()
    return value if len(value) == 12 and len(set(value)) > 1 else ''


def did_plaintext(mac: str, *, bluetooth_mac='', mmc_id='', serial='', android_id=None, model=None) -> str:
    """输入为 Android 取值层返回的字符串；实验只接受 ASCII 设备字段。"""
    values = [mac, bluetooth_mac, mmc_id, serial, android_id, model]
    if any(value is not None and not value.isascii() for value in values):
        raise ValueError('本实验仅支持 ASCII 设备字段')
    plaintext = '|'.join([normalize_mac(mac), normalize_mac(bluetooth_mac), mmc_id.lower(), serial.lower()])
    if len(plaintext) < 4:
        if android_id is None or model is None:
            raise ValueError('设备字段全空时需同时提供 android-id 和 model')
        # Java Character.isWhitespace 在 ASCII 范围的空白字符。
        clean_model = re.sub(r'[\x09-\x0d\x1c-\x20]', '', model)
        plaintext = android_id + '@' + clean_model
    return plaintext


def generate_did(mac: str, **kwargs) -> str:
    return encode_did(did_plaintext(mac, **kwargs))


def generate_local_identity() -> dict:
    """同一个新随机 MAC 同时用于 Buvid 和本地 DID；每次调用均不读缓存。"""
    mac = generate_mac()
    return {'mac': mac, 'buvid': generate_buvid('mac', mac), 'did': generate_did(mac)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mac', help='省略时随机生成本地管理单播 MAC')
    parser.add_argument('--bluetooth-mac', default='')
    parser.add_argument('--mmc-id', default='', help='SDK 取值层返回的 MMC 标识')
    parser.add_argument('--serial', default='', help='SDK 取值层返回的 USB/系统序列号')
    parser.add_argument('--android-id')
    parser.add_argument('--model')
    parser.add_argument('--show-input', action='store_true')
    args = parser.parse_args()
    mac = generate_mac() if args.mac is None else args.mac
    try:
        plaintext = did_plaintext(mac, bluetooth_mac=args.bluetooth_mac, mmc_id=args.mmc_id,
                                  serial=args.serial, android_id=args.android_id, model=args.model)
        did = encode_did(plaintext)
    except ValueError as error:
        parser.error(str(error))
    if args.show_input:
        print(f'mac: {mac}\nplaintext: {plaintext}', file=sys.stderr)
    print(did)


if __name__ == '__main__':
    main()
