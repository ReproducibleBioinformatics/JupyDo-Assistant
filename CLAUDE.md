# JupyDo Assistant

A small AI service for JupyDo (a JupyterHub-based platform that turns any Docker image
into a reproducible JupyterLab environment). It helps users who do not know what Docker
is: they describe their analysis in plain words, the assistant asks follow-up questions
and narrows the choice down to 1-3 container images from a fixed catalog. The chosen
image is then written into JupyDo's spawn form ("Custom Image" field) and JupyDo builds
it as usual.

This repository is separate from JupyDo (same model as the Genome Index Service,
github.com/ReproducibleBioinformatics/Index_builderGUI): JupyDo only loads it as a hub
service.

## Decisions already taken (do not change without asking)

- Must run on ordinary computers WITHOUT a GPU (CPU only, amd64 and arm64).
- Model: Qwen2.5-1.5B-Instruct, quantized GGUF (~1 GB, Apache 2.0). A copy is kept in this
  repository's GitHub releases with its checksum. JupyDo downloads it in first_start.sh
  (it must be ready at the first click, no download on first use).
- Runtime: llama.cpp server. Built by a GitHub Action from OUR fork of llama.cpp at a pinned
  commit, for amd64 and arm64, published on ghcr.io. Nothing is pulled from third parties
  at runtime.
- The llama.cpp container is started ON DEMAND through the Docker socket (like JupyDo's job
  queue starts jobs): it starts when the user clicks "Help me choose", and is stopped when
  the user confirms an image. Several users share one instance (count open sessions; stop
  when the last one closes). Safety net: a session with no messages for 10 minutes is
  closed (user closed the tab without choosing).
- One model only, no embedding model: the LLM extracts search keywords from the
  conversation, the service searches the catalog in pure Python, the LLM chooses ONLY among
  the catalog results (it can never propose an image that is not in the catalog).
- The model writes its own clarifying questions, but the CODE decides when it must ask
  (too many candidates left) instead of answering, one question at a time. The model always
  answers in JSON that the code validates.
- The service itself (chat page + catalog search) is small and always on; only llama.cpp
  is heavy and on demand.
- UI: JupyDo's spawn form shows a link like "Not sure what Docker is? Ask our assistant",
  which opens the service's page in a smaller panel over the form. On "Use this image" the
  page sends a message to the form (e.g. postMessage with the image name); the form switches
  to "Custom Image", fills the field and closes the panel. The form knows nothing else about
  the assistant; if the service is not installed, the link does not appear.
- JupyDo services are tornado apps with HubOAuthenticated, one port each (this one: 10108,
  path /services/assistant/), Python 3.9 compatible (the hub image is python:3.9).

## Catalog (first thing to build)

Script: `scripts/make-catalog.py`, derived from `scripts/reference/make-lists.py` (the
image selection of JupyDo's large-scale test). Same selection rules:
- BioContainers (quay.io/biocontainers): the 3000 most used single tools (no mulled-v2),
  by quay.io popularity or, if quay.io gives none, Bioconda downloads (anaconda.org).
- StaPH-B: all repositories of the staphb organization on Docker Hub.
- Docker Hub: most pulled repositories found by a search for "bioinformatics"
  (BioContainers and StaPH-B left out).
Differences from make-lists.py:
- Also store a DESCRIPTION for each image: Bioconda package summary from anaconda.org for
  BioContainers; the repository's short description from Docker Hub for StaPH-B and the
  search results. Images without a description are left out.
- Store the available architectures (amd64, arm64, both): the assistant prefers native
  images and warns when an image would run emulated.
- Store the most recent tag at build time (no live lookup at runtime). No digests: the
  catalog suggests, JupyDo pins the digest when it builds the environment.
- Output: `catalog.json` (not TSV).
- Runs in a scheduled GitHub Action every 3 months (must finish within 6 hours). Docker
  Hub login from repository secrets in the Action; from `docker login` when run locally.

## How to work with me (Luca)

- Everything technical in ENGLISH: code, comments, output messages, file names.
- Short, direct explanations, one thing at a time. Explain a decision before asking me to
  choose.
- If a request is unclear, ask BEFORE implementing.
- No changes I did not ask for (no defaults or extras added on your own initiative):
  propose them and ask first.
- Long commands (catalog build, tests) run in the foreground, never in the background
  with nohup or &, so I can see when they finish.
