#!/usr/bin/env python3
"""Offline audit for Bilibili TV APKs. No install, execution, network, or device IDs."""
from __future__ import annotations
import argparse, hashlib, json, re, subprocess, zipfile
from pathlib import Path
AAPT = r"C:\Users\admin\AppData\Local\Android\Sdk\build-tools\37.0.0\aapt.exe"
EXPECTED_PACKAGE = "com.xiaodianshi.tv.yst"
def audit(path: Path):
    raw = path.read_bytes(); sha = hashlib.sha256(raw).hexdigest()
    badging = subprocess.run([AAPT,"dump","badging",str(path)],capture_output=True,text=True,encoding="utf-8",errors="replace").stdout
    m = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'",badging)
    tree = subprocess.run([AAPT,"dump","xmltree",str(path),"AndroidManifest.xml"],capture_output=True,text=True,encoding="utf-8",errors="replace").stdout
    b = re.search(r'android:name\([^)]*\)="BUILD_SN".*?android:value\([^)]*\)=\(type 0x10\)0x([0-9a-f]+)',tree,re.S|re.I)
    mobi = re.search(r'android:name\([^)]*\)="MOBI_APP".*?android:value\([^)]*\)="([^"]+)"',tree,re.S)
    channels=[]
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.lower().startswith("assets/") and n.lower().endswith(".channel"):
                channels.append({"file":n,"value":z.read(n).decode("utf-8","replace").strip() or None})
    return {"file":str(path),"sha256":sha,"bytes":len(raw),"package":m.group(1) if m else None,"version_name":m.group(3) if m else None,"version_code":int(m.group(2)) if m else None,"build_sn":int(b.group(1),16) if b else None,"build_sn_hex":("0x"+b.group(1).lower()) if b else None,"mobi_app":mobi.group(1) if mobi else None,"channels":channels,"package_ok":bool(m and m.group(1)==EXPECTED_PACKAGE)}
def main():
 ap=argparse.ArgumentParser(); ap.add_argument("apk",nargs="+"); ap.add_argument("-o","--output",type=Path); a=ap.parse_args(); rows=[audit(Path(x)) for x in a.apk]; out=json.dumps(rows,ensure_ascii=False,indent=2); print(out); a.output.write_text(out+"\n",encoding="utf-8") if a.output else None
if __name__=="__main__": main()
