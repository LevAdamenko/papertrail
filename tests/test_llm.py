import json
import tempfile
import unittest
from pathlib import Path

from papertrail import llm


def body(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class LLMTest(unittest.TestCase):
    def test_chat_uses_transport_and_returns_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text(json.dumps(body("  hello ")))
            t = llm.replay_transport([p])
            self.assertEqual("hello", llm.chat("m", [{"role": "user", "content": "x"}], transport=t))
            self.assertEqual("m", t.calls[0]["model"])
            with self.assertRaises(llm.LLMError):
                llm.chat("m", [], transport=t)

    def test_bad_shape_raises(self):
        with self.assertRaises(llm.LLMError):
            llm.chat("m", [], transport=lambda u, p, h: {"error": "x"})

    def test_parse_json_fenced_and_bare(self):
        self.assertEqual({"a": 1}, llm.parse_json('Sure:\n```json\n{"a": 1}\n```'))
        self.assertEqual({"a": {"b": 2}}, llm.parse_json('x {"a": {"b": 2}} y'))
        with self.assertRaises(llm.LLMError):
            llm.parse_json("no json here")


class RecordedTruncationTest(unittest.TestCase):
    def test_reasoning_that_eats_max_tokens_raises_truncated(self):
        fixture = Path(__file__).parent / "fixtures" / "api" / "03-zawiadomienie-przedluzenie.extract-truncated.json"
        with self.assertRaises(llm.LLMTruncated):
            llm.chat("m", [], transport=llm.replay_transport([fixture]))


if __name__ == "__main__":
    unittest.main()
