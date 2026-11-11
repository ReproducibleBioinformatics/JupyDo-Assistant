#!/usr/bin/env python3
"""Builds the image catalog of the JupyDo assistant (catalog.json), from public
registries, with the same selection rules as the large-scale test of jupydo-builder
(scripts/bigtest/make-lists.py) and no image chosen by hand:

  biocontainers  the N MOST USED tools of quay.io/biocontainers (single tools: the
                 "mulled-v2-..." combinations of several packages are left out);
                 use = quay.io's popularity index of the repository or, if quay.io
                 does not give it, the total downloads of the matching Bioconda
                 package on anaconda.org; for each tool, its most recent tag
  staphb         ALL the repositories of the StaPH-B organization on Docker Hub;
                 for each, its most recent version tag
  dockerhub      the K most pulled Docker Hub repositories found by a search for
                 "bioinformatics" (repositories of the two sets above left out); for
                 each, its most recent version tag

Differences from make-lists.py (the catalog suggests images, it does not test them):
  - every image has a DESCRIPTION (what the tool does): the summary of the Bioconda
    package (anaconda.org) for BioContainers, the repository's short description for
    Docker Hub. Images without one are left out: the assistant could not use them.
  - every image has its ARCHITECTURES (amd64, arm64 or both), read from the image's
    own metadata, so the assistant prefers native images and warns about emulated
    ones. Images published for neither are left out.
  - no digest: JupyDo pins the image when it builds the environment.
  - the output is one JSON file, not a .tsv.

Usage: scripts/make-catalog.py [--n-biocontainers 3000] [--top-dockerhub 100]
                               [--out catalog.json] [--workers 8]
Needs internet (quay.io, anaconda.org, hub.docker.com). Docker Hub shows anonymous
requests only the first 100 results of any list: the requests are made with the
account in DOCKERHUB_USERNAME + DOCKERHUB_TOKEN (GitHub Actions secrets) or, if
those are not set, with the account of `docker login`.
A set finished earlier the same day with the same parameters is reused (files
.partial-<date>-<set>.json next to the output): a stopped run restarts from there.
"""
import argparse
import base64
import concurrent.futures
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "JupyDo-assistant-catalog/1.0"}
ARCHS = ("amd64", "arm64")   # the architectures JupyDo runs on
SCHEMA = 1                   # version of the format of catalog.json


def hub_credentials():
    """User and token for Docker Hub: DOCKERHUB_USERNAME + DOCKERHUB_TOKEN if set,
    else the ones saved by `docker login` (~/.docker/config.json or the credential
    helper it names). (None, None) if there are none."""
    if os.environ.get("DOCKERHUB_USERNAME") and os.environ.get("DOCKERHUB_TOKEN"):
        return os.environ["DOCKERHUB_USERNAME"], os.environ["DOCKERHUB_TOKEN"]
    try:
        with open(os.path.expanduser("~/.docker/config.json")) as f:
            cfg = json.load(f)
    except (FileNotFoundError, ValueError):
        return None, None
    server = "https://index.docker.io/v1/"
    auth = (cfg.get("auths", {}).get(server) or {}).get("auth")
    if auth:
        user, _, secret = base64.b64decode(auth).decode().partition(":")
        return user, secret
    helper = (cfg.get("credHelpers") or {}).get(server) or cfg.get("credsStore")
    if helper:
        try:
            r = subprocess.run(["docker-credential-" + helper, "get"], input=server,
                               capture_output=True, text=True)
        except FileNotFoundError:
            return None, None
        if r.returncode == 0:
            c = json.loads(r.stdout)
            return c.get("Username"), c.get("Secret")
    return None, None


HUB_JWT = None


def hub_login():
    global HUB_JWT
    user, secret = hub_credentials()
    if not user:
        print("  no Docker Hub account found: Docker Hub lists stop at 100 results without it", file=sys.stderr)
        return
    r = subprocess.run(["curl", "-s", "--max-time", "60", "-H", "Content-Type: application/json",
                        "-d", json.dumps({"username": user, "password": secret}),
                        "https://hub.docker.com/v2/users/login"], capture_output=True, text=True)
    try:
        HUB_JWT = json.loads(r.stdout).get("token")
    except ValueError:
        HUB_JWT = None
    print(f"  Docker Hub: {'logged in as ' + user if HUB_JWT else 'login FAILED, going on anonymously'}",
          file=sys.stderr)


def get_curl(url, tries=6):
    """Docker Hub (behind Cloudflare) refuses Python's own HTTPS client with 403 but
    accepts curl: its requests go through curl, with the Docker Hub account."""
    for i in range(tries):
        auth = ["-H", "Authorization: Bearer " + HUB_JWT] if HUB_JWT else []
        r = subprocess.run(["curl", "-s", "--max-time", "60", "-A", UA["User-Agent"], *auth,
                            "-w", "\n%{http_code}", url], capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        if code == "200":
            return json.loads(body)
        if code == "404":
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        if i < tries - 1:
            wait = 20 * (i + 1)
            print(f"\n  (registry says {code or 'no answer'}: waiting {wait}s)", file=sys.stderr)
            time.sleep(wait)
    hint = (" (Docker Hub shows anonymous requests only the first 100 results: set DOCKERHUB_USERNAME"
            " and DOCKERHUB_TOKEN, or run `docker login`)" if code == "403" and not HUB_JWT else "")
    raise RuntimeError(f"{url}: HTTP {code or 'no answer'} after {tries} tries{hint}")


def get(url, tries=6):
    if url.startswith("https://hub.docker.com/"):
        return get_curl(url, tries)
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503, 504) and i < tries - 1:
                wait = int(e.headers.get("Retry-After") or 0) or 20 * (i + 1)
                print(f"\n  (registry says {e.code}: waiting {wait}s)", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if i < tries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise


def clean(text, limit=400):
    """One line of plain text, at most `limit` characters (cut at a word)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text


# ---------------------------------------------------------------- quay.io
def quay_repositories(namespace):
    """{name: (popularity or None, description)} of every public repository of the namespace."""
    repos, page = {}, None
    while True:
        url = f"https://quay.io/api/v1/repository?namespace={namespace}&public=true&popularity=true"
        if page:
            url += "&next_page=" + urllib.parse.quote(page)
        data = get(url)
        for r in data.get("repositories", []):
            repos[r["name"]] = (r.get("popularity"), r.get("description") or "")
        page = data.get("next_page")
        print(f"\r  quay.io/{namespace}: {len(repos)} repositories", end="", file=sys.stderr)
        if not page:
            print(file=sys.stderr)
            return repos


def quay_summary(markdown):
    """The tool's one-line summary in quay.io's description of a BioContainers
    repository (the first '> ' line that is not the licence)."""
    for line in markdown.splitlines():
        if line.startswith("> ") and not line.startswith("> Licen"):
            return line[2:]
    return ""


def bioconda_package(name):
    """The Bioconda package of anaconda.org (summary, home, licence, downloads), or {}."""
    try:
        return get(f"https://api.anaconda.org/package/bioconda/{urllib.parse.quote(name)}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def quay_architectures(repo, digest, tries=4):
    """The REAL architectures of an image of quay.io, from its metadata (registry API):
    the platforms of a multi-architecture index, or the 'architecture' of the
    configuration of a single-architecture image. Nothing is downloaded."""
    accept = ("application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, "
              "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json")
    for i in range(tries):
        try:
            tok = get(f"https://quay.io/v2/auth?service=quay.io&scope=repository:{repo}:pull")["token"]
            def reg(path, acc):
                req = urllib.request.Request(f"https://quay.io/v2/{repo}/{path}",
                                             headers={"Authorization": "Bearer " + tok, "Accept": acc})
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            m = reg(f"manifests/{digest}", accept)
            if "manifests" in m:
                return {p.get("platform", {}).get("architecture") for p in m["manifests"]
                        if p.get("platform", {}).get("os") == "linux"}
            return {reg(f"blobs/{m['config']['digest']}", "*/*").get("architecture")}
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
            if i == tries - 1:
                raise
            time.sleep(10 * (i + 1))


def quay_latest(namespace, name):
    """The most recent tag of the tool and the architectures of its image, or None."""
    data = get(f"https://quay.io/api/v1/repository/{namespace}/{name}/tag/?onlyActiveTags=true&limit=100")
    tags = sorted((t for t in data.get("tags", []) if t.get("manifest_digest") and t.get("name") != "latest"),
                  key=lambda t: t.get("start_ts", 0), reverse=True)
    if not tags:
        return None
    t = tags[0]
    archs = quay_architectures(f"{namespace}/{name}", t["manifest_digest"])
    return {"tag": t["name"], "archs": sorted(a for a in archs if a in ARCHS),
            "updated": datetime.datetime.fromtimestamp(t.get("start_ts", 0), datetime.timezone.utc).strftime("%Y-%m-%d")}


# ---------------------------------------------------------------- Docker Hub
def hub_org_repositories(org):
    """{name: (pull count, description)} of every repository of the organization."""
    repos, url = {}, f"https://hub.docker.com/v2/repositories/{org}/?page_size=100"
    while url:
        data = get(url)
        for r in data.get("results", []):
            repos[r["name"]] = (r.get("pull_count", 0), r.get("description") or "")
        url = data.get("next")
        time.sleep(1)
    return repos


def hub_search(query, pages):
    """{repository: (pull count, short description)} of the search results."""
    found = {}
    for page in range(1, pages + 1):
        try:
            data = get(f"https://hub.docker.com/v2/search/repositories/?query={urllib.parse.quote(query)}"
                       f"&page_size=50&page={page}")
        except urllib.error.HTTPError as e:
            if e.code == 404:  # past the last page
                break
            raise
        for r in data.get("results", []):
            name = r["repo_name"] if "/" in r["repo_name"] else "library/" + r["repo_name"]
            found[name] = (r.get("pull_count", 0), r.get("short_description") or "")
        if not data.get("next"):
            break
        time.sleep(0.5)
    return found


def hub_latest(repo):
    """Most recent tag (other than 'latest') with a linux image for amd64 or arm64,
    and the architectures it is published for; 'latest' if there is no other."""
    data = get(f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=50&ordering=last_updated")
    time.sleep(0.5)
    def archs(t):
        return sorted({i.get("architecture") for i in t.get("images") or []
                       if i.get("os") == "linux" and i.get("architecture") in ARCHS})
    for want_latest in (False, True):
        for t in data.get("results", []):
            if (t["name"] == "latest") == want_latest and archs(t):
                return {"tag": t["name"], "archs": archs(t), "updated": (t.get("last_updated") or "")[:10]}
    return None


def short(repo):
    return repo[len("library/"):] if repo.startswith("library/") else repo


# ---------------------------------------------------------------- the three sets
def set_biocontainers(a, stats):
    repos = quay_repositories("biocontainers")
    tools = {n: v for n, v in repos.items() if not n.startswith("mulled-v2-")}
    if sum(1 for p, _ in tools.values() if p) >= len(tools) // 2:
        use, measure = {n: p or 0 for n, (p, _) in tools.items()}, "quay.io popularity"
        packages = {}
    else:  # quay.io gives no popularity: downloads of the matching Bioconda package
        print("  quay.io gives no popularity: using Bioconda downloads (anaconda.org)", file=sys.stderr)
        measure = "Bioconda downloads (anaconda.org)"
        with concurrent.futures.ThreadPoolExecutor(a.workers) as ex:
            packages = dict(zip(tools, ex.map(bioconda_package, tools)))
        use = {n: packages[n].get("ndownloads", 0) for n in tools}

    def lookup(name):
        """(entry, None) or (None, the reason it is left out)."""
        try:
            pkg = packages.get(name)
            if pkg is None:
                pkg = bioconda_package(name)
            description = clean(pkg.get("summary"))
            origin = "bioconda"
            if not description:
                description, origin = clean(quay_summary(tools[name][1])), "quay.io"
            if not description:
                return None, "no description"
            t = quay_latest("biocontainers", name)
            if not t:
                return None, "no tag"
            if not t["archs"]:
                return None, "not amd64 nor arm64"
            return {"source": "biocontainers", "name": name, "image": f"quay.io/biocontainers/{name}:{t['tag']}",
                    "repository": f"quay.io/biocontainers/{name}", "tag": t["tag"], "updated": t["updated"],
                    "archs": t["archs"], "description": description, "description_from": origin, "homepage": pkg.get("home") or "",
                    "license": pkg.get("license") or "", "use": use[name]}, None
        except Exception as e:  # noqa: BLE001 - one unreadable tool must not stop hours of work
            return None, "error: " + type(e).__name__

    ranked = sorted(use, key=lambda n: (-use[n], n))
    entries, examined = [], 0
    with concurrent.futures.ThreadPoolExecutor(a.workers) as ex:
        while len(entries) < a.n_biocontainers and examined < len(ranked):
            # a batch at a time, in order of use: the first N usable tools, no more
            batch = ranked[examined:examined + max(a.workers, a.n_biocontainers - len(entries))]
            batch = batch[:a.workers * 8]
            examined += len(batch)
            for entry, reason in ex.map(lookup, batch):
                if entry and len(entries) < a.n_biocontainers:
                    entries.append(entry)
                elif reason:
                    stats[reason] = stats.get(reason, 0) + 1
            print(f"\r  {len(entries)}/{a.n_biocontainers} (looked at {examined} of {len(ranked)})",
                  end="", file=sys.stderr)
    print(file=sys.stderr)
    stats["measure"], stats["examined"] = measure, examined
    return entries


def hub_entry(source, repo, pulls, description, stats):
    """Many repositories have no short description on Docker Hub (StaPH-B's pages all
    show the same generic README): the summary of the Bioconda package with the same
    name is used instead, and the entry says so."""
    description, origin = clean(description), "docker hub"
    if not description:
        try:
            description, origin = clean(bioconda_package(short(repo).split("/")[-1]).get("summary")), "bioconda"
        except Exception:  # noqa: BLE001
            description = ""
    if not description:
        stats["no description"] = stats.get("no description", 0) + 1
        return None
    try:
        t = hub_latest(repo)
    except Exception as e:  # noqa: BLE001
        stats["error: " + type(e).__name__] = stats.get("error: " + type(e).__name__, 0) + 1
        return None
    if not t:
        stats["not amd64 nor arm64"] = stats.get("not amd64 nor arm64", 0) + 1
        return None
    return {"source": source, "name": short(repo).split("/")[-1], "image": f"{short(repo)}:{t['tag']}",
            "repository": short(repo), "tag": t["tag"], "updated": t["updated"], "archs": t["archs"],
            "description": description, "description_from": origin, "homepage": f"https://hub.docker.com/r/{repo}", "license": "",
            "use": pulls}


def set_staphb(a, stats):
    repos = hub_org_repositories("staphb")
    entries = []
    for i, name in enumerate(sorted(repos, key=lambda n: (-repos[n][0], n)), 1):
        e = hub_entry("staphb", f"staphb/{name}", *repos[name], stats)
        if e:
            entries.append(e)
        print(f"\r  {i}/{len(repos)}", end="", file=sys.stderr)
    print(file=sys.stderr)
    stats["measure"], stats["examined"] = "Docker Hub pulls", len(repos)
    return entries


def set_dockerhub(a, stats):
    found = hub_search("bioinformatics", a.search_pages)
    ranked = sorted(((p, r, d) for r, (p, d) in found.items()
                     if not r.startswith(("staphb/", "biocontainers/"))), key=lambda x: (-x[0], x[1]))
    entries, examined = [], 0
    for pulls, repo, description in ranked:
        if len(entries) >= a.top_dockerhub:
            break
        examined += 1
        e = hub_entry("dockerhub", repo, pulls, description, stats)
        if e:
            entries.append(e)
        print(f"\r  {len(entries)}/{a.top_dockerhub} (looked at {examined} of {len(ranked)})", end="", file=sys.stderr)
    print(file=sys.stderr)
    stats["measure"], stats["examined"] = "Docker Hub pulls", examined
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n-biocontainers", type=int, default=3000)
    ap.add_argument("--top-dockerhub", type=int, default=100)
    ap.add_argument("--search-pages", type=int, default=20)
    ap.add_argument("--workers", type=int, default=8, help="parallel requests to quay.io and anaconda.org")
    ap.add_argument("--out", default="catalog.json")
    a = ap.parse_args()
    outdir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(outdir, exist_ok=True)
    today = datetime.date.today().isoformat()
    images, stats = [], {}

    def run(name, key, build):
        """The set, reused if it was finished earlier today with the same parameters."""
        path = os.path.join(outdir, f".partial-{today}-{name}.json")
        try:
            with open(path) as f:
                c = json.load(f)
            if c.get("key") == key:
                print(f"  (already done earlier today: {len(c['images'])} images, reused)", file=sys.stderr)
                stats[name] = c["stats"]
                return c["images"]
        except (FileNotFoundError, ValueError):
            pass
        stats[name] = {}
        entries = build(a, stats[name])
        with open(path, "w") as f:
            json.dump({"key": key, "images": entries, "stats": stats[name]}, f)
        return entries

    print("biocontainers  quay.io, most used", file=sys.stderr)
    images += run("biocontainers", a.n_biocontainers, set_biocontainers)
    hub_login()
    print("staphb  Docker Hub, all", file=sys.stderr)
    images += run("staphb", 0, set_staphb)
    print('dockerhub  Docker Hub search "bioinformatics", by pulls', file=sys.stderr)
    images += run("dockerhub", [a.top_dockerhub, a.search_pages], set_dockerhub)

    counts = {s: sum(1 for i in images if i["source"] == s) for s in ("biocontainers", "staphb", "dockerhub")}
    catalog = {"schema": SCHEMA, "made": today, "made_by": "scripts/make-catalog.py",
               "rules": {"n_biocontainers": a.n_biocontainers, "top_dockerhub": a.top_dockerhub,
                         "search_pages": a.search_pages},
               "counts": counts, "left_out": stats, "images": images}
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(catalog, f, indent=1, ensure_ascii=False)
    os.replace(tmp, a.out)
    for name in counts:
        try:
            os.remove(os.path.join(outdir, f".partial-{today}-{name}.json"))
        except FileNotFoundError:
            pass
    print(f"{a.out}: {len(images)} images {counts}")


if __name__ == "__main__":
    main()
