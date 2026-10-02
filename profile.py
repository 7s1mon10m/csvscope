"""流式列统计。

一趟扫完，不把数据读进内存。这里是整个项目真正花心思的地方：

* 均值/标准差用 **Welford 在线算法**算，不是 sum(x²)-sum(x)²/n。
  后者在"值很大、方差很小"的数据上会灾难性丢精度（比如一列全是
  1e9 附近的数），而 Welford 是同样几行代码但**正确**的那种写法。
* 直方图和相关性的区间、形状事先不知道，所以留一个 **行级蓄水池抽样**
  （Algorithm R）：小文件全留、大文件等概率抽样，并在报告里**明写是不是抽样**。
* 均值、标准差、最小/最大是**精确**的，跟文件多大无关。抽样的只有直方图、
  相关性和两端极值。

一条纪律：任何"我替你做了处理"的事情都要写进报告
（千分位被剥掉了、这列疑似是编号不该算均值），不许偷偷做。
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter

# 缺失标记。刻意保守：只认业界公认的那几个，不加 "-" "?" "无" 这类
# 在真实数据里正当存在的值 —— 把真值当缺失，比把缺失当真值更糟。
MISSING_TOKENS = {"na", "n/a", "nan", "null", "none", "nil"}

_CURRENCY = "$€£¥￥"

# 文本列的唯一值统计上限。超过就停止收录新值（老值继续计数），
# 并在报告里标注"基数已达上限"—— 高基数列的 top-N 本来就没什么意义，
# 但一个十亿行的文件不能因此把内存吃光。
DISTINCT_LIMIT = 10_000

# 蓄水池默认容量（按行，不是按列）。
RESERVOIR_ROWS = 4_000

# 只有前这么多行做"前导零 / 日期形状"检查：够判断了，且不拖慢大文件。
SHAPE_CHECK_ROWS = 300

_DATE_PATTERNS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"),
    re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$"),
    re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$"),
)

_LEADING_ZERO = re.compile(r"^[+-]?0\d")

MISSING = "missing"
NUMBER = "number"
TEXT = "text"


def parse_number(raw: str):
    """把一个字段解析成数字，返回 (值, 类别, 处理说明)。

    类别是 'number' / 'missing' / 'text'；值在不是数字时为 None；
    处理说明是"我替你做了什么"，没有就是空串 —— 它会原样出现在报告里。

    现实世界的数字长这样：``$1,234.56``、``(1,234)``（会计负数）、``50%``、
    ``+3``、``1.2e5``。这些都能认，但认的过程必须留痕：用户得知道
    ``1,234`` 被当成了一千二百三十四。
    """
    t = raw.strip()
    if not t or t.lower() in MISSING_TOKENS:
        return None, MISSING, ""

    body = t
    notes = []
    negative = False

    if body.startswith("(") and body.endswith(")") and len(body) > 2:
        negative = True
        notes.append("括号表示负数")
        body = body[1:-1].strip()
    if body[:1] in ("+", "-"):
        if body[0] == "-":
            negative = not negative
        body = body[1:].strip()
    if body[:1] in _CURRENCY:
        notes.append("货币符号已剥离")
        body = body[1:].strip()
    if body.endswith("%"):
        notes.append("百分号已剥离（按原数字 50 处理，不是 0.5）")
        body = body[:-1].strip()

    if "," in body:
        int_part = body.split(".", 1)[0]
        groups = int_part.split(",")
        # 只接受 1,234 / 12,345,678 这种规矩的千分位；
        # "1,23" 更像"两个数字被逗号连起来"，不当数字处理。
        if len(groups) > 1 and not all(len(g) == 3 for g in groups[1:]):
            return None, TEXT, ""
        notes.append("千分位分隔符已剥离")
        body = body.replace(",", "")

    if not body:
        return None, TEXT, ""
    try:
        v = float(body)
    except ValueError:
        return None, TEXT, ""
    if not math.isfinite(v):
        return None, MISSING, ""
    return (-v if negative else v), NUMBER, "、".join(notes)


class ColumnStats:
    """一列的全部累积状态。"""

    __slots__ = (
        "index", "name", "present", "missing", "empty", "numeric_ok", "numeric_fail",
        "n", "mean", "m2", "lo", "hi", "all_integer", "leading_zero", "counter",
        "distinct_capped", "repeats", "len_min", "len_sum", "len_max", "shape_checked",
        "date_hits", "notes",
    )

    def __init__(self, index: int, name: str):
        self.index = index
        self.name = name
        self.present = 0        # 这一列在这一行确实存在（列数不齐时可能不存在）
        self.missing = 0        # 存在但是空的，或者整格缺列
        self.empty = 0          # 明确是空串的
        self.numeric_ok = 0
        self.numeric_fail = 0
        self.n = 0              # 参与数值统计的个数
        self.mean = 0.0
        self.m2 = 0.0           # Welford 的偏差平方累积量
        self.lo = math.inf
        self.hi = -math.inf
        self.all_integer = True
        self.leading_zero = False
        self.counter: Counter = Counter()
        self.distinct_capped = False
        self.repeats = 0        # 见到了已经出现过的值。判断“是不是编号”靠它，
                                # 不靠 distinct/present（那个会被计数上限弄坏）
        self.len_min = math.inf
        self.len_sum = 0
        self.len_max = 0
        self.shape_checked = 0
        self.date_hits = 0
        self.notes: Counter = Counter()

    # -- 在线更新 ------------------------------------------------------

    def add_absences(self, k: int) -> None:
        """列数不齐时，尾部缺掉的字段按"缺失"计 —— 不能当它不存在，也不能当零。"""
        self.missing += k

    def add(self, raw: str, check_shape: bool, parsed) -> None:
        """parsed 为 None 表示"这列不用做数值统计"，跳过数字解析。"""
        self.present += 1
        t = raw.strip()

        if not t or t.lower() in MISSING_TOKENS:
            self.missing += 1
            if not t:
                self.empty += 1
            return

        if check_shape and self.shape_checked < SHAPE_CHECK_ROWS:
            self.shape_checked += 1
            if _LEADING_ZERO.match(t):
                self.leading_zero = True
            if any(p.match(t) for p in _DATE_PATTERNS):
                self.date_hits += 1

        ln = len(t)
        self.len_sum += ln
        if ln < self.len_min:
            self.len_min = ln
        if ln > self.len_max:
            self.len_max = ln

        # 唯一值/频率。到上限后只继续给"已认识的"值计数，并标记截断。
        if t in self.counter:
            self.counter[t] += 1
            self.repeats += 1
        elif len(self.counter) < DISTINCT_LIMIT:
            self.counter[t] = 1
        else:
            self.distinct_capped = True

        if parsed is None:
            return
        value, kind, note = parsed
        if note:
            self.notes[note] += 1
        if kind == NUMBER:
            self._add_number(value)
        elif kind == TEXT:
            self.numeric_fail += 1

    def _add_number(self, x: float) -> None:
        """Welford 在线更新。

        关键在 m2 用的是**更新后**的 mean：
            mean += (x - mean) / n
            m2   += (x - mean_new) * (x - mean_old)
        写错成同一个 mean 两次，结果会在数据量大时慢慢漂 —— 而且不报错。
        """
        self.numeric_ok += 1
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)
        if x < self.lo:
            self.lo = x
        if x > self.hi:
            self.hi = x
        if self.all_integer and not x.is_integer():
            self.all_integer = False

    # -- 结论 ----------------------------------------------------------

    @property
    def std(self) -> float:
        if self.n < 2:
            return 0.0
        return math.sqrt(self.m2 / (self.n - 1))

    @property
    def distinct(self) -> int:
        return len(self.counter)

    @property
    def distinct_ratio(self) -> float:
        return self.distinct / self.present if self.present else 0.0

    @property
    def repeat_ratio(self) -> float:
        """重复值占比。**判断编号只能用这个，不能用 distinct/present。**

        distinct 会被 DISTINCT_LIMIT 截断：一列 30 万个订单号，
        distinct 停在 10000，distinct/present 算出 3%，
        “几乎不重复”的判定就失效了 —— 订单号被当成数值，
        报告里凭空冒出一个“平均订单号”。

        而 repeats 是白算的：本来就是靠“这个值认不认识”决定往哪张
        计数器里放。它只会在计数上限之前的行为上受影响，恰好不影响判断。
        """
        return self.repeats / self.present if self.present else 0.0

    def numeric_success_rate(self) -> float:
        attempts = self.numeric_ok + self.numeric_fail
        return self.numeric_ok / attempts if attempts else 0.0

    def looks_like_id(self) -> tuple[bool, str]:
        """这列是"编号"还是"数值"？

        电话、邮编、订单号、学号全是数字，但它们的均值毫无意义。
        同类工具最常在这里翻车：给一列订单号算出个"平均订单号 30512"，
        然后有人照着它做决策。
        """
        if self.n == 0 or not self.all_integer:
            return False, ""
        if self.leading_zero:
            return True, "含前导零（000123），前导零只会出现在编号里"
        digits = len(str(abs(int(self.hi)))) if math.isfinite(self.hi) else 0
        if digits >= 5 and self.repeat_ratio <= 0.05:
            return True, f"整数、{digits} 位、几乎不重复（重复率 {self.repeat_ratio:.1%}）"
        return False, ""

    # -- 导出 ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "present": self.present,
            "missing": self.missing,
            "empty": self.empty,
            "distinct": self.distinct,
            "distinct_capped": self.distinct_capped,
            "len_min": 0 if self.len_min is math.inf else int(self.len_min),
            "len_max": int(self.len_max),
            "len_mean": round(self.len_sum / self.present, 1) if self.present else 0,
            "numeric_ok": self.numeric_ok,
            "numeric_fail": self.numeric_fail,
            "n": self.n,
            "mean": self.mean if self.n else None,
            "std": self.std if self.n else None,
            "min": self.lo if self.n else None,
            "max": self.hi if self.n else None,
            "all_integer": bool(self.n and self.all_integer),
            "leading_zero": self.leading_zero,
            "repeat_ratio": round(self.repeat_ratio, 6),
            "date_hits": self.date_hits,
            "shape_checked": self.shape_checked,
            "notes": [[n, c] for n, c in self.notes.most_common()],
        }


class Reservoir:
    """行级蓄水池抽样（Algorithm R）。

    单遍流过 N 行，等概率地留下 k 行。N 事先不知道，也不需要知道。

    为什么是**行级**而不是列级：直方图只需要单列的值，但相关性需要
    同一行的多个列对齐。按列各抽各的，抽出来的值来自不同的行，
    两列根本对不上，相关系数就是编的。
    """

    __slots__ = ("k", "rows", "seen")

    def __init__(self, k: int = RESERVOIR_ROWS):
        self.k = k
        self.rows: list[tuple] = []
        self.seen = 0

    def offer(self, row: tuple) -> None:
        self.seen += 1
        if len(self.rows) < self.k:
            self.rows.append(row)
            return
        j = random.randrange(self.seen)
        if j < self.k:
            self.rows[j] = row

    @property
    def sampled(self) -> bool:
        return self.seen > len(self.rows)


# -- 几何 / 统计小工具 -------------------------------------------------


def build_histogram(values: list[float], lo: float, hi: float, bins: int = 24) -> dict:
    """按**精确**的 min/max 定桶，用抽样值计数。

    桶边界必须用全量扫出来的真实极值，不能用抽样值的极值 ——
    否则直方图会悄悄截断掉两端的尾巴，而尾巴恰恰是最该看的地方。
    """
    if not values:
        return {"bins": bins, "lo": lo, "hi": hi, "counts": [0] * bins, "n": 0}
    if lo == hi:
        return {"bins": 1, "lo": lo, "hi": hi, "counts": [len(values)], "n": len(values)}
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        k = int((v - lo) / width)
        if k < 0:
            k = 0
        elif k >= bins:
            k = bins - 1          # 最大值正好落在右端点上，收进最后一桶
        counts[k] += 1
    return {"bins": bins, "lo": lo, "hi": hi, "counts": counts, "n": len(values)}


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sxx = syy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mx
        dy = y - my
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    denom = math.sqrt(sxx * syy)
    if denom <= 0:
        return None
    return sxy / denom


# -- 主控 --------------------------------------------------------------


class Profiler:
    """把行喂进来，扫完拿报告。"""

    def __init__(self, columns: list[str], numeric_candidates: set[int] | None = None,
                 reservoir_rows: int = RESERVOIR_ROWS):
        self.columns = [ColumnStats(i, name) for i, name in enumerate(columns)]
        self.candidates: set[int] = set(numeric_candidates or ())
        # 列号 → 在蓄水池向量里的下标。预计算，否则每行都要重排一次，白烧 CPU。
        self.positions = {c: p for p, c in enumerate(sorted(self.candidates))}
        self.reservoir = Reservoir(reservoir_rows)
        self.rows_seen = 0
        self.ragged_rows = 0

    def feed(self, row: list[str]) -> None:
        self.rows_seen += 1
        ncols = len(self.columns)
        n = len(row)
        if n != ncols:
            self.ragged_rows += 1

        check = self.rows_seen <= SHAPE_CHECK_ROWS
        vector: list = [None] * len(self.positions)

        for i in range(min(n, ncols)):
            col = self.columns[i]
            parsed = None
            if i in self.candidates:
                # 一次解析，两个用途：列统计 + 蓄水池。不能解析两遍。
                parsed = parse_number(row[i])
                if parsed[1] == NUMBER:
                    vector[self.positions[i]] = parsed[0]
            col.add(row[i], check, parsed)

        if n < ncols:
            for i in range(n, ncols):
                self.columns[i].add_absences(1)

        if any(v is not None for v in vector):
            self.reservoir.offer(tuple(vector))

    # -- 汇总 ----------------------------------------------------------

    def numeric_indices(self) -> list[int]:
        """最终判定为"数值"（而不是编号、也不是文本）的列号。"""
        out = []
        for col in self.columns:
            if col.numeric_ok + col.numeric_fail == 0:
                continue
            if col.numeric_success_rate() < 0.95:
                continue
            is_id, _ = col.looks_like_id()
            if is_id:
                continue
            out.append(col.index)
        return sorted(out)

    def column_report(self, col: ColumnStats) -> dict:
        d = col.to_dict()
        total = self.rows_seen or 1
        d["missing_pct"] = round(col.missing / total * 100, 2)

        num_attempts = col.numeric_ok + col.numeric_fail
        is_id, why = col.looks_like_id() if num_attempts else (False, "")

        if col.present == 0 and self.rows_seen == 0:
            d["type"], d["type_reason"] = "unknown", "文件里没有数据行"
        elif col.present == 0:
            d["type"], d["type_reason"] = "unknown", "所有行都比表头短，这一列从没出现过"
        elif col.missing == col.present:
            d["type"], d["type_reason"] = "empty", "整列都是空的（或缺失标记）"
        elif num_attempts and col.numeric_success_rate() >= 0.95 and is_id:
            d["type"], d["type_reason"] = "id", f"疑似编号：{why}"
        elif num_attempts and col.numeric_success_rate() >= 0.95:
            d["type"] = "number"
            d["type_reason"] = f"{col.numeric_success_rate():.0%} 的值可以解析成数字"
        elif col.shape_checked and col.date_hits / col.shape_checked >= 0.9:
            d["type"], d["type_reason"] = "date", "绝大多数值看起来是日期/时间"
        else:
            d["type"], d["type_reason"] = "text", "以文本为主"

        if d["type"] == "id":
            # 把数字整个抹掉，而不是“界面上不显示”。
            # 值还在数据里的话，早晚有人拿它去画图、去排序、去写进结论。
            # 订单号的均值本来就不存在，不是“不该看”，是“没有”。
            d["mean"] = d["std"] = d["min"] = d["max"] = None

        d["top"] = [[v, c] for v, c in col.counter.most_common(12)]

        if d["type"] == "number":
            values = self._sample_column(col.index)
            d["histogram"] = build_histogram(values, col.lo, col.hi) if values else None
            d["extremes"] = self._extremes(values)
        else:
            d["histogram"] = None
            d["extremes"] = []
        return d

    def _sample_column(self, col_index: int) -> list[float]:
        pos = self.positions.get(col_index)
        if pos is None:
            return []
        return [r[pos] for r in self.reservoir.rows if r[pos] is not None]

    @staticmethod
    def _extremes(values: list[float]) -> list[float]:
        """抽样值里最小三个、最大三个 —— 用来给人看一眼尾巴长什么样。"""
        if len(values) < 4:
            return []
        s = sorted(values)
        return [round(v, 6) for v in (s[:3] + s[-3:])]

    def correlations(self, threshold: float = 0.5, cap: int = 40) -> list[dict]:
        idx = self.numeric_indices()
        if len(idx) < 2 or not self.reservoir.rows:
            return []

        series = {c: self._sample_column(c) for c in idx}
        # 每列的长度可能不同（缺失值个数不同），必须按行对齐，
        # 所以直接遍历蓄水池的行，成对取。
        out = []
        for a_i in range(len(idx)):
            for b_i in range(a_i + 1, len(idx)):
                a, b = idx[a_i], idx[b_i]
                pos_a, pos_b = self.positions.get(a), self.positions.get(b)
                if pos_a is None or pos_b is None:
                    continue
                xs, ys = [], []
                for r in self.reservoir.rows:
                    x, y = r[pos_a], r[pos_b]
                    if x is not None and y is not None:   # 成对删除
                        xs.append(x)
                        ys.append(y)
                if len(xs) < 30:
                    continue
                r_val = pearson(xs, ys)
                if r_val is None or abs(r_val) < threshold:
                    continue
                out.append({
                    "a": self.columns[a].name,
                    "b": self.columns[b].name,
                    "r": round(r_val, 3),
                    "n": len(xs),
                })
        out.sort(key=lambda d: abs(d["r"]), reverse=True)
        return out[:cap]

    def report(self, file_info: dict, duration_s: float, type_sample_rows: int) -> dict:
        sampled = self.reservoir.sampled
        return {
            "file": file_info,
            "rows": {
                "data_rows": self.rows_seen,
                "ragged_rows": self.ragged_rows,
                "columns": len(self.columns),
            },
            "scan": {
                "duration_s": round(duration_s, 2),
                "type_sample_rows": type_sample_rows,
                "reservoir_rows": len(self.reservoir.rows),
                "reservoir_seen": self.reservoir.seen,
                "sampled": sampled,
                # 哪些数是精确的、哪些是抽样的，直接摆在用户面前。
                # "差不多对"和"精确"混在一起说，是数据工具最坏的毛病。
                "exact_fields": ["缺失数", "均值", "标准差", "最小/最大", "长度统计"]
                                if sampled else ["全部（文件未超过抽样上限）"],
                "sampled_fields": ["直方图", "相关性", "两端极值"] if sampled else [],
                "distinct_limit": DISTINCT_LIMIT,
            },
            "columns": [self.column_report(c) for c in self.columns],
            "correlations": self.correlations(),
        }


def guess_numeric_candidates(sample_rows: list[list[str]], n_columns: int) -> set[int]:
    """从样本里挑出"值得在每个单元格上花力气解析"的列。

    这是全流程唯一的近似之处：一列如果前 200 行全是文本、后面才变成数字，
    这里就不会把它当数值列。代价换来的是宽表上的速度 —— 实测值得。

    样本里完全看不出内容的列（整列缺失、或者样本全是短行）一律列入候选：
    对它一无所知的时候，宁可多花点力气去搞清楚。
    """
    candidates = set()
    for i in range(n_columns):
        ok = fail = seen = 0
        for row in sample_rows:
            if i >= len(row):
                continue
            _, kind, _ = parse_number(row[i])
            if kind == NUMBER:
                ok += 1
                seen += 1
            elif kind == TEXT:
                fail += 1
                seen += 1
        if seen == 0 or (ok and ok / (ok + fail) >= 0.8):
            candidates.add(i)
    return candidates


def unique_headers(raw: list[str]) -> list[str]:
    """列名去重 + 补空名。

    CSV 里出现两个同名列是常事（分页导出尤其常见），重名会让前端的
    下钻和相关性列表直接串台。加后缀区分，并保留原始顺序。
    """
    seen: dict[str, int] = {}
    out = []
    for i, name in enumerate(raw):
        # 主动剔掉 BOM：正常路径下解码器已经处理掉了，
        # 但用户一旦用 --encoding 手动指定编码，BOM 就会泄到第一个列名里，
        # 变成一列“﻿id”——看起来一模一样，就是找不到。
        base = (name or "").strip().lstrip("\ufeff").strip() or f"第{i + 1}列"
        if base in seen:
            seen[base] += 1
            out.append(f"{base}（第 {seen[base]} 个同名列）")
        else:
            seen[base] = 1
            out.append(base)
    return out
