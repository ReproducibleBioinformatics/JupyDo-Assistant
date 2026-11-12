"""The conversation logic (assistant/dialogue.py), with a fake model that answers from a
script: what the code does with the model's answers, not how good the model is."""
import asyncio
import json
import os
import tempfile
import unittest

from assistant.catalog import Catalog
from assistant.dialogue import Conversation, pick_named
from assistant.llm import LLMError
from tests.test_catalog import IMAGES


class FakeLLM:
    """Answers chat_json() with the next answer of the script, by step: the step is
    recognised from the system prompt. An answer can be an exception to raise."""
    def __init__(self, **answers):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls = []

    async def chat_json(self, messages, schema, **kw):
        prompt = messages[0]["content"]
        step = ("keywords" if "search keywords" in prompt else "ask" if "ONE more piece" in prompt else "choose")
        self.calls.append((step, schema))
        answer = self.answers[step].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def run(coro):
    return asyncio.run(coro)


class Dialogue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"made": "2026-01-01", "images": IMAGES}, f)
        cls.catalog = Catalog(cls.path)

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.path)

    def test_named_tools_in_the_catalog_are_the_answer(self):
        llm = FakeLLM(keywords=[{"terms": ["variant calling"], "tools": ["freebayes", "bcftools", "gatk4"]}],
                      choose=[{"choices": [{"tool": "freebayes", "why": "calls variants"}], "message": "ok"}])
        r = run(Conversation(self.catalog, llm).answer("I want to call variants"))
        self.assertEqual(r["type"], "choices")
        self.assertEqual([c["name"] for c in r["choices"]], ["freebayes"])
        # the model could choose only among the named tools that are in the catalog (gatk4 is not)
        enum = llm.calls[-1][1]["properties"]["choices"]["items"]["properties"]["tool"]["enum"]
        self.assertEqual(sorted(enum), ["bcftools", "freebayes"])

    def test_vague_first_request_gets_one_question(self):
        llm = FakeLLM(keywords=[{"terms": ["taxonomic", "sequence"], "tools": []},
                                {"terms": ["taxonomic", "sequence classifier"], "tools": []}],
                      ask=[{"question": "What do you want to learn?", "options": ["Species", "Genes"]}],
                      choose=[{"choices": [{"tool": "kraken2", "why": "classifies"}], "message": ""}])
        conv = Conversation(self.catalog, llm)
        r = run(conv.answer("metagenomics"))
        self.assertEqual(r["type"], "question")
        self.assertEqual(r["options"], ["Species", "Genes"])
        r = run(conv.answer("species"))   # no second question
        self.assertEqual(r["type"], "choices")

    def test_choice_outside_the_list_is_ignored_and_falls_back(self):
        llm = FakeLLM(keywords=[{"terms": ["variant calls"], "tools": []}],
                      ask=[{"question": "", "options": []}],   # no usable question: choose instead
                      choose=[{"choices": [{"tool": "not-in-catalog", "why": "x"}], "message": ""},
                              LLMError("broken")])
        r = run(Conversation(self.catalog, llm).answer("variants"))
        self.assertEqual(r["type"], "choices")
        self.assertTrue(r["choices"])          # the best matches of the search
        self.assertEqual(r["choices"][0]["why"], "")

    def test_nothing_found_retries_once(self):
        llm = FakeLLM(keywords=[{"terms": ["xyzzy"], "tools": []}, {"terms": ["plugh"], "tools": []}])
        r = run(Conversation(self.catalog, llm).answer("something odd"))
        self.assertEqual(r["type"], "nothing")
        self.assertEqual([c[0] for c in llm.calls], ["keywords", "keywords"])

    def test_same_tool_from_two_sources_is_one_choice_with_alternatives(self):
        llm = FakeLLM(keywords=[{"terms": ["assembler"], "tools": ["flye"]}],
                      choose=[{"choices": [{"tool": "flye", "why": "assembles"}], "message": ""}])
        r = run(Conversation(self.catalog, llm).answer("assemble long reads"))
        self.assertEqual(r["choices"][0]["image"], "quay.io/biocontainers/flye:1.0")

    def test_history_keeps_the_conversation(self):
        llm = FakeLLM(keywords=[{"terms": ["variant calling"], "tools": ["bcftools"]},
                                {"terms": ["variant calling"], "tools": ["bcftools"]}],
                      choose=[{"choices": [{"tool": "bcftools", "why": "a"}], "message": "m"},
                              {"choices": [{"tool": "bcftools", "why": "b"}], "message": "m"}])
        conv = Conversation(self.catalog, llm)
        run(conv.answer("first"))
        run(conv.answer("second"))
        self.assertEqual([m["role"] for m in conv.history], ["user", "assistant", "user", "assistant"])


class PickNamed(unittest.TestCase):
    def test_exact_name_before_name_inside(self):
        groups = [("r-seurat", [{"image": "quay.io/biocontainers/r-seurat:3.0.2"}]),
                  ("seurat", [{"image": "satijalab/seurat:6.0.0"}]),
                  ("bioconductor-deseq2", [{"image": "quay.io/biocontainers/bioconductor-deseq2:1.50"}])]
        self.assertEqual([n for n, _ in pick_named(groups, ["Seurat", "deseq2", "unknown"])],
                         ["seurat", "bioconductor-deseq2"])


if __name__ == "__main__":
    unittest.main()
