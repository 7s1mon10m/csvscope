"""csvscope —— 本地 CSV 探查器。

    python csvscope.py data.csv

起一个只监听 127.0.0.1 的本地网页服务，把 CSV 的列结构、缺失、分布、
异常值摊开给你看。**原始数据不进浏览器**：前端拿到的只是统计摘要，
想看某列具体的值要单独点"查看样本"，后端才按需发几行。

默认只读、不落盘、不留档。关掉进程什么都不剩。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import profile as prof
import reader as rdr

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

DEFAULT_PORT = 8765
SAMPLE_SCAN_CAP = 200_000      # 单列取样最多扫这么多行，避免在超大文件上挂住

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class ScanState:
    """扫描状态。扫描线程写，HTTP 线程读，用锁护着。"""

    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.path = path
        self.state = "scanning"          # scanning | ready | error
        self.phase = "嗅探编码与分隔符"
        self.bytes_read = 0
        self.total_bytes = 0
        self.rows = 0
        self.error: str | None = None
        self.report: dict | None = None
        self.sniff: dict | None = None
        self.started = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "phase": self.phase,
                "bytes_read": self.bytes_read,
                "total_bytes": self.total_bytes,
                "rows": self.rows,
                "error": self.error,
                "elapsed": round(time.time() - self.started, 2),
            }

    def update(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)


def scan(path: str, st: ScanState, encoding=None, delimiter=None) -> None:
    """后台扫描线程。任何异常都要落进 state.error，不能让页面永远转圈。"""
    try:
        st.update(phase="嗅探编码与分隔符")
        info = rdr.sniff(path, encoding=encoding, delimiter=delimiter)
        st.update(sniff=info)

        st.update(phase="读取列名")
        st.total_bytes = os.path.getsize(path)

        rows = rdr.iter_rows(path, info["encoding"], info["delimiter"])
        try:
            header = next(rows)
        except StopIteration:
            st.update(state="ready", phase="完成",
                      report=_empty_report(path, info))
            return
        finally:
            rows.close()      # 后面还要重头读一遍，不能把文件句柄敷在这里

        columns = prof.unique_headers(header)
        candidates = prof.guess_numeric_candidates(info["sample_rows"], len(columns))
        profiler = prof.Profiler(columns, candidates)

        st.update(phase="流式统计")

        def on_progress(read, total):
            st.update(bytes_read=read, total_bytes=total or st.total_bytes,
                      rows=profiler.rows_seen, state="scanning")

        # 把 iter_rows 包一层，才能同时拿到进度回调和行本身。
        def tracked():
            for row in rdr.iter_rows(path, info["encoding"], info["delimiter"],
                                     on_progress=on_progress):
                yield row

        it = tracked()
        next(it)  # 表头已经单独取过了
        for row in it:
            profiler.feed(row)

        st.update(phase="汇总")
        duration = time.time() - st.started
        file_info = {
            "name": os.path.basename(path),
            "path": os.path.abspath(path),
            "bytes": st.total_bytes,
            "encoding": info["encoding"],
            "bom": info["bom"],
            "delimiter": info["delimiter"],
            "delimiter_display": info["delimiter_display"],
            "truncated_sample": info["truncated_sample"],
            "decode_errors_in_sample": info["decode_errors_in_sample"],
        }
        report = profiler.report(file_info, duration, len(info["sample_rows"]))
        report["sniff"] = {
            "delimiter_scores": {k: v for k, v in info["delimiter_scores"].items()},
            "note": info.get("encoding_note"),
        }
        st.update(state="ready", phase="完成", report=report,
                  bytes_read=st.total_bytes, rows=profiler.rows_seen)
    except Exception as exc:  # 页面绝不允许无声地卡在"扫描中"
        st.update(state="error", error=f"{type(exc).__name__}: {exc}",
                  phase="失败")
        traceback.print_exc()


def _empty_report(path: str, info: dict) -> dict:
    return {
        "file": {
            "name": os.path.basename(path), "path": os.path.abspath(path),
            "bytes": 0, "encoding": info["encoding"], "bom": info["bom"],
            "delimiter": info["delimiter"],
            "delimiter_display": info["delimiter_display"],
        },
        "rows": {"data_rows": 0, "ragged_rows": 0, "columns": 0},
        "scan": {"duration_s": 0, "sampled": False, "exact_fields": [], "sampled_fields": []},
        "columns": [],
        "correlations": [],
        "empty_file": True,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "csvscope"
    protocol_version = "HTTP/1.1"

    # -- 基础工具 ------------------------------------------------------

    def log_message(self, fmt, *args):
        if self.server.verbose:                      # 默认不刷屏
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # -- 路由 ----------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                return self._static("index.html")
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            if url.path == "/api/status":
                return self._json(self.server.state.snapshot())
            if url.path == "/api/events":
                return self._events()
            if url.path == "/api/report":
                return self._report()
            if url.path == "/api/sample":
                return self._sample(qs)
        except (BrokenPipeError, ConnectionResetError):
            return                                   # 用户刷新页面时的正常现象
        except Exception as exc:
            traceback.print_exc()
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json({"error": "没有这个地址"}, 404)

    # -- 各接口 --------------------------------------------------------

    def _static(self, rel: str):
        safe = os.path.normpath(rel.replace("\\", "/")).lstrip("/")
        if safe.startswith("..") or os.path.isabs(safe):
            return self._json({"error": "路径不合法"}, 400)
        full = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(full):
            return self._json({"error": f"没有 {safe}"}, 404)
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            body = fh.read()
        self._send(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"),
                   {"Cache-Control": "no-store"})

    def _report(self):
        st = self.server.state
        snap = st.snapshot()
        if snap["state"] == "error":
            return self._json({"error": snap["error"]}, 500)
        if snap["state"] != "ready":
            return self._json({"error": "还在扫描", "status": snap}, 409)
        return self._json(st.report)

    def _events(self):
        """SSE 进度流。

        只监听本机，一个用户，不必操心连接数。扫描结束后服务端主动收尾，
        客户端收到 done 就关掉 EventSource，免得浏览器自动重连。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        # 光发 Connection: close 头不会真的关连接，得把这个标志置上。
        # 不置的话浏览器会一直等 Content-Length，进度就永远不显示。
        self.close_connection = True

        st = self.server.state
        last = None
        while True:
            snap = st.snapshot()
            payload = json.dumps(snap, ensure_ascii=False)
            if payload != last:
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                last = payload
            if snap["state"] != "scanning":
                self.wfile.write(b"event: done\ndata: {}\n\n")
                self.wfile.flush()
                return
            time.sleep(0.2)

    def _sample(self, qs: dict):
        """按需返回某一列的前若干个真实值。

        这是全项目唯一会把原始数据发到浏览器的地方，而且是用户主动点的，
        一次只给一列、几行。默认不发。
        """
        st = self.server.state
        if st.snapshot()["state"] != "ready":
            return self._json({"error": "还在扫描"}, 409)
        col = int(qs.get("col", ["0"])[0])
        want = max(1, min(200, int(qs.get("n", ["20"])[0])))

        f = st.report["file"]
        values, scanned = [], 0
        for i, row in enumerate(rdr.iter_rows(f["path"], f["encoding"], f["delimiter"],
                                              max_rows=SAMPLE_SCAN_CAP)):
            scanned = i
            if i == 0:
                continue                                   # 表头
            if col < len(row):
                values.append([i, row[col]])
                if len(values) >= want:
                    break
        return self._json({
            "column": col,
            "values": values,
            "scanned_rows": scanned,
            "capped": scanned >= SAMPLE_SCAN_CAP - 1,
            "note": "从文件开头往后找，最多扫 %d 行" % SAMPLE_SCAN_CAP,
        })


# -- 控制台摘要 --------------------------------------------------------


def _disp_width(s: str) -> int:
    """终端里的显示宽度：汉字/全角占两格，别的占一格。

    直接用 f"{x:<8}" 是按**字符数**对齐的，中文列名和英文表头会错位 ——
    错位本身不算 bug，但一张歪掉的表会让人觉得整个工具都不靠谱。
    """
    return sum(2 if ord(ch) > 127 else 1 for ch in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    fill = " " * max(0, width - _disp_width(s))
    return fill + s if right else s + fill


def _clip(s: str, width: int) -> str:
    """按显示宽度截断，而不是按字符数 —— 否则中文列名会撑破表格。"""
    out, used = "", 0
    for ch in s:
        w = 2 if ord(ch) > 127 else 1
        if used + w > width:
            return out + "…"
        out += ch
        used += w
    return out


def print_summary(report: dict) -> None:
    f, rows, cols = report["file"], report["rows"], report["columns"]
    print()
    print(f"文件      {f['name']}（{human_bytes(f['bytes'])}）")
    print(f"编码      {f['encoding']}" + ("（带 BOM，已剥除）" if f.get("bom") else ""))
    print(f"分隔符    {f['delimiter_display']}")
    print(f"规模      {rows['data_rows']:,} 行 × {rows['columns']} 列")
    if rows["ragged_rows"]:
        print(f"          ! 有 {rows['ragged_rows']:,} 行列数不齐（按缺失处理）")
    if report["scan"].get("sampled"):
        print(f"抽样      直方图/相关性/两端极值基于 {report['scan']['reservoir_rows']:,} 行抽样；"
              f"均值、标准差、极值是精确值")
    print()
    print(_pad("列名", 26) + _pad("类型", 8) + _pad("缺失", 8, True) + "  "
          + _pad("唯一值", 9, True) + "  说明")
    print("-" * 92)
    for c in cols:
        miss = f"{c['missing_pct']:.1f}%"
        dist = f"{c['distinct']:,}" + ("+" if c["distinct_capped"] else "")
        print(_pad(_clip(c["name"], 25), 26) + _pad(type_label(c["type"]), 8)
              + _pad(miss, 8, True) + "  " + _pad(dist, 9, True) + "  "
              + _clip(c["type_reason"], 40))
    if report["correlations"]:
        print()
        print("强相关（|r| ≥ 0.5）")
        for c in report["correlations"][:8]:
            print(f"  {c['r']:+.3f}  {c['a']} ↔ {c['b']}   （{c['n']:,} 对）")
    print()


def type_label(t: str) -> str:
    return {"number": "数值", "id": "编号", "date": "日期", "text": "文本",
            "empty": "全空", "unknown": "无数据"}.get(t, t)


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# -- 入口 --------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="csvscope", description="本地 CSV 探查器：看看这个文件里到底有什么。")
    ap.add_argument("path", help="CSV / TSV 文件路径")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"监听端口（默认 {DEFAULT_PORT}；被占用时自动换一个）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--encoding", help="手动指定编码（默认自动嗅探）")
    ap.add_argument("--delimiter", help="手动指定分隔符（默认自动嗅探）")
    ap.add_argument("--dump-json", metavar="PATH", help="把报告写成 JSON 文件")
    ap.add_argument("--no-serve", action="store_true",
                    help="只扫一遍并把报告打到控制台，不启动网页服务")
    args = ap.parse_args(argv)

    rdr.raise_field_size_limit()

    if not os.path.isfile(args.path):
        print(f"找不到文件：{args.path}", file=sys.stderr)
        return 2

    state = ScanState(args.path)
    thread = threading.Thread(target=scan, args=(args.path, state),
                              kwargs={"encoding": args.encoding,
                                      "delimiter": args.delimiter},
                              daemon=True)
    thread.start()

    if args.no_serve:
        thread.join()
        return _finish(args, state)

    httpd, port = _bind(args.port)
    httpd.state = state
    url = f"http://127.0.0.1:{port}/"
    print(f"csvscope  →  {url}")
    print(f"扫描：{os.path.abspath(args.path)}")
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)),
                         daemon=True).start()

    # 扫完就把摘要打到控制台 —— 但网页还是要继续伺候着。
    # 不能等 serve_forever 返回再打印，那样摘要要等到用户按 Ctrl+C 才看得到。
    def watch():
        thread.join()
        _finish(args, state)
        print(f"网页还在 {url}，按 Ctrl+C 停止。")

    threading.Thread(target=watch, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


def _finish(args, state: ScanState) -> int:
    """打印控制台摘要、按需导出 JSON。网页和 --no-serve 两条路径共用。"""
    snap = state.snapshot()
    if snap["state"] == "error":
        print(f"扫描失败：{snap['error']}", file=sys.stderr)
        return 1
    if not state.report:
        return 0
    print_summary(state.report)
    if args.dump_json:
        with open(args.dump_json, "w", encoding="utf-8") as fh:
            json.dump(state.report, fh, ensure_ascii=False, indent=2)
        print(f"报告已写入 {args.dump_json}")
    return 0


def _bind(preferred: int):
    """先试指定端口；被占了就退到系统随便给一个，绝不因为端口冲突而失败。"""
    for port in (preferred, 0):
        try:
            # 只绑 127.0.0.1。扫的是用户自己的隐私数据，
            # 绝不能监听到 0.0.0.0 上让同一局域网的人也能看。
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            httpd.verbose = False
            httpd.state = None            # 由调用方填
            return httpd, httpd.server_address[1]
        except OSError:
            continue
    raise SystemExit("找不到可用端口")


if __name__ == "__main__":
    sys.exit(main())
