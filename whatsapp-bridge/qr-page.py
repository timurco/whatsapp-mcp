#!/usr/bin/env python3
"""QR for re-linking the bridge, as a page in the browser.

  ./qr-page.py            follow /tmp/whatsapp-bridge.log, open qr.html

The bridge runs under launchd, so its QR lands in a log file as half-block
characters. WhatsApp rotates the code every ~20 seconds, so a single
screenshot goes stale before it is scanned. This script watches the log for
`QR-CODE:` lines, re-renders the image and the page reloads itself every
second. Stops when the bridge reports a successful login.
"""
import base64
import io
import subprocess
import sys
import time
from pathlib import Path

import qrcode

LOG = Path("/tmp/whatsapp-bridge.log")
PAGE = Path(__file__).resolve().parent / "store" / "qr.html"

HTML = """<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="1">
<title>WhatsApp bridge QR</title>
<body style="margin:0;height:100vh;display:grid;place-items:center;background:#09090b;color:#fafafa;font:16px system-ui">
<div style="text-align:center">{body}</div>"""


def write(body: str) -> None:
    PAGE.write_text(HTML.format(body=body))


def png(code: str) -> str:
    img = qrcode.make(code, box_size=10, border=3)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def main() -> None:
    start = LOG.stat().st_size if LOG.exists() else 0
    write("<p>Waiting for the bridge to print a QR code…</p>")
    subprocess.run(["open", str(PAGE)])
    pos, n, deadline = start, 0, time.time() + 600
    while time.time() < deadline:
        with LOG.open() as f:
            f.seek(pos)
            new = f.read()
            pos = f.tell()
        for line in new.splitlines():
            if line.startswith("QR-CODE: "):
                n += 1
                write(f'<img src="data:image/png;base64,{png(line[9:].strip())}" width="420">'
                      f"<p>WhatsApp → Linked devices → Link a device · code {n}</p>")
                print(f"code {n}", flush=True)
            elif "Successfully connected and authenticated" in line or "Successfully paired" in line:
                write("<h2>Linked ✓</h2><p>The bridge is connected, this tab can be closed.</p>")
                print("linked", flush=True)
                return
        time.sleep(0.5)
    write("<p>Timed out. Run qr-page.py again.</p>")
    sys.exit(1)


if __name__ == "__main__":
    main()
