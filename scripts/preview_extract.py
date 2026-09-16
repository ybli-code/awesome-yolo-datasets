#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preview_extract.py —— GitHub Action: 下载数据集 ZIP 并提取 4 张真实样本
读取 preview_urls.json: [{"row": 1378, "title": "...", "url": "https://app.roboflow.com/ds/xxx?key=yyy"}]
输出: /tmp/dataset_downloads/samples_{row}/sample_0..3.{ext}（随 artifact 上传）
"""
import os
import sys
import json
import time
import zipfile
import random
import urllib.request
import logging

TEMP_DIR = "/tmp/dataset_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("preview_extract")
try:
    _fh = logging.FileHandler(os.path.join(TEMP_DIR, "run.log"), encoding="utf-8")
    _fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(_fh)
except Exception as _e:
    print("FileHandler fail:", _e)


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    total = 0
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(4 * 1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    return total


def process(item):
    row = item.get("row")
    title = item.get("title")
    url = item.get("url")
    logger.info("处理: %s (行%s)", title, row)
    zip_path = os.path.join(TEMP_DIR, f"row{row}.zip")
    t0 = time.time()
    size = download(url, zip_path)
    logger.info("  下载完成 %.1fMB 耗时%.0fs", size / 1024 / 1024, time.time() - t0)
    sdir = os.path.join(TEMP_DIR, f"samples_{row}")
    os.makedirs(sdir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        imgs = [n for n in z.namelist()
                if n.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp"))
                and not n.startswith("__MACOSX")]
        logger.info("  ZIP 图片 %d 张", len(imgs))
        if not imgs:
            raise RuntimeError("no images in zip")
        random.seed(20260916 + (row or 0))
        random.shuffle(imgs)
        for i, n in enumerate(imgs[:4]):
            data = z.read(n)
            ext = os.path.splitext(n)[1].lower() or ".jpg"
            with open(os.path.join(sdir, f"sample_{i}{ext}"), "wb") as f:
                f.write(data)
    os.remove(zip_path)
    logger.info("  ✓ 已提取 4 张样本到 %s", sdir)
    return True


def main():
    cfg = "preview_urls.json"
    items = json.load(open(cfg, encoding="utf-8"))
    ok = 0
    for it in items:
        try:
            if process(it):
                ok += 1
        except Exception as e:
            logger.error("  失败: %s", str(e)[:200])
    logger.info("完成 %d/%d", ok, len(items))
    print(f"DONE {ok}/{len(items)}")
    if ok < len(items):
        sys.exit(1)


if __name__ == "__main__":
    main()
