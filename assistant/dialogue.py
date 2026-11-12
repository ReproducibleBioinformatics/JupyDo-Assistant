"""The conversation: from what the user writes to 1-3 images of the catalog.

At every user message:
  1. the model turns the whole conversation into search keywords, of two kinds: terms
     of the analysis ("variant calling", "long read") and names of well-known tools
     ("bcftools");
  2. the code searches the catalog with them (assistant/catalog.py);
  3. the CODE decides what comes next:
     - if some of the tools the model named are in the catalog, those are the answer
       (at most 3, each with its best image of that exact name): the model knows the
       standard tools of an analysis well (DESeq2, DADA2...) but chooses badly from a
       list, even a short one; it only explains them;
     - otherwise, at the first message (a vague request), the model asks ONE question
       (it writes the question and 2-4 possible answers, looking at the candidates);
     - otherwise it chooses 1-3 tools ONLY among the candidates: the JSON schema lets it
       write only their names.
The same tool can be in the catalog more than once (e.g. BioContainers and StaPH-B):
the model sees it once, and the reply gives the best image plus the others.
The assistant always writes in English, whatever the user's language.
Python 3.9 compatible.
"""
import logging
import re

from .catalog import words
from .llm import LLMError

log = logging.getLogger("assistant")

CANDIDATES = 80       # images taken from the search (the same tool can be there many times)
TOOLS_SHOWN = 12      # different tools shown to the model
# the same tool from several sources: the makers' own image, then BioContainers (usually
# the newest version), then StaPH-B, then any other Docker Hub repository
SOURCE_ORDER = {"trusted": 0, "biocontainers": 1, "staphb": 2, "dockerhub": 3}
MAX_QUESTIONS = 1     # then choose anyway

KEYWORDS_PROMPT = """You turn a biologist's request into search keywords for a catalog of \
bioinformatics software (each entry: tool name + one-line description, in English).
Give, in ENGLISH whatever the user's language:
- "terms": 3-8 technical terms of the analysis: the analysis steps, the data types, \
the technologies, with synonyms. No tool names here.
- "tools": 0-4 names of well-known tools for exactly this analysis (empty if unsure).
Use the whole conversation: later messages add details to the first one.
Examples:
- "ho letture nanopore di un batterio e voglio ricostruire il genoma" -> \
{"terms": ["genome assembly", "assembler", "long read", "nanopore", "bacterial genome"], "tools": ["flye", "unicycler"]}
- "I want to find differentially expressed genes from my RNA-seq counts" -> \
{"terms": ["differential expression", "rna-seq", "gene expression", "count matrix"], "tools": ["deseq2", "edger"]}
- "quality check of my fastq files" -> \
{"terms": ["quality control", "fastq", "sequencing reads", "read trimming"], "tools": ["fastqc", "multiqc", "fastp"]}
- "call variants on human exomes, starting from the fastq files" -> \
{"terms": ["read alignment", "variant calling", "exome", "human genome"], "tools": ["bwa", "gatk4", "samtools"]}
- "16S metabarcoding of soil samples" -> \
{"terms": ["metabarcoding", "amplicon", "16S", "microbiome", "taxonomy"], "tools": ["dada2", "qiime2", "cutadapt"]}
- "I need to do metagenomics" -> {"terms": ["metagenomics", "metagenome", "microbiome"], "tools": []}
When the conversation has several messages, a later message adds a detail to the first one: \
keep the main subject of the FIRST message in the terms, and add the new detail.
Answer ONLY with JSON."""

KEYWORDS_SCHEMA = {"type": "object", "properties": {
    "terms": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
    "tools": {"type": "array", "items": {"type": "string"}, "maxItems": 4}},
    "required": ["terms", "tools"]}

RETRY_NOTE = ("The terms {terms} found nothing in the catalog. Write different, more general terms "
              "(broader analysis names, other synonyms).")

ASK_PROMPT = """You help a biologist choose ONE software container for an analysis. \
These tools from the catalog fit the request so far:
{candidates}
They do different things, so you need ONE more piece of information. Look at what makes \
these tools DIFFERENT (the kind of input data, the organism, the sequencing technology, \
the step of the analysis, the programming language) and ask about that.
Rules:
- NEVER ask about something the user already said.
- One short, simple question, for someone who knows nothing about software.
- NEVER put tool names in the question or in the answers: the user does not know them.
- Give 2 to 4 short possible answers, about the user's data or goal.
- Write in English.
Example: tools for metagenomics -> {{"question": "What do you want to learn from your \
samples?", "options": ["Which species are there", "Which genes or functions are there", \
"Rebuild the genomes of the microbes"]}}
Answer ONLY with JSON: {{"question": "...", "options": ["...", "..."]}}"""

ASK_SCHEMA = {"type": "object", "properties": {
    "question": {"type": "string"},
    "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4}},
    "required": ["question", "options"]}

CHOOSE_PROMPT = """You help a biologist choose a software container for an analysis. \
Choose the 1 to 3 tools that best fit the request, ONLY from this list:
{candidates}
Prefer tools that do exactly what was asked. If the analysis has several steps and the \
user starts from raw data (e.g. FASTQ files), choose tools for the FIRST step, and say in \
the message which steps come next. For each tool, write one short sentence saying why it \
fits, about THAT tool. Then a short message for the user. Write in English.
Answer ONLY with JSON: {{"choices": [{{"tool": "...", "why": "..."}}], "message": "..."}}"""


def choose_schema(names):
    return {"type": "object", "properties": {
        "choices": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
            "type": "object", "properties": {"tool": {"type": "string", "enum": names}, "why": {"type": "string"}},
            "required": ["tool", "why"]}},
        "message": {"type": "string"}},
        "required": ["choices", "message"]}


# at the end of the messages too: a small model follows the language of the last message
ENGLISH = {"role": "system", "content": "Answer in English, with the JSON asked above."}

NOTHING = ("I could not find anything that fits in the catalog. Could you describe the analysis "
           "in other words (what data you have, what result you want)?")


def by_tool(found, limit=TOOLS_SHOWN):
    """The search results grouped by tool name, best tool first: [(name, [images])].
    Inside a group the images are ordered by trust, not by their description: by source
    (SOURCE_ORDER), then native to this machine, then the most used (a personal
    "rstudio" image whose description repeats more keywords must not beat rocker/rstudio)."""
    groups = {}
    for image in found:
        groups.setdefault(image["name"], []).append(image)
    for images in groups.values():
        images.sort(key=lambda i: (SOURCE_ORDER.get(i["source"], len(SOURCE_ORDER)), not i["native"],
                                   -(i.get("use") or 0)))
    return list(groups.items())[:limit]


def pick_named(catalog, tools):
    """For each tool the model named (in its order, at most 3), its most trusted image in
    the WHOLE catalog (not only among the search results): the tool with that exact name
    ("seurat" -> satijalab/seurat, not r-seurat), or else the most used tool with that
    name inside its name ("deseq2" -> bioconductor-deseq2, not r-empiricalfdr.deseq2)."""
    def bare(name):   # "Kb-Python" -> "kbpython" (every letter kept: "r-seurat" is not "seurat")
        return re.sub(r"[^a-z0-9]", "", name.lower())
    picked = []
    for tool in tools:
        if not bare(tool):
            continue
        groups = by_tool(catalog.search([], [tool], limit=500), limit=None)
        exact = [g for g in groups if bare(g[0]) == bare(tool)]
        inside = sorted((g for g in groups if bare(tool) in words(g[0])),
                        key=lambda g: (SOURCE_ORDER.get(g[1][0]["source"], 9), -(g[1][0].get("use") or 0)))
        for g in (exact or inside)[:1]:
            if g[0] not in [p[0] for p in picked]:
                picked.append(g)
    return picked[:3]


def listing(tools):
    return "\n".join(f"- {name}: {images[0]['description']}" for name, images in tools)


class Conversation:
    def __init__(self, catalog, llm):
        self.catalog, self.llm = catalog, llm
        self.history = []     # {"role": "user"|"assistant", "content": text}
        self.questions = 0    # questions asked so far

    async def keywords(self, extra=None):
        """(terms, tools) for the catalog search, from the whole conversation."""
        messages = [{"role": "system", "content": KEYWORDS_PROMPT}] + self.history
        if extra:
            messages.append({"role": "system", "content": extra})
        for _ in range(2):
            try:
                r = await self.llm.chat_json(messages, KEYWORDS_SCHEMA)
            except LLMError as e:
                log.warning("keywords: %s", e)
                continue
            clean = lambda xs: [x.strip() for x in xs if isinstance(x, str) and x.strip()]  # noqa: E731
            terms, tools = clean(r.get("terms", []))[:8], clean(r.get("tools", []))[:4]
            if terms or tools:
                return terms, tools
        raise LLMError("The model did not give usable search keywords.")

    async def answer(self, text):
        """The reply to a user message: {"type": "question"|"choices"|"nothing", ...}
        (a question comes with 'options', possible answers the page shows as buttons)."""
        self.history.append({"role": "user", "content": text.strip()[:2000]})
        terms, tools = await self.keywords()
        found = self.catalog.search(terms, tools, limit=CANDIDATES)
        if not found:
            terms, tools = await self.keywords(RETRY_NOTE.format(terms=terms))
            found = self.catalog.search(terms, tools, limit=CANDIDATES)
        keywords = {"terms": terms, "tools": tools}
        if not found:
            log.info("keywords %s -> nothing", keywords)
            return self._reply({"type": "nothing", "text": NOTHING, "keywords": keywords})
        groups = by_tool(found)
        # the tools the model named for this analysis, when the catalog has them: the model
        # knows the standard tools well, but chooses badly from a long list
        named = pick_named(self.catalog, tools)
        log.info("keywords %s -> %d tools, named and in the catalog: %s", keywords, len(groups),
                 [n for n, _ in named])
        if not named and len(self.history) == 1 and self.questions < MAX_QUESTIONS:
            question, options = await self._ask(groups)   # a vague first request
            if question:
                self.questions += 1
                return self._reply({"type": "question", "text": question, "options": options,
                                    "keywords": keywords})
        return self._reply(await self._choose(named or groups, keywords))

    async def _ask(self, groups):
        messages = ([{"role": "system", "content": ASK_PROMPT.format(candidates=listing(groups))}]
                    + self.history + [ENGLISH])
        try:
            r = await self.llm.chat_json(messages, ASK_SCHEMA, temperature=0.4)
        except LLMError as e:
            log.warning("question: %s", e)
            return "", []
        q = r.get("question", "")
        options = [o.strip() for o in r.get("options", []) if isinstance(o, str) and o.strip()][:4]
        return (q.strip() if isinstance(q, str) else ""), options

    async def _choose(self, groups, keywords):
        names = [n for n, _ in groups]
        messages = ([{"role": "system", "content": CHOOSE_PROMPT.format(candidates=listing(groups))}]
                    + self.history + [ENGLISH])
        picked, message = [], ""
        for _ in range(2):
            try:
                r = await self.llm.chat_json(messages, choose_schema(names))
            except LLMError as e:
                log.warning("choose: %s", e)
                continue
            message = r.get("message", "") if isinstance(r.get("message"), str) else ""
            for ch in r.get("choices", []):
                if ch.get("tool") in names and ch["tool"] not in [p[0] for p in picked]:
                    picked.append((ch["tool"], str(ch.get("why", "")).strip()))
            if picked:
                break
        if not picked:  # the model failed twice: the best matches of the search
            picked = [(n, "") for n in names[:3]]
        images_of = dict(groups)
        choices = []
        for name, why in picked[:3]:
            best, *others = images_of[name]
            choice = {k: best.get(k, "") for k in ("image", "name", "description", "source", "organization",
                                                   "archs", "native", "homepage", "license")}
            choice.update(why=why, also=[{"image": o["image"], "source": o["source"], "native": o["native"]}
                                         for o in others])
            choices.append(choice)
        return {"type": "choices", "text": message, "choices": choices, "keywords": keywords}

    def _reply(self, reply):
        text = reply["text"]
        if reply["type"] == "question" and reply.get("options"):
            text += " (" + " / ".join(reply["options"]) + ")"
        if reply["type"] == "choices":
            text += "\n" + "\n".join(f"- {c['name']}: {c['why']}" for c in reply["choices"])
        self.history.append({"role": "assistant", "content": text})
        return reply
