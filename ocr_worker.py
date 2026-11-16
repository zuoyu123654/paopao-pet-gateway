# -*- coding: utf-8 -*-
"""字幕条 OCR 的常驻小工：stdin 收图片路径，stdout 回一行 JSON。

用 OCR 那个独立环境（rapidocr）跑，模型只加载一次；
外面每 2 秒丢一张「下三分之一」的截图进来，比每次起进程快一个数量级。
"""
import json
import sys


def main() -> None:
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        path = line.strip()
        if not path:
            continue
        try:
            result, _ = engine(path)
            texts = [str(item[1]) for item in (result or []) if len(item) > 1]
            print(json.dumps({"text": " ".join(texts)}, ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
