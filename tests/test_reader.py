"""reader 的测试。

每一条都对应真实世界里会遇到的一种脏 CSV —— 不是为了凑覆盖率，
是为了把踩过的坑钉死，免得以后改代码时又掉回去。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reader  # noqa: E402


class Base(unittest.TestCase):
    def make(self, data, suffix=".csv"):
        if isinstance(data, str):
            data = data.encode("utf-8")
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        self.addCleanup(os.unlink, path)
        return path

    def sniff(self, path, **kw):
        return reader.sniff(path, **kw)

    def read(self, path, **kw):
        info = reader.sniff(path, **kw)
        return list(reader.iter_rows(path, info["encoding"], info["delimiter"]))


class EncodingTest(Base):
    def test_utf8_bom_is_detected_and_stripped(self):
        # Windows 版 Excel 导出的 CSV 必然带 BOM。带 BOM 的 'a' 和裸 'a'
        # 是两个不同的字符串，不剥掉的话第一个列名永远对不上。
        path = self.make(b"\xef\xbb\xbfa,b\n1,2\n")
        info = self.sniff(path)
        self.assertEqual(info["encoding"], "utf-8-sig")
        self.assertEqual(info["bom"], "utf-8-sig")
        rows = list(reader.iter_rows(path, info["encoding"], info["delimiter"]))
        self.assertEqual(rows[0], ["a", "b"])

    def test_gbk_is_detected(self):
        # 中文版 Excel 存 CSV 的默认编码就是 GBK，不是 UTF-8。
        path = self.make("姓名,年龄\n张三,20\n".encode("gbk"))
        info = self.sniff(path)
        self.assertEqual(info["encoding"], "gbk")
        rows = list(reader.iter_rows(path, info["encoding"], info["delimiter"]))
        self.assertEqual(rows[1], ["张三", "20"])

    def test_plain_utf8_still_wins_over_gbk(self):
        path = self.make("名字\n李四\n")
        info = self.sniff(path)
        self.assertIn(info["encoding"], ("utf-8-sig", "utf-8"))
        rows = list(reader.iter_rows(path, info["encoding"], info["delimiter"]))
        self.assertEqual(rows[1], ["李四"])

    def test_sample_cut_mid_character_does_not_mislead(self):
        # 样本是从文件中间硬切的，最后一个汉字常常只切到一半。
        # 用 sample.decode() 会因此报错、把正确的编码排除掉。
        fluffy = ("测试,值\n" + "汉字内容,1\n" * 200).encode("utf-8")
        path = self.make(fluffy)
        info = self.sniff(path, sample_bytes=333)   # 奇数长度，必然切断
        self.assertEqual(info["encoding"], "utf-8-sig")

    def test_manual_override_wins(self):
        path = self.make("名字\n李四\n".encode("gbk"))
        info = self.sniff(path, encoding="gbk", delimiter=",")
        self.assertEqual(info["encoding"], "gbk")
        self.assertEqual(info["delimiter"], ",")


class DelimiterTest(Base):
    def test_comma(self):
        self.assertEqual(self.sniff(self.make("a,b,c\n1,2,3\n"))["delimiter"], ",")

    def test_semicolon(self):
        # 欧洲区 Excel 用分号，因为那里的小数点是逗号。
        self.assertEqual(self.sniff(self.make("a;b;c\n1;2;3\n"))["delimiter"], ";")

    def test_tab(self):
        self.assertEqual(self.sniff(self.make("a\tb\tc\n1\t2\t3\n"))["delimiter"], "\t")

    def test_pipe(self):
        self.assertEqual(self.sniff(self.make("a|b|c\n1|2|3\n"))["delimiter"], "|")

    def test_semicolon_inside_quoted_comma_file(self):
        # 逗号文件里某列含分号，不能因此误判成分号分隔。
        path = self.make('a,b\n"x;y",2\n"p;q",3\n')
        self.assertEqual(self.sniff(path)["delimiter"], ",")

    def test_single_column_file_falls_back_to_comma(self):
        path = self.make("只有一列\n值\n")
        self.assertEqual(self.sniff(path)["delimiter"], ",")

    def test_manual_override(self):
        path = self.make("a;b\n1;2\n")
        self.assertEqual(self.sniff(path, delimiter=";")["delimiter"], ";")


class QuotingTest(Base):
    def test_quoted_field_with_embedded_newline(self):
        """这是整个 reader 里最容易写错的一处。

        地址、备注、JSON 字段里带换行非常常见。如果实现是"先按 \\n 切行、
        再逐行丢给 csv.reader"，引号里的换行会被切碎 —— 而且不报错。
        正确做法是让一个 csv.reader 一直读同一个行流。
        """
        path = self.make('id,note\n1,"第一行\n第二行"\n2,正常\n')
        rows = self.read(path)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1], ["1", "第一行\n第二行"])
        self.assertEqual(rows[2], ["2", "正常"])

    def test_quoted_comma(self):
        path = self.make('a,b\n"含,逗号",2\n')
        self.assertEqual(self.read(path)[1], ["含,逗号", "2"])

    def test_escaped_double_quote(self):
        path = self.make('a,b\n"他说""你好""",2\n')
        self.assertEqual(self.read(path)[1], ['他说"你好"', "2"])

    def test_crlf_line_endings(self):
        path = self.make("a,b\r\n1,2\r\n3,4\r\n")
        rows = self.read(path)
        self.assertEqual(rows, [["a", "b"], ["1", "2"], ["3", "4"]])

    def test_newline_inside_quotes_does_not_shift_progress(self):
        # 带引号换行的字段会让"字节数"和"行数"对不上，
        # 进度回调必须仍然走到 100%，不能半路停住。
        body = "id,note\n" + "".join(f'{i},"多行\n备注"\n' for i in range(500))
        path = self.make(body)
        seen = []
        info = self.sniff(path)
        list(reader.iter_rows(path, info["encoding"], info["delimiter"],
                              chunk_size=512, on_progress=lambda a, b: seen.append((a, b))))
        self.assertTrue(seen, "进度回调一次都没触发")
        self.assertEqual(seen[-1][0], seen[-1][1])


class MalformedTest(Base):
    def test_ragged_rows_do_not_crash(self):
        path = self.make("a,b,c\n1,2,3\n4,5\n6,7,8,9,10\n")
        rows = self.read(path)
        self.assertEqual([len(r) for r in rows], [3, 3, 2, 5])

    def test_blank_lines_are_dropped(self):
        path = self.make("a,b\n\n1,2\n\n\n3,4\n")
        self.assertEqual(self.read(path), [["a", "b"], ["1", "2"], ["3", "4"]])

    def test_empty_file(self):
        path = self.make("")
        rows = self.read(path)
        self.assertEqual(rows, [])

    def test_header_only(self):
        path = self.make("a,b,c\n")
        self.assertEqual(self.read(path), [["a", "b", "c"]])

    def test_very_long_field_exceeds_default_limit(self):
        # csv 模块的字段默认上限是 128KB，一个塞了长文本的字段就能让它炸。
        big = "x" * 300_000
        path = self.make(f"a,b\n{big},2\n")
        rows = self.read(path)
        self.assertEqual(len(rows[1][0]), 300_000)

    def test_no_trailing_newline(self):
        path = self.make("a,b\n1,2")
        self.assertEqual(self.read(path), [["a", "b"], ["1", "2"]])

    def test_max_rows_stops_early(self):
        path = self.make("a\n" + "".join(f"{i}\n" for i in range(100)))
        info = self.sniff(path)
        rows = list(reader.iter_rows(path, info["encoding"], info["delimiter"], max_rows=10))
        self.assertEqual(len(rows), 10)

    def test_bad_lines_are_counted_and_reading_continues(self):
        """有语法问题的行被跳过时必须计数 —— 用户得知道自己少看了几行。

        真实触发方式是字段超过 csv 的尺寸上限（这里故意把上限压到 10 字节）。
        重点是 reader 跨过坏行之后还能接着读，不会把后面全丢掉。
        """
        import csv
        original = csv.field_size_limit()
        path = self.make("a,b\nxxxxxxxxxxxxxx,2\n1,2\n")
        stats = {}
        try:
            info = self.sniff(path)          # sniff 会把上限调回最大值
            csv.field_size_limit(10)         # 所以这里再压回去
            rows = list(reader.iter_rows(path, info["encoding"], info["delimiter"],
                                         stats=stats))
        finally:
            csv.field_size_limit(original)
        self.assertEqual(stats["bad_lines"], 1)
        self.assertEqual(rows, [["a", "b"], ["1", "2"]], "坏行之后的数据必须照读")

    def test_bad_lines_is_zero_for_clean_file(self):
        path = self.make("a,b\n1,2\n")
        stats = {}
        info = self.sniff(path)
        list(reader.iter_rows(path, info["encoding"], info["delimiter"], stats=stats))
        self.assertEqual(stats["bad_lines"], 0)


class SampleRowsTest(Base):
    def test_sample_rows_exclude_truncated_tail(self):
        path = self.make("a,b\n1,2\n3,4\n5,6\n")
        info = self.sniff(path)
        for row in info["sample_rows"]:
            self.assertEqual(len(row), 2)

    def test_truncated_sample_flag(self):
        big = "a,b\n" + "".join(f"{i},x\n" for i in range(50_000))
        path = self.make(big)
        self.assertTrue(self.sniff(path)["truncated_sample"])
        self.assertFalse(self.sniff(self.make("a,b\n1,2\n"))["truncated_sample"])


if __name__ == "__main__":
    unittest.main()
