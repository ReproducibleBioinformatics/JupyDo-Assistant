"""Search of the catalog (assistant/catalog.py), on a small catalog made here."""
import json
import os
import tempfile
import unittest

from assistant.catalog import Catalog, matches, stem, words


def image(name, description, source="biocontainers", archs=("amd64",), use=1):
    repo = name if source == "dockerhub" else f"quay.io/biocontainers/{name}"
    return {"source": source, "name": name, "image": f"{repo}:1.0", "repository": repo, "tag": "1.0",
            "updated": "2026-01-01", "archs": list(archs), "description": description, "use": use}


IMAGES = [
    image("samtools", "Tools for dealing with SAM, BAM and CRAM files", use=100),
    image("bcftools", "BCFtools, utilities for manipulating variant calls in VCF and BCF format", use=90),
    image("freebayes", "Bayesian haplotype-based polymorphism discovery and genotyping", use=50),
    image("kraken2", "Taxonomic sequence classifier", use=80),
    image("taxonomy", "Python and Rust library for loading and manipulating taxonomic trees", use=5),
    image("flye", "De novo assembler for single molecule sequencing reads", use=60, archs=("amd64", "arm64")),
    image("someone/flye", "Flye genome assembler, long reads", source="dockerhub", use=10 ** 6),
    image("bioconductor-scrnaseq", "Collection of public single-cell RNA-seq datasets", use=40),
]


class Words(unittest.TestCase):
    def test_words_drop_stopwords_and_single_characters(self):
        self.assertEqual(words("Tools for SARS-CoV-2 analysis"), ["sars", "cov"])

    def test_stem(self):
        self.assertEqual(stem("calling"), "call")
        self.assertEqual(stem("calls"), "call")
        self.assertEqual(stem("alignment"), "align")
        self.assertEqual(stem("bam"), "bam")   # too short to cut

    def test_matches_by_stem(self):
        self.assertTrue(matches("calling", "calls"))
        self.assertTrue(matches("assembly", "assembler"))
        self.assertTrue(matches("cell", "cellular"))    # 4 letters or more: also the start of a word
        self.assertFalse(matches("bam", "bamtools"))    # shorter: only the same word
        self.assertTrue(matches("bam", "bam"))


class Search(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"made": "2026-01-01", "images": IMAGES}, f)
        cls.catalog = Catalog(cls.path)

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.path)

    def names(self, *args, **kw):
        return [i["name"] for i in self.catalog.search(*args, **kw)]

    def test_tool_name_wins(self):
        self.assertEqual(self.names([], ["samtools"])[0], "samtools")

    def test_terms_match_descriptions(self):
        self.assertEqual(self.names(["variant calling"])[0], "bcftools")

    def test_generic_term_does_not_pick_a_tool_by_its_name(self):
        # "taxonomy" as a term must not make the library called "taxonomy" win
        found = self.names(["taxonomic classification", "taxonomy"], ["kraken2"])
        self.assertEqual(found[0], "kraken2")

    def test_preferred_sources_before_docker_hub(self):
        found = self.names(["assembler", "long reads"], ["flye"])
        self.assertLess(found.index("flye"), found.index("someone/flye"))

    def test_native_architecture(self):
        r = {i["name"]: i for i in self.catalog.search([], ["flye"], arch="arm64")}
        self.assertTrue(r["flye"]["native"])
        self.assertFalse(r["someone/flye"]["native"])

    def test_relevance_parts(self):
        r = self.catalog.search(["genotyping"], ["freebayes"])[0]
        self.assertEqual(r["name"], "freebayes")
        self.assertGreater(r["tool_relevance"], 0)
        self.assertGreater(r["term_relevance"], 0)
        self.assertAlmostEqual(r["relevance"], r["tool_relevance"] + r["term_relevance"], places=2)

    def test_nothing(self):
        self.assertEqual(self.names(["xyzzy"]), [])

    def test_phrase(self):
        self.assertEqual(self.names(["single-cell RNA-seq"])[0], "bioconductor-scrnaseq")


if __name__ == "__main__":
    unittest.main()
