#!/usr/bin/env python3
"""Builds the image lists of the large-scale test (Fig. 1), from public registries,
with rules fixed in advance (no image is chosen by hand):

  A  biocontainers  the N MOST USED tools of quay.io/biocontainers (single tools: the
                    "mulled-v2-..." combinations of several packages are left out);
                    use = quay.io's popularity index of the repository or, if quay.io
                    does not give it, the total downloads of the matching Bioconda
                    package on anaconda.org (the list says which); for each tool, its
                    most recent tag
  B  staphb         ALL the repositories of the StaPH-B organization on Docker Hub;
                    for each, its most recent version tag with a linux/amd64 image
  C  dockerhub      the K most pulled Docker Hub repositories found by a search for
                    "bioinformatics" (repositories of sets A and B left out); for each,
                    its most recent version tag with a linux/amd64 image

Every image is pinned by digest (repository:tag@sha256:...), so the list names exactly
the images that were tested. Writes, in the output folder:
  bigtest-<date>-<arch>.tsv           set, image, repository, tag, updated, use (the list)
  bigtest-<date>-<arch>-sources.json  what the registries answered (to show the selection),
                                      and how far down each ranking the list had to go
Inside each set the images are in order of use: running only the first n images of
set A tests the n most used tools.

Usage: scripts/bigtest/make-lists.py [--arch amd64|arm64] [--n-biocontainers 1000]
                                     [--top-dockerhub 100] [--out lists/bigtest]
--arch: the images must be published for that architecture (same rules, same ranking:
the N most used that exist for it). The three sets are interleaved in the list.
Needs internet (quay.io, hub.docker.com). Docker Hub limits anonymous API calls:
the script waits and retries when told to slow down.
"""
import argparse
import base64
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "JupyDo-bigtest/1.0"}


def docker_login_credentials():
    """User and password (or token) saved by `docker login`: in ~/.docker/config.json
    or in the credential helper it names. (None, None) if there are none."""
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
        r = subprocess.run(["docker-credential-" + helper, "get"], input=server, capture_output=True, text=True)
        if r.returncode == 0:
            c = json.loads(r.stdout)
            return c.get("Username"), c.get("Secret")
    return None, None


HUB_JWT = None


def hub_login():
    """Docker Hub shows only the first 100 results of any list to anonymous requests:
    the requests are made with the account of `docker login`, if there is one."""
    global HUB_JWT
    user, secret = docker_login_credentials()
    if not user:
        print("  no `docker login` found: Docker Hub lists stop at 100 results without it", file=sys.stderr)
        return
    r = subprocess.run(["curl", "-s", "--max-time", "60", "-H", "Content-Type: application/json",
                        "-d", json.dumps({"username": user, "password": secret}),
                        "https://hub.docker.com/v2/users/login"], capture_output=True, text=True)
    try:
        HUB_JWT = json.loads(r.stdout).get("token")
    except ValueError:
        HUB_JWT = None
    print(f"  Docker Hub: {'logged in as ' + user if HUB_JWT else 'login with the docker login account FAILED'}",
          file=sys.stderr)


def get_curl(url, tries=6):
    """Docker Hub (behind Cloudflare) refuses Python's own HTTPS client with 403 but
    accepts curl: its requests go through curl, with the docker login account."""
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
            print(f"  (registry says {code or 'no answer'}: waiting {wait}s)", file=sys.stderr)
            time.sleep(wait)
    hint = (" (Docker Hub shows anonymous requests only the first 100 results: run `docker login` and"
            " start again)" if code == "403" and not HUB_JWT else "")
    raise RuntimeError(f"{url}: HTTP {code or 'no answer'} after {tries} tries{hint}")


def get(url, tries=6):
    if url.startswith("https://hub.docker.com/"):
        return get_curl(url, tries)
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # Docker Hub sometimes answers 403 instead of 429 to many quick requests
            if e.code in (403, 429, 500, 502, 503, 504) and i < tries - 1:
                wait = int(e.headers.get("Retry-After") or 0) or 20 * (i + 1)
                print(f"  (registry says {e.code}: waiting {wait}s)", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if i < tries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise


# ---------------------------------------------------------------- quay.io
def quay_repositories(namespace):
    """{name: popularity or None} of every public repository of the namespace."""
    repos, page = {}, None
    while True:
        url = f"https://quay.io/api/v1/repository?namespace={namespace}&public=true&popularity=true"
        if page:
            url += "&next_page=" + urllib.parse.quote(page)
        data = get(url)
        for r in data.get("repositories", []):
            repos[r["name"]] = r.get("popularity")
        page = data.get("next_page")
        print(f"\r  quay.io/{namespace}: {len(repos)} repositories", end="", file=sys.stderr)
        if not page:
            print(file=sys.stderr)
            return repos


def bioconda_downloads(name):
    """Total downloads of the Bioconda package (all versions and files), or 0."""
    try:
        data = get(f"https://api.anaconda.org/package/bioconda/{urllib.parse.quote(name)}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 0
        raise
    return sum(f.get("ndownloads", 0) for f in data.get("files", []))


ARCH = "amd64"   # the architecture the list is for (--arch)
ARCH_SEEN = {}   # tool -> architectures of its most recent image (saved with the sources)


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
    """The most recent tag of the tool, if its image is published for ARCH: the
    architecture is read from the image's own metadata (not from the tag's name)."""
    data = get(f"https://quay.io/api/v1/repository/{namespace}/{name}/tag/?onlyActiveTags=true&limit=100")
    tags = sorted((t for t in data.get("tags", []) if t.get("manifest_digest") and t.get("name") != "latest"),
                  key=lambda t: t.get("start_ts", 0), reverse=True)
    if not tags:
        return None
    t = tags[0]   # the most recent version of the tool
    try:
        archs = quay_architectures(f"{namespace}/{name}", t["manifest_digest"])
    except Exception as e:  # noqa: BLE001 - one unreadable tool must not stop hours of work
        ARCH_SEEN[name] = ["error:" + type(e).__name__]
        return None
    ARCH_SEEN[name] = sorted(a for a in archs if a)
    if ARCH not in archs:
        return None
    return {"tag": t["name"], "digest": t["manifest_digest"],
            "updated": datetime.datetime.fromtimestamp(t.get("start_ts", 0), datetime.timezone.utc).strftime("%Y-%m-%d")}


# ---------------------------------------------------------------- Docker Hub
def hub_org_repositories(org):
    """{name: pull count} of every repository of the organization."""
    repos, url = {}, f"https://hub.docker.com/v2/repositories/{org}/?page_size=50"
    while url:
        data = get(url)
        for r in data.get("results", []):
            repos[r["name"]] = r.get("pull_count", 0)
        url = data.get("next")
        time.sleep(1)
    return repos


def hub_search(query, pages):
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
            found[name] = r.get("pull_count", 0)
        if not data.get("next"):
            break
        time.sleep(0.5)
    return found


def hub_latest(repo):
    """Most recent tag (other than 'latest') with a linux image for ARCH; 'latest' if none."""
    data = get(f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=50&ordering=last_updated")
    time.sleep(0.5)
    tags = data.get("results", [])
    def for_arch(t):
        return any(i.get("os") == "linux" and i.get("architecture") == ARCH for i in t.get("images") or [])
    for want_latest in (False, True):
        for t in tags:
            if (t["name"] == "latest") == want_latest and for_arch(t) and t.get("digest"):
                return {"tag": t["name"], "digest": t["digest"], "updated": (t.get("last_updated") or "")[:10]}
    return None


def ref(repo, tag, digest):
    short = repo[len("library/"):] if repo.startswith("library/") else repo
    return f"{short}:{tag}@{digest}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-biocontainers", type=int, default=1000)
    ap.add_argument("--top-dockerhub", type=int, default=100)
    ap.add_argument("--search-pages", type=int, default=20)
    ap.add_argument("--out", default="lists/bigtest")
    ap.add_argument("--arch", default="amd64", choices=["amd64", "arm64"],
                    help="the architecture the images must be published for (same rules, same ranking)")
    a = ap.parse_args()
    global ARCH
    ARCH = a.arch
    os.makedirs(a.out, exist_ok=True)
    today = datetime.date.today().isoformat()
    rows, sources = [], {"date": today}

    def cached(name, key):
        """Rows of a set finished earlier today with the same parameters (or None)."""
        path = os.path.join(a.out, f".partial-{today}-{a.arch}-{name}.json")
        try:
            with open(path) as f:
                c = json.load(f)
            if c.get("key") == key:
                print(f"  (already done earlier today: {len(c['rows'])} images, reused)", file=sys.stderr)
                sources.update(c.get("sources", {}))
                return [tuple(r) for r in c["rows"]]
        except (FileNotFoundError, ValueError):
            pass
        return None

    def keep(name, key, set_rows, set_sources):
        with open(os.path.join(a.out, f".partial-{today}-{a.arch}-{name}.json"), "w") as f:
            json.dump({"key": key, "rows": set_rows, "sources": set_sources}, f)

    print("A  BioContainers (quay.io), most used", file=sys.stderr)
    done = cached("biocontainers", a.n_biocontainers)
    if done is not None:
        rows += done
        measure = sources.get("biocontainers_measure", "?")
    else:
        rows += set_a(a, sources)
        measure = sources["biocontainers_measure"]
        keep("biocontainers", a.n_biocontainers, [r for r in rows if r[0] == "biocontainers"],
             {k: v for k, v in sources.items() if k.startswith("biocontainers")})

    print("B  StaPH-B (Docker Hub)", file=sys.stderr)
    hub_login()
    done = cached("staphb", 0)
    if done is not None:
        rows += done
    else:
        rows += set_b(sources)
        keep("staphb", 0, [r for r in rows if r[0] == "staphb"], {"staphb_all": sources["staphb_all"]})

    print('C  Docker Hub search "bioinformatics", by pulls', file=sys.stderr)
    done = cached("dockerhub", a.top_dockerhub)
    if done is not None:
        rows += done
    else:
        rows += set_c(a, sources)
        keep("dockerhub", a.top_dockerhub, [r for r in rows if r[0] == "dockerhub"],
             {"dockerhub_search": sources["dockerhub_search"]})
    write(a, today, rows, sources, measure)


def set_a(a, sources):
    rows = []
    repos = quay_repositories("biocontainers")
    tools = {n: p for n, p in repos.items() if not n.startswith("mulled-v2-")}
    if sum(1 for p in tools.values() if p) >= len(tools) // 2:
        use, measure = tools, "quay.io popularity"
    else:  # quay.io gives no popularity: downloads of the matching Bioconda package
        print("  quay.io gives no popularity: using Bioconda downloads (anaconda.org)", file=sys.stderr)
        use, measure = {}, "Bioconda downloads (anaconda.org)"
        for i, n in enumerate(sorted(tools), 1):
            use[n] = bioconda_downloads(n)
            print(f"\r  downloads {i}/{len(tools)}", end="", file=sys.stderr)
        print(file=sys.stderr)
    sources["biocontainers_measure"] = measure
    sources["biocontainers_use"] = use
    taken, examined = 0, 0
    for name in sorted(use, key=lambda n: (-(use[n] or 0), n)):
        if taken >= a.n_biocontainers:
            break
        examined += 1
        t = quay_latest("biocontainers", name)
        if t:
            rows.append(("biocontainers", ref(f"quay.io/biocontainers/{name}", t["tag"], t["digest"]),
                         f"quay.io/biocontainers/{name}", t["tag"], t["updated"], str(use[name] or 0)))
            taken += 1
        print(f"\r  {taken}/{a.n_biocontainers} (looked at {examined} of the ranking)", end="", file=sys.stderr)
    print(file=sys.stderr)
    sources["biocontainers_examined"] = examined
    sources["biocontainers_architectures"] = dict(ARCH_SEEN)   # every tool looked at
    return rows


def set_b(sources):
    rows = []
    pulls = hub_org_repositories("staphb")
    sources["staphb_all"] = pulls
    staphb = sorted(pulls, key=lambda n: (-pulls[n], n))   # all of them, most pulled first
    for i, name in enumerate(staphb, 1):
        t = hub_latest(f"staphb/{name}")
        if t:
            rows.append(("staphb", ref(f"staphb/{name}", t["tag"], t["digest"]), f"staphb/{name}",
                         t["tag"], t["updated"], str(pulls[name])))
        print(f"\r  {i}/{len(staphb)}", end="", file=sys.stderr)
    print(file=sys.stderr)
    return rows


def set_c(a, sources):
    rows = []
    found = hub_search("bioinformatics", a.search_pages)
    sources["dockerhub_search"] = found
    ranked = sorted(((p, r) for r, p in found.items()
                     if not r.startswith(("staphb/", "biocontainers/"))), reverse=True)
    taken, examined = 0, 0
    for pulls, repo in ranked:
        if taken >= a.top_dockerhub:
            break
        examined += 1
        t = hub_latest(repo)
        if t:
            rows.append(("dockerhub", ref(repo, t["tag"], t["digest"]), repo, t["tag"], t["updated"], str(pulls)))
            taken += 1
        print(f"\r  {taken}/{a.top_dockerhub} (looked at {examined} of the ranking)", end="", file=sys.stderr)
    print(file=sys.stderr)
    sources["dockerhub_examined"] = examined
    return rows


def interleave(rows):
    """The three sets mixed in proportion (each still in its own order of use), so
    that a run stopped early has tested the most used images of every set."""
    sets = {}
    for r in rows:
        sets.setdefault(r[0], []).append(r)
    keyed = [((i + 0.5) / len(lst), k, r) for k, lst in sets.items() for i, r in enumerate(lst)]
    return [r for _, _, r in sorted(keyed)]


def write(a, today, rows, sources, measure):
    rows = interleave(rows)
    sources["arch"] = a.arch
    out = os.path.join(a.out, f"bigtest-{today}-{a.arch}.tsv")
    with open(out, "w") as f:
        f.write(f"# Large-scale test list ({a.arch}), made {today} by scripts/bigtest/make-lists.py: the "
                f"{a.n_biocontainers} most used BioContainers (by {measure}), all StaPH-B, the "
                f"{a.top_dockerhub} most pulled Docker Hub repositories for 'bioinformatics'\n")
        f.write("#set\timage\trepository\ttag\tupdated\tuse\n")
        for r in rows:
            f.write("\t".join(r) + "\n")
    with open(os.path.join(a.out, f"bigtest-{today}-{a.arch}-sources.json"), "w") as f:
        json.dump(sources, f, indent=1)
    counts = {s: sum(1 for r in rows if r[0] == s) for s in ("biocontainers", "staphb", "dockerhub")}
    print(f"{out}: {len(rows)} images {counts}")


if __name__ == "__main__":
    main()
