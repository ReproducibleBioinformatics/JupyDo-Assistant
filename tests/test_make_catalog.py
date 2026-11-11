"""The rules of scripts/make-catalog.py that choose tags and filter images, without
internet (the registry answers are made here)."""
import importlib.util
import os
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("make_catalog", os.path.join(HERE, "..", "scripts", "make-catalog.py"))
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)


def tag(name, archs=("amd64",), updated="2026-01-01"):
    return {"name": name, "last_updated": updated + "T00:00:00Z",
            "images": [{"os": "linux", "architecture": a} for a in archs]}


class Versions(unittest.TestCase):
    def test_version_key_compares_numbers(self):
        tags = ["2.9--h1_0", "2.31.1--h2_3", "2.27.1--h3_9", "0.1.19--h4_15"]
        self.assertEqual(max(tags, key=mc.version_key), "2.31.1--h2_3")

    def test_version_key_then_build(self):
        self.assertGreater(mc.version_key("1.25--h9dcdb79_1"), mc.version_key("1.25--h9dcdb79_0"))

    def test_development_tags(self):
        for t in ("devel-r-4.6.1", "develop", "test", "v1.2-rc1", "2.0beta", "1.0-dev", "nightly-2026"):
            self.assertTrue(mc.DEV_TAG.search(t), t)
        for t in ("RELEASE_3_22", "5.3.0", "latest_arm64", "contest", "devtools-1.0"):
            self.assertFalse(mc.DEV_TAG.search(t.lower()), t)


class Words(unittest.TestCase):
    def test_whole_word(self):
        self.assertTrue(mc.has_word("cran", "Builds of CRAN releases of Seurat"))
        self.assertFalse(mc.has_word("cran", "crane operator"))
        self.assertTrue(mc.has_word("single-cell", "Single Cell RNA"))
        self.assertTrue(mc.has_word("r", "R 4.4 with RStudio"))
        self.assertFalse(mc.has_word("r", "crane"))

    def test_clean(self):
        self.assertEqual(mc.clean("  a\n b  "), "a b")
        self.assertTrue(mc.clean("word " * 200).endswith("..."))

    def test_quay_text(self):
        md = "# Samtools\n\n> Tools for SAM files\n\n> Licence: MIT\n"
        self.assertEqual(mc.quay_text(md), "Tools for SAM files")
        self.assertEqual(mc.quay_text("# Title\n\nA platform for microbiome analysis.\n"),
                         "A platform for microbiome analysis.")


class HubLatest(unittest.TestCase):
    def latest(self, tags):
        with mock.patch.object(mc, "get", lambda url, tries=6: {"results": tags}), \
                mock.patch.object(mc.time, "sleep", lambda s: None):   # no internet, no waiting
            return mc.hub_latest("org/repo")

    def test_plain_version_first_the_highest(self):
        t = self.latest([tag("gh1100-ultima-gpu"), tag("1.9.0"), tag("1.10.0", updated="2025-01-01"), tag("latest")])
        self.assertEqual(t["tag"], "1.10.0")

    def test_version_with_suffix_before_other_names(self):
        t = self.latest([tag("nightly"), tag("main"), tag("2.17.1-viral")])
        self.assertEqual(t["tag"], "2.17.1-viral")

    def test_latest_before_development(self):
        self.assertEqual(self.latest([tag("devel"), tag("latest")])["tag"], "latest")

    def test_only_amd64_or_arm64(self):
        self.assertIsNone(self.latest([tag("1.0", archs=("ppc64le",))]))
        t = self.latest([tag("1.0", archs=("arm64", "amd64", "s390x"))])
        self.assertEqual(t["archs"], ["amd64", "arm64"])


if __name__ == "__main__":
    unittest.main()
