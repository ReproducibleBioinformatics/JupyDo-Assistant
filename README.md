# JupyDo Assistant

A small AI service for [JupyDo](https://github.com/ReproducibleBioinformatics): users
describe their analysis in plain words, the assistant asks follow-up questions and
suggests 1-3 container images from a fixed catalog. The chosen image is written into
JupyDo's spawn form and JupyDo builds the environment as usual.

It runs on ordinary computers without a GPU: a small language model (Qwen2.5-1.5B-Instruct,
quantized) served by llama.cpp, started only while someone is using the assistant.

## Catalog

`catalog.json` lists the images the assistant can suggest. It is rebuilt every 3 months
by a GitHub Action (`.github/workflows/catalog.yml`) with `scripts/make-catalog.py`,
from public registries and with fixed rules:

- all the tools of [BioContainers](https://biocontainers.pro) (quay.io)
- all the images of [StaPH-B](https://github.com/StaPH-B/docker-builds) (Docker Hub)
- all the images of a short list of trusted organizations, the makers of widely used
  tools (Oxford Nanopore, Broad Institute, DeepVariant, QIIME 2, Bioconductor, rocker,
  nf-core): see `TRUSTED` in the script. The list of organizations is chosen by hand,
  the images are not.
- the most pulled Docker Hub repositories for a set of search words (bioinformatics,
  bioconductor, cran, python, single-cell, ...): see `SEARCH_WORDS` in the script

Every entry has the image's description, its most recent version and the architectures
it is published for (amd64, arm64).

## Licenses of the images

The images in the catalog are made and published by third parties: this project only
lists their names, it does not build or redistribute them. Each image, and the software
inside it, has its own license, and some (for example 10x Genomics Cell Ranger) require
the user to accept the vendor's license before use. **Users are responsible for checking
and respecting the license of every image and software they use.**

The model is Qwen2.5-1.5B-Instruct by Alibaba Cloud (Qwen team), Apache License 2.0
(<https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF>), distributed unchanged with its
license file. llama.cpp is MIT licensed (<https://github.com/ggml-org/llama.cpp>).
