"""profile 的测试。

重点是四件事：
  1. Welford 真的比朴素公式精确（不是"看起来差不多"）；
  2. 千分位、货币符、括号负数都认，但**必须留痕**；
  3. 编号不能当数值算均值；
  4. 抽样是等概率的，而且报告里如实说自己抽了样。
"""

import os
import random
import statistics
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import profile  # noqa: E402


def build(rows, headers, reservoir_rows=profile.RESERVOIR_ROWS):
    """走一遍完整流程：从样本猜数值候选 → 逐行喂 → 出报告。"""
    cands = profile.guess_numeric_candidates(rows[:200], len(headers))
    p = profile.Profiler(headers, cands, reservoir_rows=reservoir_rows)
    for r in rows:
        p.feed(r)
    return p


def column(p, name):
    for c in p.report({}, 0.0, 0)["columns"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"没有这一列：{name}")


class WelfordTest(unittest.TestCase):
    # 教科书上的例子：值很大、方差很小。
    # 注意故意用小数 —— 写成整数的话这列会被判成“编号”（四个不重复的大整数
    # 本来就像编号），数字整个被抹掉，就测不到 Welford 了。
    OFFSETS = (4.5, 7.5, 13.5, 16.5)
    BASE = 1_000_000_000
    TRUE_STD = 5.477225575051661     # sqrt(30)

    def test_large_offset_small_variance(self):
        """Welford 存在的唯一理由。

        1e8 附近的四个数，方差只有 30。朴素公式 ``sum(x²) - sum(x)²/n``
        里 sum(x²) ≈ 4e16，而 float64 在 4e16 上的分辨率大约是 8 ——
        方差会被捣得面目全非，而且它不报错。
        """
        rows = [[str(self.BASE + d)] for d in self.OFFSETS]
        got = column(build(rows, ["v"]), "v")["std"]
        self.assertAlmostEqual(got, self.TRUE_STD, places=9)

    def test_naive_formula_would_have_failed(self):
        """把"朴素公式确实会错"这件事也钉住。

        如果哪天有人把 Welford 换成朴素公式，上面那条会挂，
        但这一条能直接告诉后来的人：不是测试太严，是那个公式真的不行。
        """
        xs = [self.BASE + d for d in self.OFFSETS]
        n = len(xs)
        naive = (sum(x * x for x in xs) - sum(xs) ** 2 / n) / (n - 1)
        # 实测：在 1e9 这个量级上朴素公式直接给出 0.0，真实值是 30。
        # 差异不是“精度差一点”，而是比答案本身还大的量级。
        self.assertGreater(
            abs(naive - 30.0), 10.0,
            "朴素公式在这里必然彻底偏离真实方差（真实值 30，实际常常是 0.0）")

    def test_matches_statistics_module(self):
        rnd = random.Random(20261001)
        xs = [rnd.uniform(-1e6, 1e6) for _ in range(5000)]
        p = build([[repr(x)] for x in xs], ["v"])
        c = column(p, "v")
        self.assertAlmostEqual(c["mean"], statistics.fmean(xs), places=6)
        self.assertAlmostEqual(c["std"], statistics.stdev(xs), places=6)
        self.assertAlmostEqual(c["min"], min(xs), places=6)
        self.assertAlmostEqual(c["max"], max(xs), places=6)

    def test_single_value_has_zero_std(self):
        p = build([["5"], ["5"]], ["v"])
        self.assertEqual(column(p, "v")["std"], 0.0)

    def test_one_value_only(self):
        p = build([["5"]], ["v"])
        c = column(p, "v")
        self.assertEqual(c["n"], 1)
        self.assertEqual(c["std"], 0.0)


class ParseNumberTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(profile.parse_number("12.5")[0], 12.5)
        self.assertEqual(profile.parse_number("  42  ")[0], 42.0)
        self.assertEqual(profile.parse_number("-3")[0], -3.0)
        self.assertEqual(profile.parse_number("+3")[0], 3.0)
        self.assertEqual(profile.parse_number("1.2e5")[0], 120000.0)

    def test_thousand_separators(self):
        v, kind, note = profile.parse_number("1,234.56")
        self.assertEqual(v, 1234.56)
        self.assertIn("千分位", note)

    def test_malformed_thousand_separator_is_not_a_number(self):
        # "1,23" 更像"两个数被逗号连起来"，硬当成 123 会造出假数据。
        self.assertEqual(profile.parse_number("1,23")[1], profile.TEXT)

    def test_accounting_negative(self):
        self.assertEqual(profile.parse_number("(1,234)")[0], -1234.0)

    def test_currency(self):
        self.assertEqual(profile.parse_number("$1,234.56")[0], 1234.56)
        self.assertEqual(profile.parse_number("￥88")[0], 88.0)

    def test_percent_keeps_the_number(self):
        v, kind, note = profile.parse_number("50%")
        self.assertEqual(v, 50.0)
        self.assertIn("百分号", note)

    def test_missing_tokens(self):
        for t in ("", "   ", "N/A", "n/a", "NA", "NaN", "null", "None", "nil"):
            self.assertEqual(profile.parse_number(t)[1], profile.MISSING, t)

    def test_text(self):
        for t in ("abc", "12abc", "2026-01-01", "一", "-"):
            self.assertEqual(profile.parse_number(t)[1], profile.TEXT, t)

    def test_infinity_is_missing(self):
        self.assertEqual(profile.parse_number("inf")[0], None)
        self.assertEqual(profile.parse_number("nan")[1], profile.MISSING)

    def test_notes_are_collected_on_the_column(self):
        rows = [["$1,234.56"]] * 5
        p = build(rows, ["amount"])
        notes = dict(column(p, "amount")["notes"])
        self.assertTrue(any("千分位" in k for k in notes))
        self.assertTrue(any("货币" in k for k in notes))

    def test_clean_values_produce_no_notes(self):
        p = build([["42"]] * 5, ["v"])
        self.assertEqual(column(p, "v")["notes"], [])


class TypeInferenceTest(unittest.TestCase):
    def test_numbers_are_numbers(self):
        p = build([[str(i)] for i in range(100)], ["v"])
        c = column(p, "v")
        self.assertEqual(c["type"], "number")
        self.assertIsNotNone(c["mean"])

    def test_leading_zero_is_an_identifier(self):
        """邮编和工号长这样。给它们算平均数是典型的错误结论。"""
        rows = [[f"0{i:04d}"] for i in range(1, 200)]
        p = build(rows, ["zip"])
        c = column(p, "zip")
        self.assertEqual(c["type"], "id")
        self.assertIn("前导零", c["type_reason"])
        self.assertIsNone(c["mean"], "编号不该有均值")

    def test_long_unique_integers_are_identifiers(self):
        rows = [[str(1000000 + i * 7)] for i in range(500)]
        p = build(rows, ["order_id"])
        self.assertEqual(column(p, "order_id")["type"], "id")

    def test_identifier_when_distinct_far_exceeds_the_counter_cap(self):
        """★ 回归测试：唯一值计数上限不能反过来把编号判成数值。

        计数上限是 10000。一列有 30 万个订单号时，distinct 停在 10000，
        distinct/present 算出 3% 这种毫无意义的比值，
        “几乎不重复”的判定就失效了 —— 订单号被当数值，
        报告里凭空出现一个“平均订单号 200150000.0”。

        真实数据里，这恰恰是最常见的情况：订单号本来就多。
        修法：改用 repeat_ratio（见到已知值的比例），它不会被上限影响。
        """
        n = profile.DISTINCT_LIMIT + 5_000
        rows = [[str(20_000_000_000 + i)] for i in range(n)]
        c = column(build(rows, ["order_id"]), "order_id")
        self.assertTrue(c["distinct_capped"], "这列本就该触发计数上限")
        self.assertEqual(c["type"], "id")
        self.assertIsNone(c["mean"])

    def test_repeats_are_tracked_even_past_the_cap(self):
        # 值重复了就必须看得见，即使超出计数上限也一样。
        n = profile.DISTINCT_LIMIT + 3_000
        rows = [[str(i % 5)] for i in range(n)]      # 只有 5 个不同的值
        c = column(build(rows, ["v"]), "v")
        # 只有 5 个不同的值，其余全是重复 —— 重复率应该顶到 1 附近
        self.assertGreater(c["repeat_ratio"], 0.99)
        self.assertEqual(c["type"], "number", "大量重复的小整数是数值，不是编号")

    def test_year_column_is_not_an_identifier(self):
        # 年份是整数、四位，但取值只有那么几个，唯一率低 —— 不是编号。
        rows = [[str(2016 + (i % 10))] for i in range(500)]
        p = build(rows, ["year"])
        self.assertEqual(column(p, "year")["type"], "number")

    def test_price_repeated_is_not_an_identifier(self):
        rows = [[f"{9.99 + (i % 5)}"] for i in range(500)]
        p = build(rows, ["price"])
        self.assertEqual(column(p, "price")["type"], "number")

    def test_decimal_high_unique_is_still_number(self):
        # 小数永远不是编号，哪怕它一个都不重复。
        rows = [[f"{i * 1.5}"] for i in range(500)]
        p = build(rows, ["ratio"])
        self.assertEqual(column(p, "ratio")["type"], "number")

    def test_dates_are_detected(self):
        rows = [[f"2026-0{(i % 9) + 1}-15"] for i in range(300)]
        p = build(rows, ["d"])
        self.assertEqual(column(p, "d")["type"], "date")

    def test_chinese_dates_are_detected(self):
        rows = [[f"2026年{(i % 9) + 1}月15日"] for i in range(300)]
        p = build(rows, ["d"])
        self.assertEqual(column(p, "d")["type"], "date")

    def test_text_stays_text(self):
        rows = [[f"名称{i % 20}"] for i in range(300)]
        p = build(rows, ["name"])
        self.assertEqual(column(p, "name")["type"], "text")

    def test_all_empty_column(self):
        rows = [["", "1"] for _ in range(50)]
        p = build(rows, ["blank", "v"])
        self.assertEqual(column(p, "blank")["type"], "empty")

    def test_column_never_present(self):
        # 所有数据行都比表头短。
        rows = [["1"] for _ in range(50)]
        p = build(rows, ["a", "b"])
        c = column(p, "b")
        self.assertEqual(c["type"], "unknown")
        self.assertEqual(c["missing"], 50)

    def test_mixed_column_is_text(self):
        rows = [[("42" if i % 2 else "未知")] for i in range(100)]
        p = build(rows, ["mixed"])
        self.assertEqual(column(p, "mixed")["type"], "text")


class MissingTest(unittest.TestCase):
    def test_missing_tokens_count_as_missing(self):
        rows = [["1"], ["N/A"], [""], ["NaN"], ["5"]]
        p = build(rows, ["v"])
        c = column(p, "v")
        self.assertEqual(c["missing"], 3)
        self.assertEqual(c["n"], 2)
        self.assertEqual(c["missing_pct"], 60.0)

    def test_ragged_rows_count_as_missing(self):
        """列数不齐时尾部缺掉的字段按缺失算 —— 不能当它不存在，更不能当零。"""
        rows = [["1", "2", "3"], ["4", "5"], ["6"], ["7", "8", "9"]]
        p = build(rows, ["a", "b", "c"])
        self.assertEqual(column(p, "a")["missing"], 0)
        self.assertEqual(column(p, "b")["missing"], 1)
        self.assertEqual(column(p, "c")["missing"], 2)
        # 第 2、3 行的列数和表头（3）对不上
        self.assertEqual(p.report({}, 0.0, 0)["rows"]["ragged_rows"], 2)

    def test_extra_fields_are_ignored_not_crashing(self):
        rows = [["1", "2"], ["3", "4", "5", "6"]]
        p = build(rows, ["a", "b"])
        self.assertEqual(p.report({}, 0.0, 0)["rows"]["data_rows"], 2)


class DistinctTest(unittest.TestCase):
    def test_counter_and_distinct(self):
        rows = [["x"], ["y"], ["x"], ["x"]]
        p = build(rows, ["v"])
        c = column(p, "v")
        self.assertEqual(c["distinct"], 2)
        self.assertEqual(c["top"][0], ["x", 3])

    def test_distinct_is_capped_and_flagged(self):
        limit = profile.DISTINCT_LIMIT
        rows = [[f"值{i}"] for i in range(limit + 500)]
        p = build(rows, ["v"])
        c = column(p, "v")
        self.assertTrue(c["distinct_capped"], "超过上限必须如实标注，不能假装数得过来")
        self.assertLessEqual(c["distinct"], limit + 1)

    def test_length_stats(self):
        rows = [["abc"], ["a"], ["ab"]]
        p = build(rows, ["v"])
        c = column(p, "v")
        self.assertEqual(c["len_min"], 1)
        self.assertEqual(c["len_max"], 3)
        self.assertEqual(c["len_mean"], 2.0)


class HistogramTest(unittest.TestCase):
    def test_every_value_lands_in_some_bin(self):
        values = [random.Random(7).uniform(-5, 5) for _ in range(2000)]
        h = profile.build_histogram(values, -5.0, 5.0)
        self.assertEqual(sum(h["counts"]), len(values))

    def test_max_is_counted_in_last_bin(self):
        # 最大值正好在右端点上，不能因为越界被丢掉。
        h = profile.build_histogram([0.0, 1.0], 0.0, 1.0, bins=4)
        self.assertEqual(sum(h["counts"]), 2)
        self.assertEqual(h["counts"][-1], 1)

    def test_single_distinct_value(self):
        h = profile.build_histogram([3.0, 3.0], 3.0, 3.0)
        self.assertEqual(h["counts"], [2])

    def test_empty(self):
        h = profile.build_histogram([], 0.0, 10.0)
        self.assertEqual(sum(h["counts"]), 0)

    def test_report_histogram_uses_exact_range(self):
        """直方图的分桶范围必须用全量扫出来的真实极值。

        否则两端的尾巴会被悄悄切掉 —— 而尾巴恰恰是最该看的地方。
        """
        # 用非整数，免得这列被当成“编号”而拿不到直方图
        rows = [[str(i * 0.5)] for i in range(1000)] + [["1e9"]]
        p = build(rows, ["v"], reservoir_rows=50)   # 故意让抽样丢掉那一行
        c = column(p, "v")
        self.assertEqual(c["max"], 1e9)
        self.assertEqual(c["histogram"]["hi"], 1e9, "桶的上界必须是真实的 max")


class ReservoirTest(unittest.TestCase):
    def test_keeps_everything_when_small(self):
        r = profile.Reservoir(100)
        for i in range(50):
            r.offer((float(i),))
        self.assertEqual(len(r.rows), 50)
        self.assertFalse(r.sampled)
        self.assertEqual([row[0] for row in r.rows], [float(i) for i in range(50)])

    def test_marks_itself_sampled_when_large(self):
        r = profile.Reservoir(10)
        for i in range(1000):
            r.offer((float(i),))
        self.assertEqual(len(r.rows), 10)
        self.assertTrue(r.sampled)

    def test_each_row_has_equal_chance_of_surviving(self):
        """Algorithm R 的全部意义就在这里。

        这是唯一一条会因写法不同而悄悄出错的测试：取样时用
        ``random.randrange(seen)`` 才是等概率；写成 ``random.randrange(k)``
        会让前面的行永远被压住，而结果看起来"也挺随机"。
        """
        n, k, trials = 20, 3, 4000
        counts = [0] * n
        for _ in range(trials):
            r = profile.Reservoir(k)
            for i in range(n):
                r.offer((i,))
            for row in r.rows:
                counts[row[0]] += 1
        expected = trials * k / n
        for i, c in enumerate(counts):
            self.assertGreater(c, expected * 0.7, f"第 {i} 行被系统性排除了")
            self.assertLess(c, expected * 1.3, f"第 {i} 行被系统性偏爱了")


class CorrelationTest(unittest.TestCase):
    def test_perfect_positive(self):
        rows = [[str(i), str(2 * i + 1)] for i in range(200)]
        p = build(rows, ["x", "y"])
        c = p.correlations()[0]
        self.assertEqual(c["a"], "x")
        self.assertEqual(c["b"], "y")
        self.assertAlmostEqual(c["r"], 1.0, places=6)

    def test_perfect_negative(self):
        rows = [[str(i), str(-3 * i)] for i in range(200)]
        p = build(rows, ["x", "y"])
        self.assertAlmostEqual(p.correlations()[0]["r"], -1.0, places=6)

    def test_uncorrelated_is_filtered_out(self):
        rnd = random.Random(99)
        rows = [[str(i), str(rnd.uniform(-100, 100))] for i in range(400)]
        p = build(rows, ["x", "noise"])
        self.assertEqual(p.correlations(), [], "无关的两列不该出现在报告里")

    def test_pairs_with_missing_values_are_dropped_together(self):
        """成对删除：某一对里缺一个值，这一对就整体不参与，
        而不是拿两列各自的剩余值去硬凑 —— 那样算出来的 r 是编的。
        """
        rows = []
        for i in range(200):
            if i % 10 == 0:
                rows.append(["", str(2 * i)])       # x 缺失
            else:
                rows.append([str(i), str(2 * i)])
        p = build(rows, ["x", "y"])
        c = p.correlations()[0]
        self.assertEqual(c["n"], 180)
        self.assertAlmostEqual(c["r"], 1.0, places=6)

    def test_needs_enough_points(self):
        rows = [[str(i), str(i)] for i in range(10)]
        p = build(rows, ["x", "y"])
        self.assertEqual(p.correlations(), [])

    def test_identifier_columns_are_excluded(self):
        rows = [[str(1000000 + i * 7), str(i), str(i * 2)] for i in range(200)]
        p = build(rows, ["order_id", "x", "y"])
        names = {c["a"] for c in p.correlations()} | {c["b"] for c in p.correlations()}
        self.assertNotIn("order_id", names, "编号不该出现在相关性里")


class ReportTest(unittest.TestCase):
    def test_sampled_report_says_so(self):
        rows = [[str(i)] for i in range(2000)]
        p = build(rows, ["v"], reservoir_rows=100)
        rep = p.report({"name": "x.csv"}, 1.5, 200)
        self.assertTrue(rep["scan"]["sampled"])
        self.assertIn("直方图", rep["scan"]["sampled_fields"])
        self.assertIn("均值", rep["scan"]["exact_fields"])

    def test_unsampled_report_says_nothing_was_sampled(self):
        rows = [[str(i)] for i in range(50)]
        p = build(rows, ["v"], reservoir_rows=1000)
        rep = p.report({"name": "x.csv"}, 0.1, 50)
        self.assertFalse(rep["scan"]["sampled"])
        self.assertEqual(rep["scan"]["sampled_fields"], [])

    def test_non_numeric_columns_have_no_histogram(self):
        rows = [[f"名字{i % 5}"] for i in range(50)]
        p = build(rows, ["n"])
        c = column(p, "n")
        self.assertIsNone(c["histogram"])
        self.assertEqual(c["extremes"], [])


class CandidateTest(unittest.TestCase):
    def test_numeric_columns_are_candidates(self):
        sample = [["1", "abc"], ["2", "def"], ["3", "ghi"]]
        self.assertIn(0, profile.guess_numeric_candidates(sample, 2))
        self.assertNotIn(1, profile.guess_numeric_candidates(sample, 2))

    def test_column_with_no_sample_data_is_a_candidate(self):
        # 样本里看不出内容时，宁可多花点力气去搞清楚。
        sample = [["1"], ["2"]]
        self.assertIn(1, profile.guess_numeric_candidates(sample, 2))

    def test_mostly_numeric_column_is_a_candidate(self):
        sample = [["1"], ["2"], ["N/A"], ["4"], ["5"]]
        self.assertIn(0, profile.guess_numeric_candidates(sample, 1))

    def test_half_numeric_column_is_not(self):
        sample = [[("1" if i % 2 else "未知")] for i in range(50)]
        self.assertNotIn(0, profile.guess_numeric_candidates(sample, 1))


class HeaderTest(unittest.TestCase):
    def test_duplicates_get_suffixes(self):
        out = profile.unique_headers(["a", "b", "a", "a"])
        self.assertEqual(out[0], "a")
        self.assertEqual(out[2], "a（第 2 个同名列）")
        self.assertEqual(out[3], "a（第 3 个同名列）")

    def test_blank_names_get_placeholder(self):
        self.assertEqual(profile.unique_headers(["", "  "]), ["第1列", "第2列"])

    def test_bom_contaminated_first_name_is_cleaned(self):
        self.assertEqual(profile.unique_headers(["\ufeffid", "v"])[0], "id")


class PearsonTest(unittest.TestCase):
    def test_matches_manual_computation(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, 1.0, 4.0, 3.0, 5.0]
        expected = statistics.correlation(xs, ys)
        self.assertAlmostEqual(profile.pearson(xs, ys), expected, places=12)

    def test_zero_variance_returns_none(self):
        self.assertIsNone(profile.pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))

    def test_too_few_points(self):
        self.assertIsNone(profile.pearson([1.0, 2.0], [1.0, 2.0]))


if __name__ == "__main__":
    unittest.main()
