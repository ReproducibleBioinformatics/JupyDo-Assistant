"""Search of the image catalog (catalog.json, made by scripts/make-catalog.py), in pure
Python: the model extracts search keywords from the conversation, this module finds
the images that match them, and the model chooses only among those. The keywords
are of two kinds: terms of the analysis ("variant calling"), matched on the
descriptions, and tool names ("samtools"), matched on the names.

Ranking, in order of importance:
  1. relevance: how well the keywords match the tool's name and description
  2. source: BioContainers, StaPH-B and the trusted organizations before other
     Docker Hub repositories
  3. architecture: images native to this machine before emulated ones
  4. use: within the same source, more used images first

Python 3.9 compatible (the hub image is python:3.9).

Try it: python3 -m assistant.catalog catalog.json "single cell" "clustering" --tools seurat scanpy
"""
import json
import math
import platform
import re
import sys

PREFERRED_SOURCES = ("biocontainers", "staphb", "trusted")
STOPWORDS = {"a", "an", "and", "for", "from", "in", "of", "on", "the", "to", "tool", "tools",
             "with", "data", "analysis", "file", "files"}


def machine_arch():
    """amd64 or arm64: the architecture of this machine, in the catalog's names."""
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") else "amd64"


def words(text):
    """Lowercase words of the text (letters and digits), without the stopwords and
    single characters ('2' of 'sars-cov-2' would match any version number)."""
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOPWORDS and len(w) > 1]


def stem(word):
    """The word without a common English ending, if at least 4 letters are left:
    'calling' and 'calls' -> 'call', 'aligner' -> 'align'."""
    for ending in ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ers", "er", "ed", "es", "s", "y"):
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            return word[:-len(ending)]
    return word


def matches(keyword_word, text_word):
    """A keyword word matches a word of the text if it is the same word or, for words
    of 4 letters or more, if the text word starts with its stem: 'calling' matches
    'calls' and 'caller', 'alignment' matches 'aligner'."""
    if text_word == keyword_word:
        return True
    s = stem(keyword_word)
    return len(s) >= 4 and text_word.startswith(s)


class Catalog:
    def __init__(self, path):
        with open(path) as f:
            data = json.load(f)
        self.made = data.get("made", "")
        self.images = data["images"]
        for image in self.images:
            image["_name"] = image["name"].lower()
            image["_name_words"] = words(image["name"].replace("_", " ").replace("-", " "))
            image["_words"] = words(image["description"])
            image["_text"] = " ".join(image["_words"])
        # use, as a percentile within each source (sources measure it differently)
        for source in {i["source"] for i in self.images}:
            ranked = sorted((i for i in self.images if i["source"] == source), key=lambda i: i.get("use") or 0)
            for n, image in enumerate(ranked):
                image["_use"] = (n + 1) / len(ranked)

    @staticmethod
    def term_relevance(image, terms):
        """How well the terms of the analysis ("variant calling", "long read") match the
        image's description and the words of its name: 0 if none does."""
        score = 0.0
        for term in terms:
            kw = words(term)
            if not kw:
                continue
            if len(kw) > 1 and " ".join(kw) in image["_text"]:
                score += 3    # the whole phrase in the description ("single cell")
            elif all(any(matches(k, w) for w in image["_name_words"]) for k in kw):
                score += 2    # in the tool's name ("assembler" -> raven-assembler)
            else:             # some of its words in the description
                score += 2 * sum(any(matches(k, w) for w in image["_words"]) for k in kw) / len(kw)
        return score

    @staticmethod
    def tool_relevance(image, tools):
        """How well the tool names ("samtools", "kraken") match the image's name."""
        score = 0.0
        for tool in tools:
            kw = words(tool)
            if not kw:
                continue
            if tool.lower().strip() == image["_name"] or "".join(kw) == image["_name"]:
                score += 8    # the tool itself ("samtools")
            elif all(any(matches(k, w) for w in image["_name_words"]) for k in kw):
                score += 4    # in the tool's name ("kraken" -> kraken2)
        return score

    def search(self, terms, tools=(), arch=None, limit=20):
        """The images that match the terms of the analysis and the tool names, best
        first (at most `limit`). Each comes with its 'score', 'relevance' (how well it
        matches the terms and tools), 'term_relevance' (the terms only: how well it fits
        the analysis, whatever its name) and 'native' (True if it runs without emulation
        on `arch`). Tool names count only on the image's name: a generic word such as
        "taxonomy" must go in the terms, or a library called "taxonomy" would win."""
        arch = arch or machine_arch()
        found = []
        for image in self.images:
            t = self.term_relevance(image, terms)
            r = t + self.tool_relevance(image, tools)
            if r <= 0:
                continue
            native = arch in image["archs"]
            score = (r + (1.0 if image["source"] in PREFERRED_SOURCES else 0)
                     + (0.5 if native else 0) + 0.5 * image["_use"])
            found.append((score, image, native, r, t))
        found.sort(key=lambda x: (-x[0], x[1]["image"]))
        return [dict({k: v for k, v in image.items() if not k.startswith("_")},
                     score=round(score, 2), relevance=round(r, 2), term_relevance=round(t, 2), native=native)
                for score, image, native, r, t in found[:limit]]


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python3 -m assistant.catalog catalog.json TERM [TERM ...] [--tools TOOL ...]")
    args = sys.argv[2:]
    terms, tools = (args[:args.index("--tools")], args[args.index("--tools") + 1:]) if "--tools" in args else (args, [])
    catalog = Catalog(sys.argv[1])
    for i in catalog.search(terms, tools):
        print(f"{i['score']:6.2f}  {i['image']:55}  {','.join(i['archs']):12}  {i['description'][:70]}")


if __name__ == "__main__":
    main()
