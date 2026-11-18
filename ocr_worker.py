# -*- coding: utf-8 -*-
"""字幕条 OCR 的常驻小工：stdin 收图片路径，stdout 回一行 JSON。

用 OCR 那个独立环境（rapidocr）跑，模型只加载一次；
外面每 2 秒丢一张「前台窗口下三分之一」的截图进来，比每次起进程快一个数量级。

2026-11-18：返回**每一行**（带坐标），不再把所有文本拼成一句——外面才能只挑
最像字幕的那一行，不会把侧栏推荐/任务栏时钟混进剧情参考。
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
            lines = []
            for item in (result or []):
                if len(item) < 3:
                    continue
                box, text, score = item[0], str(item[1]), item[2]
                try:
                    ys = [float(p[1]) for p in box]
                    height = int(max(ys) - min(ys))
                    top = int(min(ys))
                except Exception:  # noqa: BLE001
                    top, height = 0, 0
                lines.append({"text": text, "y": top, "h": height,
                              "score": float(score)})
            print(json.dumps({"lines": lines,
                              "text": " ".join(x["text"] for x in lines)},
                             ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
