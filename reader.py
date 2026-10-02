"""CSV 嗅探与流式读取。

只做两件事：猜出这个文件是什么编码、用什么分隔符，然后把行流式吐出来。

一条纪律：**所有猜出来的东西都要进报告**。猜错了要让用户看得见，
命令行也留了 --encoding / --delimiter 手动覆盖的口子。
悄悄猜错、让人拿着错误的结论去做决定，是最坏的结果。
"""

from __future__ import annotations

import codecs
import csv
import io
import os
import sys

# 按"猜错的代价"排序：先 UTF-8（含 BOM），再中文 Windows 的 GBK，
# 再西欧的 cp1252，最后 latin-1 兜底 —— 它能解开任意字节序列，所以必须垫底。
ENCODINGS = ("utf-8-sig", "gbk", "cp1252", "latin-1")

DELIMITERS = (",", "\t", ";", "|")

SAMPLE_BYTES = 256 * 1024


def raise_field_size_limit() -> int:
    """把 csv 的单字段上限拉到系统允许的最大值。

    默认上限是 128KB，一个塞了长文本（JSON、堆栈、评论）的字段就能让它炸。
    """
    limit = sys.maxsize
    while limit > 1024:
        try:
            csv.field_size_limit(limit)
            return limit
        except OverflowError:  # 32 位平台上 sys.maxsize 超了 C long
            limit //= 10
    return csv.field_size_limit()


def detect_bom(sample: bytes) -> str | None:
    """认出 BOM。Windows 版 Excel 导出的 CSV 十有八九带 UTF-8 BOM，
    不去掉的话第一个列名前面会挂一个看不见的 \\ufeff，后面按名字找列全都找不到。"""
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith(codecs.BOM_UTF16_LE):
        return "utf-16"
    if sample.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if sample.startswith(codecs.BOM_UTF32_LE):
        return "utf-32"
    if sample.startswith(codecs.BOM_UTF32_BE):
        return "utf-32"
    return None


def sniff_encoding(sample: bytes) -> str:
    """从样本里试出编码。

    用**增量解码器**而不是 sample.decode()：样本是从文件中间硬切下来的，
    最后一个字符很可能只切到一半。直接 decode 会因此报错，把正确的编码排除掉。
    增量解码器只对"确定非法"的字节报错，尾部残缺序列它会先存着。
    """
    bom = detect_bom(sample)
    if bom:
        return bom

    for enc in ENCODINGS:
        try:
            decoder = codecs.getincrementaldecoder(enc)()
            decoder.decode(sample)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"


def sniff_delimiter(text: str, candidates=DELIMITERS) -> tuple[str, dict]:
    """猜分隔符，返回 (分隔符, 各候选的评分明细)。

    不用 csv.Sniffer：它在列数不齐或有引号的脏数据上会判错，而且判错了不吭声。
    这里自己算 —— 对所有候选分隔符各切一遍，看**哪种切法列数最稳定**。

    逗号文件用分号去切，每行都只有 1 列，"稳定"得毫无信息量，
    所以列数 < 2 的候选直接淘汰。
    """
    lines = [ln for ln in text.splitlines()[:200] if ln.strip()]
    scores: dict[str, dict] = {}

    if not lines:
        return ",", scores

    for cand in candidates:
        try:
            rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=cand))
        except csv.Error:
            scores[cand] = {"ok": False, "reason": "解析报错"}
            continue
        counts = [len(r) for r in rows if r]
        if not counts:
            scores[cand] = {"ok": False, "reason": "没有数据行"}
            continue
        mode = max(set(counts), key=counts.count)
        consistency = counts.count(mode) / len(counts)
        ok = mode >= 2
        scores[cand] = {
            "ok": ok,
            "columns": mode,
            "consistency": round(consistency, 4),
            "rows_used": len(counts),
        }

    # 先比一致性，再比列数；同分时按 candidates 的先后（逗号优先）。
    ranked = [
        (s["consistency"], s["columns"], -i, cand)
        for i, cand in enumerate(candidates)
        if (s := scores.get(cand)) and s.get("ok")
    ]
    if not ranked:
        return ",", scores
    ranked.sort(reverse=True)
    return ranked[0][3], scores


def _iter_lines(path, encoding, chunk_size, on_progress):
    """把文件切成"行"吐出来，同时报告字节进度。

    为什么要手写"读二进制块 → 增量解码 → 按 \\n 切"，而不是直接 open(..., 'r')：
    文本模式下 f.tell() 返回的是解码器背后的不透明数字，做不了进度条。
    而逐行 readline() 又拿不到字节数。只好自己来。
    """
    total = _file_size(path)
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    buf = ""

    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            buf += decoder.decode(chunk)

            start = 0
            while True:
                idx = buf.find("\n", start)
                if idx == -1:
                    break
                yield buf[start:idx + 1]
                start = idx + 1
            buf = buf[start:]

            if on_progress is not None:
                on_progress(fh.tell(), total)

        tail = decoder.decode(b"", final=True)
        if tail:
            buf += tail
        if buf:
            yield buf

    if on_progress is not None:
        on_progress(total, total)


def iter_rows(path, encoding, delimiter, chunk_size=1 << 20, on_progress=None,
              max_rows=None, stats=None):
    """流式产出 CSV 行，永不把整个文件读进内存。

    ★ 关键：**只建一个 csv.reader**，让它一直读同一个行流。
    如果每行自己新建一个 reader（next(csv.reader([line]))），
    **引号里带换行的字段会被切碎** —— reader 看不到引号还开着，
    不知道往下接。这个坑在地址、备注、JSON 字段上极常见，而且不报错。

    stats 传一个 dict 进来可以拿到 {"bad_lines": n}（一开始就是 0，
    即使一行没坏也能读到，调用方不用先自己塞一个键）。
    """
    if stats is not None:
        stats.setdefault("bad_lines", 0)
    lines = _iter_lines(path, encoding, chunk_size, on_progress)
    cr = csv.reader(lines, delimiter=delimiter)
    yielded = 0

    try:
        while True:
            try:
                row = next(cr)
            except StopIteration:
                break
            except csv.Error:
                # 单行语法有问题（字段超过上限、畸形引号）时不能把整个文件废掉。
                # 记一笔继续走（实测 reader 遇到 Error 后下一行照常能读），
                # 报告里会说跳过了多少行 —— 不吭声地少读几行最糟。
                if stats is not None:
                    stats["bad_lines"] += 1
                continue
            if not row:
                continue                  # 纯空行
            yielded += 1
            yield row
            if max_rows is not None and yielded >= max_rows:
                break
    finally:
        # 调用方提前 break（比如只取样前 200 行）时也要把文件句柄收干净，
        # 否则反复取样会把句柄堆起来。
        lines.close()


def _file_size(path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def sniff(path, sample_bytes=SAMPLE_BYTES, encoding=None, delimiter=None) -> dict:
    """嗅探：返回编码、分隔符、BOM、以及供类型推断用的样例行。

    encoding / delimiter 传了值就跳过嗅探，直接用用户给的。
    """
    raise_field_size_limit()

    with open(path, "rb") as fh:
        sample = fh.read(sample_bytes)

    bom = detect_bom(sample)
    enc = encoding or sniff_encoding(sample)
    enc, dec_note = _resolve(enc, sample)

    try:
        text = sample.decode(enc, errors="replace")
    except LookupError:
        enc, dec_note = "latin-1", f"未知编码名，已回退到 latin-1"
        text = sample.decode(enc, errors="replace")

    dl = delimiter or ","
    scores: dict = {}
    if not delimiter:
        dl, scores = sniff_delimiter(text)

    trunc = "\ufffd"
    return {
        "encoding": enc,
        "encoding_note": dec_note,
        "bom": bom,
        "delimiter": dl,
        "delimiter_display": _display_delim(dl),
        "delimiter_scores": scores,
        "sample_bytes": len(sample),
        "sample_rows": _sample_rows(text, dl),
        "decode_errors_in_sample": text.count(trunc),
        "truncated_sample": len(sample) >= sample_bytes,
    }


def _resolve(enc: str, sample: bytes):
    """把 'utf-16' 这种笼统的名字换成能实际解码的方向（LE/BE）。"""
    if enc not in ("utf-16", "utf-32"):
        return enc, None
    try:
        sample.decode(enc)
        return enc, None
    except UnicodeDecodeError:
        alt = enc + "-le" if sample[:2] == codecs.BOM_UTF16_LE else enc + "-be"
        return alt, f"BOM 指示 {alt}"


def _display_delim(dl: str) -> str:
    return {"\t": "\\t（制表符）", ",": "逗号", ";": "分号", "|": "竖线"}.get(dl, repr(dl))


def _sample_rows(text: str, delimiter: str, limit: int = 200):
    """从样本里解析前若干行，给类型推断当线索。

    样本是硬切的，最后一行可能不完整 —— 直接丢掉，只用来"猜类型"而已。
    """
    lines = text.splitlines()
    if len(lines) > 1 and not text.endswith(("\n", "\r")):
        lines = lines[:-1]
    try:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    except csv.Error:
        return []
    return rows[:limit]
