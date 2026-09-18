# -*- coding: utf-8 -*-
"""matcher 核心纯函数单测：句子切分/清理/精确命中/SRT生成（不加载模型）"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolbox.matcher import split_sentences, _clean, _exact_hit, build_srt


class TestSplitSentences(unittest.TestCase):
    def test_basic_split(self):
        s = "你好世界。这是第二句！第三句呢？"
        out = split_sentences(s)
        self.assertGreaterEqual(len(out), 3)
        self.assertIn("你好世界", out)

    def test_mixed_punct(self):
        out = split_sentences("第一句，带逗号。第二句；分号。")
        self.assertGreaterEqual(len(out), 2)

    def test_no_punct(self):
        out = split_sentences("没有标点的一句话")
        self.assertEqual(len(out), 1)


class TestClean(unittest.TestCase):
    def test_strip_spaces(self):
        # _clean 只去空白，标点保留（精确匹配用原始文本）
        self.assertEqual(_clean("  你好  "), "你好")
        self.assertEqual(_clean("你好 。"), "你好。")


class TestExactHit(unittest.TestCase):
    def test_hit(self):
        frag = {"text": "大家好，欢迎来到频道"}
        self.assertTrue(_exact_hit(frag, "大家好，欢迎来到频道"))

    def test_hit_after_clean(self):
        frag = {"text": "大家好 欢迎 来到频道"}
        self.assertTrue(_exact_hit(frag, "大家好欢迎来到频道"))

    def test_miss(self):
        frag = {"text": "完全不同的内容"}
        self.assertFalse(_exact_hit(frag, "你好世界"))


class TestBuildSrt(unittest.TestCase):
    def test_srt(self):
        chosen = [
            {"frag": {"dur": 2.5}, "text": "第一句"},
            {"frag": {"dur": 2.5}, "text": "第二句"},
        ]
        srt = build_srt(chosen)
        self.assertIn("1", srt)
        self.assertIn("0:00:00.00 --> 0:00:02.50", srt)
        self.assertIn("第一句", srt)
        self.assertIn("第二句", srt)
        # 时间轴累计：第二句从 2.5s 开始
        self.assertIn("0:00:02.50 --> 0:00:05.00", srt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
