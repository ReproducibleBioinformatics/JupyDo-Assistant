"""The conversation: from what the user writes to 1-3 images of the catalog.

At every user message:
  1. the model turns the whole conversation into search keywords, of two kinds: terms
     of the analysis ("variant calling", "long read") and names of well-known tools
     ("bcftools");
  2. the code searches the catalog with them (assistant/catalog.py);
  3. the CODE decides what comes next: if the request is still vague (the model could
     name no well-known tool for it) and too many tools fit the analysis (counting only
     the terms), and fewer than MAX_QUESTIONS were asked, the model must ask ONE question (it writes the question and 2-4 possible
     answers itself, looking at the candidates); otherwise it must choose 1-3 tools,
     ONLY among the candidates: the JSON schema lets it write only their names.
The same tool can be in the catalog more than once (e.g. BioContainers and StaPH-B):
the model sees it once, and the reply gives the best image plus the others.
The assistant always writes in English, whatever the user's language.
Python 3.9 compatible.
"""
import logging

from .llm import LLMError

log = logging.getLogger("assistant")

CANDIDATES = 20       # images taken from the search
TOOLS_SHOWN = 12      # different tools shown to the model
STRONG = 0.75         # a tool fits the analysis well if its term relevance is >= 75% of the best
MAX_STRONG = 3        # more tools than this fit well (and no tool named): ask a question
MAX_QUESTIONS = 2     # then choose anyway

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
- "I need to do metagenomics" -> {"terms": ["metagenomics", "metagenome", "microbiome"], "tools": []}
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
Prefer tools that do exactly what was asked. For each one, write one short sentence \
saying why it fits, about THAT tool. Then a short message for the user. Write in English.
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


def by_tool(found):
    """The search results grouped by tool name, best first: [(name, [images, best first])]."""
    groups = {}
    for image in found:
        groups.setdefault(image["name"], []).append(image)
    return list(groups.items())[:TOOLS_SHOWN]


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
        best = max(images[0]["term_relevance"] for _, images in groups)
        strong = [n for n, images in groups if best > 0 and images[0]["term_relevance"] >= STRONG * best]
        log.info("keywords %s -> %d tools, %d fit the analysis well", keywords, len(groups), len(strong))
        if not tools and len(strong) > MAX_STRONG and self.questions < MAX_QUESTIONS:
            question, options = await self._ask(groups)
            if question:
                self.questions += 1
                return self._reply({"type": "question", "text": question, "options": options,
                                    "keywords": keywords})
        return self._reply(await self._choose(groups, keywords))

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
