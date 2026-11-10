"""Make the single-file portable exe: launcher + appended zip of the built app folder.

usage: python launcher/pack.py <version>
input : dist/LiveTranslator_v<version>/      (PyInstaller output)
output: dist/LiveTranslator_v<version>.exe   (one file, double-click to run)
        dist/LiveTranslator_v<version>.zip   (same app as a plain zip)
"""
import os
import struct
import subprocess
import sys
import zipfile

ver = sys.argv[1]
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
name = f"LiveTranslator_v{ver}"
app_dir = os.path.join(root, "dist", name)
out_zip = os.path.join(root, "dist", name + ".zip")
out_exe = os.path.join(root, "dist", name + ".exe")
SKIP = {"config.json", "selftest.txt"}          # never ship saved logins / test output

# 1) zip the app folder (top-level folder = name)
with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for dp, _, files in os.walk(app_dir):
        for f in files:
            if f in SKIP and dp == app_dir:
                continue
            full = os.path.join(dp, f)
            z.write(full, os.path.join(name, os.path.relpath(full, app_dir)).replace("\\", "/"))
print("zip:", out_zip, os.path.getsize(out_zip) // 2**20, "MB")

# 2) compile the launcher with the version baked in (C# compiler ships with Windows)
src = open(os.path.join(root, "launcher", "launcher.cs"), encoding="utf-8").read().replace("__VERSION__", ver)
gen = os.path.join(root, "build", "launcher_gen.cs")
os.makedirs(os.path.dirname(gen), exist_ok=True)
open(gen, "w", encoding="utf-8").write(src)
launcher_exe = os.path.join(root, "build", "launcher.exe")
csc = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
subprocess.run([csc, "/nologo", "/target:winexe", "/optimize+", "/codepage:65001", f"/out:{launcher_exe}",
                "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll",
                "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", gen], check=True)

# 3) launcher + zip + [length]["LTPAYLD1"]
with open(out_exe, "wb") as out:
    out.write(open(launcher_exe, "rb").read())
    size = 0
    with open(out_zip, "rb") as z:
        while chunk := z.read(1 << 20):
            out.write(chunk)
            size += len(chunk)
    out.write(struct.pack("<q", size) + b"LTPAYLD1")
print("exe:", out_exe, os.path.getsize(out_exe) // 2**20, "MB")
