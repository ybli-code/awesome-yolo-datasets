#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HuggingFace parquet 数据集 -> YOLO 目录 -> 压缩 -> 夸克网盘分享

设计给 GitHub Actions runner 跑（机房带宽，规避家宽上行瓶颈）。

环境变量：
  HF_REPO      必填，如 Project-AgML/wheat_disease_detection
  DS_TITLE     必填，中文数据集名（用于 zip 名与目录名）
  HF_CLASSES   可选，JSON 数组，类别名（缺省为 "0".."N"）
  HF_SPLIT_AS  可选，split 名映射，默认 train/valid->val/test
  QUARK_COOKIE 必填，夸克 cookie
  QUARK_FOLDER 可选，网盘目录，默认 /同享AI数据集

产物：
  /tmp/dataset_downloads/samples/*.jpg   8 张样张（供本机做四宫格）
  /tmp/dataset_downloads/results/<name>.json  含 share_url
  /tmp/dataset_downloads/run.log
"""
import io
import json
import logging
import os
import random
import shutil
import sys
import time
import zipfile

import requests
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from quark_netdisk_pds import QuarkNetdisk  # noqa: E402

TEMP = "/tmp/dataset_downloads"
SAMPLES = os.path.join(TEMP, "samples")
RESULTS = os.path.join(TEMP, "results")
WORK = "/tmp/hf_yolo_work"
HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co")

os.makedirs(TEMP, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(os.path.join(TEMP, "run.log"), mode="a", encoding="utf-8")],
)
log = logging.getLogger("hf")


def list_parquet(repo):
    r = requests.get(f"{HF}/api/datasets/{repo}", timeout=60)
    r.raise_for_status()
    files = [s["rfilename"] for s in r.json().get("siblings", [])]
    return [f for f in files if f.endswith(".parquet")]


def download(repo, name, dest):
    url = f"{HF}/datasets/{repo}/resolve/main/{name}"
    log.info("下载 %s", name)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        got = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                got += len(chunk)
        log.info("  %s -> %.1f MB", name, got / 1e6)
    return dest


def build_yolo(repo, classes):
    import pyarrow.parquet as pq

    out = os.path.join(WORK, "ds")
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(out, exist_ok=True)
    pqdir = os.path.join(WORK, "pq")
    os.makedirs(pqdir, exist_ok=True)

    files = list_parquet(repo)
    if not files:
        raise SystemExit("仓库下没有 parquet 文件")
    stats = {}
    seen = set()
    for name in files:
        p = os.path.join(pqdir, os.path.basename(name))
        download(repo, name, p)
        f = pq.ParquetFile(p)
        for gi in range(f.num_row_groups):
            for x in f.read_row_group(gi).to_pylist():
                im = x.get("image") or {}
                raw = im.get("bytes")
                if not raw:
                    continue
                base = os.path.basename(im.get("path") or f"img_{len(seen)}.jpg")
                split = {"valid": "val", "validation": "val"}.get(
                    str(x.get("split") or "train").lower(), str(x.get("split") or "train").lower())
                stem, ext = os.path.splitext(base)
                key = f"{split}/{base}"
                while key in seen:
                    stem += "_d"
                    key = f"{split}/{stem}{ext}"
                seen.add(key)

                idir = os.path.join(out, "images", split)
                ldir = os.path.join(out, "labels", split)
                os.makedirs(idir, exist_ok=True)
                os.makedirs(ldir, exist_ok=True)
                ip = os.path.join(idir, base)
                with open(ip, "wb") as fh:
                    fh.write(raw)
                try:
                    W, H = Image.open(io.BytesIO(raw)).size
                except Exception:
                    os.remove(ip)
                    continue

                lines = []
                ob = x.get("objects") or {}
                for bb, c in zip(ob.get("bbox") or [], ob.get("categories") or []):
                    if c < 0 or c >= len(classes):
                        continue
                    bx, by, bw, bh = [float(v) for v in bb[:4]]
                    if bw <= 1 or bh <= 1:
                        continue  # 归一化坐标的 bbox 走另一分支
                    cx = min(1.0, max(0.0, (bx + bw / 2) / W))
                    cy = min(1.0, max(0.0, (by + bh / 2) / H))
                    nw = min(1.0, max(0.0, bw / W))
                    nh = min(1.0, max(0.0, bh / H))
                    if nw <= 0 or nh <= 0:
                        continue
                    lines.append(f"{c} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")
                with open(os.path.join(ldir, stem + ".txt"), "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + ("\n" if lines else ""))
                stats[split] = stats.get(split, 0) + 1
        os.remove(p)
        log.info("  %s 处理完，累计 %s", name, stats)

    yaml = ["path: .", "train: images/train", "val: images/val", "test: images/test",
            f"nc: {len(classes)}", "names:"]
    yaml += [f"  {i}: {c}" for i, c in enumerate(classes)]
    with open(os.path.join(out, "data.yaml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(yaml) + "\n")
    return out, stats


def make_zip(srcdir, title):
    zpath = os.path.join(WORK, f"{title}.zip")
    root_name = title
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for dp, _dn, fs in os.walk(srcdir):
            for f in fs:
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, srcdir).replace("\\", "/")
                z.write(p, f"{root_name}/{rel}")
                n += 1
    log.info("打包完成 %d 个文件，%.1f MB", n, os.path.getsize(zpath) / 1e6)
    return zpath


def export_samples(srcdir, k=8):
    os.makedirs(SAMPLES, exist_ok=True)
    imgs = []
    for dp, _dn, fs in os.walk(os.path.join(srcdir, "images")):
        for f in fs:
            if f.lower().endswith((".jpg", ".jpeg", ".png")):
                imgs.append(os.path.join(dp, f))
    random.seed(42)
    random.shuffle(imgs)
    # 尽量覆盖不同 split
    picked, by_split = [], {}
    for p in imgs:
        s = os.path.basename(os.path.dirname(p))
        if by_split.get(s, 0) < 3:
            by_split[s] = by_split.get(s, 0) + 1
            picked.append(p)
        if len(picked) >= k:
            break
    for i, p in enumerate(picked):
        ext = os.path.splitext(p)[1].lower()
        shutil.copy(p, os.path.join(SAMPLES, f"{i:02d}_{os.path.splitext(os.path.basename(p))[0]}{ext}"))
    log.info("样张 %d 张", len(picked))


def main():
    repo = os.environ.get("HF_REPO", "").strip()
    title = os.environ.get("DS_TITLE", "").strip()
    if not repo or not title:
        raise SystemExit("缺少 HF_REPO 或 DS_TITLE")
    os.makedirs(TEMP, exist_ok=True)
    os.makedirs(RESULTS, exist_ok=True)

    classes = json.loads(os.environ.get("HF_CLASSES") or "[]")
    if not classes:
        classes = [str(i) for i in range(16)]
        log.warning("未提供 HF_CLASSES，类别名回退为 0..15")

    t0 = time.time()
    srcdir, stats = build_yolo(repo, classes)
    export_samples(srcdir)
    zpath = make_zip(srcdir, title)

    cookie = os.environ.get("QUARK_COOKIE", "")
    if not cookie:
        raise SystemExit("缺少 QUARK_COOKIE")
    q = QuarkNetdisk(cookie=cookie)
    folder = os.environ.get("QUARK_FOLDER", "/同享AI数据集")
    remote = f"{folder}/{os.path.basename(zpath)}"
    log.info("上传夸克 %s", remote)
    t = time.time()
    fid = q.upload_file(zpath, remote)
    log.info("上传耗时 %.1fs fid=%s", time.time() - t, fid)
    share = q.create_share(remote, pwd="yolo")
    log.info("分享链 %s", share)

    res = {
        "dataset": repo,
        "title": title,
        "share_url": share,
        "fid": fid,
        "splits": stats,
        "images": sum(stats.values()),
        "classes": [c for c in classes],
        "size_mb": round(os.path.getsize(zpath) / 1e6, 1),
        "elapsed_s": round(time.time() - t0, 1),
    }
    with open(os.path.join(RESULTS, f"{int(time.time())}.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    log.info("ALL DONE %s", json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
