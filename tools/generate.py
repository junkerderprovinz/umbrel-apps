"""Builds the Umbrel community app store from the templates in unraid-apps.

unraid-apps stays the only place an app's container settings are edited. This
script takes the compose file that unraid-apps generates for each app listed in
apps.json, puts it behind Umbrel's app proxy, moves its folders under the app's
data directory and writes the umbrel-app.yml manifest next to it.

    python tools/generate.py ../unraid-apps          write the app folders
    python tools/generate.py ../unraid-apps --check  fail if they are stale
"""

import argparse
import base64
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STORE = "junkerderprovinz"
RAW = "https://raw.githubusercontent.com/junkerderprovinz/umbrel-apps/main"
AUTHOR = "Junker der Provinz"
FEED_URL = "https://github.com/junkerderprovinz/unraid-apps"

# Umbrel runs its apps as user 1000.
SYSTEM_VALUES = {"PUID": "1000", "PGID": "1000"}

MANIFESTS = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def load_gen_compose(feed):
    spec = importlib.util.spec_from_file_location("gen_compose", feed / ".github" / "gen-compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def github(path):
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return get(f"https://api.github.com/{path}", headers)
    except urllib.error.HTTPError:
        return None


def registry(image):
    """Return the registry host, repository and tag of an image reference."""
    name, _, tag = image.rpartition(":") if ":" in image.split("/")[-1] else (image, "", "latest")
    parts = name.split("/")
    if "." in parts[0] or ":" in parts[0]:
        host, repo = parts[0], "/".join(parts[1:])
    else:
        host, repo = "registry-1.docker.io", name if "/" in name else f"library/{name}"
    return host, repo, tag or "latest"


def registry_token(host, repo):
    headers = {}
    if host == "registry-1.docker.io":
        url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
        # Anonymous lookups share a small hourly limit per address.
        user, token = os.environ.get("DOCKERHUB_USERNAME"), os.environ.get("DOCKERHUB_TOKEN")
        if user and token:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
    else:
        url = f"https://{host}/token?scope=repository:{repo}:pull"
    try:
        return get(url, headers).get("token")
    except (urllib.error.URLError, ValueError):
        return None


def image_facts(image):
    """Return an image's architectures, the version it names and its build date as a version."""
    host, repo, tag = registry(image)
    token = registry_token(host, repo)
    headers = {"Accept": MANIFESTS}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = f"https://{host}/v2/{repo}"
    try:
        manifest = get(f"{base}/manifests/{tag}", headers)
        archs = None
        if "manifests" in manifest:
            platforms = [m.get("platform", {}) for m in manifest["manifests"]]
            archs = sorted({p["architecture"] for p in platforms if p.get("os") == "linux"})
            arch = "amd64" if "amd64" in archs else archs[0]
            pick = next(m for m in manifest["manifests"] if m.get("platform", {}).get("architecture") == arch)
            manifest = get(f"{base}/manifests/{pick['digest']}", headers)
        config = get(f"{base}/blobs/{manifest['config']['digest']}", headers)
    except (urllib.error.URLError, StopIteration, KeyError, IndexError, ValueError) as error:
        # A wrong architecture list fails the store build, so guessing is worse than stopping.
        raise SystemExit(f"{image}: registry lookup failed: {error}") from error
    archs = archs or [config["architecture"]]
    settings = config.get("config") or {}
    version = semver((settings.get("Labels") or {}).get("org.opencontainers.image.version"))
    # Official images such as CouchDB carry their version only in the
    # environment, next to the versions of their base image's tools.
    own = re.sub(r"\W", "", repo.split("/")[-1]).upper() + "_VERSION"
    for entry in settings.get("Env") or []:
        key, _, value = entry.partition("=")
        if not version and key == own:
            version = semver(value)
    return archs, version, semver(config.get("created", "")[:10])


def semver(value):
    # Date-based releases (2026-09-16) read as 2026.9.16.
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", value or "") or re.search(r"(\d{4})-(\d\d)-(\d\d)", value or "")
    if not match:
        return None
    return ".".join(str(int(part or 0)) for part in match.groups())


def release_version(project):
    match = re.match(r"https://github\.com/([^/]+/[^/#?]+)", project or "")
    if not match:
        return None
    release = github(f"repos/{match.group(1)}/releases/latest")
    return semver(release["tag_name"]) if release else None


def card_text(feed, app):
    readme = (feed / "README.md").read_text(encoding="utf-8")
    match = re.search(rf'readme-icons/{re.escape(app)}\.png".*?\*\*[^*]+\*\*<br>\s*\n(.+?)\n', readme, re.S)
    return match.group(1).strip().replace("&amp;", "&") if match else None


def first_paragraph(root):
    overview = (root.findtext("Overview") or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in overview.split("\n")]
    paragraph = "\n".join(lines).strip().split("\n\n")[0]
    return " ".join(paragraph.split())


def unraid_specific(text):
    # The template overviews talk to Unraid users, which reads oddly on ZimaOS.
    return re.search(r"\bUnraid\b|\btemplate\b", text, re.I) is not None


def data_path(volume, overrides):
    """Return a compose volume with its host side moved to Umbrel, and the
    folder under the app's data directory it needs, if any."""
    host, target, *mode = volume.split(":")
    folder = None
    if host.startswith("./appdata"):
        folder = "data" + host[len("./appdata"):]
        host = "${APP_DATA_DIR}/" + folder
    elif host.startswith("./"):
        host = overrides.get("paths", {}).get(target)
        if host is None:
            folder = "data/" + volume.split(":")[0][2:]
            host = "${APP_DATA_DIR}/" + folder
    return ":".join([host, target, *mode]), folder


def credential(key):
    """Return the start value for a login the template leaves to the user.

    Umbrel has no form to fill it in before the first start, but it gives
    every install a password of its own and shows it next to the app.
    """
    if re.search(r"ACCESS_KEY|USER", key):
        return "umbrel"
    if re.search(r"PASSWORD|SECRET|_KEY$", key):
        return "${APP_PASSWORD}"
    return None


def build(feed, gen_compose, app, overrides):
    xml = overrides.get("xml", f"{app}.xml")
    root = ET.parse(feed / app / xml).getroot()
    text, _ = gen_compose.compose(app, xml)
    required = re.findall(r"^\s+(\w+):.*# required$", text, re.M)
    service = yaml.safe_load(text)["services"][app]
    app_id = f"{STORE}-{app}"

    match = re.match(r"(https?)://\[IP\]:\[PORT:(\d+)\](.*)", (root.findtext("WebUI") or "").strip())
    if not match:
        return None
    _scheme, web_target, index = match.groups()
    proxied = str(overrides.get("web_port", web_target))

    service.pop("container_name", None)
    service["restart"] = "on-failure"
    # Every other port moves to one of our own, since the template's (9000,
    # 8888, ...) are taken by apps in Umbrel's store or by each other.
    ports, listens = [], []
    for mapping in service.pop("ports", []):
        target = mapping.split(":")[1]
        bare = target.split("/")[0]
        if bare in (web_target, proxied):
            continue
        host = overrides.get("host_ports", {}).get(bare)
        if host is None:
            raise SystemExit(f"{app}: port {bare} needs a host port in apps.json")
        ports.append(f"{host}:{target}")
        config = root.find(f"Config[@Type='Port'][@Target='{bare}']")
        title = config.get("Name") if config is not None else None
        label = re.sub(r"\s+Port$", "", title or bare)
        listens.append(f"{label} on port {host}")
    if ports:
        service["ports"] = ports

    folders, volumes = [], []
    for volume in service.get("volumes", []):
        if volume.split(":")[1] in overrides.get("drop_paths", []):
            continue
        moved, folder = data_path(volume, overrides)
        volumes.append(moved)
        if folder:
            folders.append(folder)
    if volumes:
        service["volumes"] = volumes
    else:
        service.pop("volumes", None)

    env = service.get("environment") or {}
    for key, value in SYSTEM_VALUES.items():
        if key in env:
            env[key] = value
    env.update(overrides.get("env", {}))
    username = overrides.get("username")
    password = False
    for key in required:
        if env.get(key):
            continue
        value = credential(key)
        if value is None:
            raise SystemExit(f"{app}: {key} needs a value in apps.json, Umbrel cannot ask for it")
        env[key] = value
        if value == "umbrel":
            username = username or value
        else:
            password = True
    password = password or any("${APP_PASSWORD}" in str(v) for v in env.values())
    if env:
        service["environment"] = env

    proxy = {"APP_HOST": f"{app_id}_{app}_1", "APP_PORT": proxied}
    if overrides.get("auth") is False:
        proxy["PROXY_AUTH_ADD"] = "false"
    compose = {"version": "3.7", "services": {"app_proxy": {"environment": proxy}, app: service}}

    archs, image_version, built = image_facts(service["image"])
    if not {"amd64", "arm64"} <= set(archs):
        print(f"{app}: image is built for {', '.join(archs)} only", file=sys.stderr)
    readme = (root.findtext("ReadMe") or "").strip()
    repo = re.match(r"https://github\.com/junkerderprovinz/[^/]+", readme)
    # A wrapper has no repository of its own; its README lives in unraid-apps.
    wrapper = repo is not None and repo.group(0).endswith("/unraid-apps")
    version = (release_version(repo.group(0)) if repo and not wrapper else None) or image_version \
        or release_version(root.findtext("Project")) or built
    name = (root.findtext("Name") or app).strip()
    tagline = overrides.get("tagline") or card_text(feed, app) or name
    description = overrides.get("description") or first_paragraph(root)
    if unraid_specific(description):
        description = tagline
    if listens:
        description += "\n\nBesides the web interface it listens on: " + ", ".join(listens) + "."
    home = (readme if wrapper else repo.group(0)) if repo else (root.findtext("Project") or "").strip()

    manifest = {
        "manifestVersion": 1,
        "id": app_id,
        "category": overrides["category"],
        "name": name,
        "version": version,
        "tagline": tagline,
        "icon": f"{RAW}/{app_id}/icon.png",
        "description": description,
        "developer": overrides.get("developer", AUTHOR),
        "website": home,
        "dependencies": [],
        "repo": home,
        "support": repo.group(0) + "/issues" if repo else home,
        "port": overrides["port"],
        "gallery": [],
        "path": index or "",
        "defaultUsername": username or "",
        "defaultPassword": "",
        "submitter": AUTHOR,
        "submission": "https://github.com/junkerderprovinz/umbrel-apps",
    }
    if password:
        manifest["deterministicPassword"] = True
    if any("storage/downloads" in v for v in volumes):
        manifest["permissions"] = ["STORAGE_DOWNLOADS"]
    return app_id, dump(manifest), dump(compose), folders


def dump(doc):
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=1000)


def readme_cards(feed, apps):
    """Return the README's app section, built from the cards in the feed README.

    Each app keeps the card, description and group (Apps, Images, Wrappers) it
    has in unraid-apps, so the two READMEs cannot drift apart.
    """
    source = (feed / "README.md").read_text(encoding="utf-8")
    groups = {}
    for app in apps:
        start = source.index(f'<img src=".github/readme-icons/{app}.png"')
        end = source.index('<br clear="all">', start) + len('<br clear="all">')
        heading = list(re.finditer(r"^## (\w+)\n\n(\*.+\*)$", source[:start], re.M))[-1]
        # A wrapper's README lives in its folder in unraid-apps.
        card = re.sub(r'href="([\w-]+/README\.md)"', rf'href="{FEED_URL}/blob/main/\1"', source[start:end])
        groups.setdefault(heading.group(1), (heading.group(2), []))[1].append(f'<a id="{app}"></a>\n{card}')
    order = [h for h in ("Apps", "Images", "Wrappers") if h in groups]
    parts = [f"## {h}\n\n{groups[h][0]}\n\n" + "\n\n".join(groups[h][1]) for h in order]
    return "\n\n\n".join(parts)


def readme(current, feed, apps):
    body = readme_cards(feed, apps)
    current = re.sub(
        r"(<!-- apps:start -->\n).*?(<!-- apps:end -->)",
        lambda m: m.group(1) + body + "\n" + m.group(2),
        current,
        flags=re.S,
    )
    return re.sub(r"badge/Apps-\d+-", f"badge/Apps-{len(apps)}-", current)


def main():
    parser = argparse.ArgumentParser(description="Write the Umbrel app store from the Unraid templates.")
    parser.add_argument("feed", type=Path, help="checkout of junkerderprovinz/unraid-apps")
    parser.add_argument("--check", action="store_true", help="report stale files instead of writing")
    args = parser.parse_args()

    gen_compose = load_gen_compose(args.feed)
    apps = json.loads((ROOT / "apps.json").read_text(encoding="utf-8"))
    ports = [p for o in apps.values() for p in [o["port"], *o.get("host_ports", {}).values()]]
    if len(ports) != len(set(ports)):
        raise SystemExit("apps.json gives the same port twice")

    # The icons are copied rather than linked, so the store holds everything
    # it shows and keeps working if unraid-apps moves.
    wanted, listed = {}, []
    for app, overrides in apps.items():
        result = build(args.feed, gen_compose, app, overrides)
        if result is None:
            print(f"{app}: no web UI, skipped", file=sys.stderr)
            continue
        app_id, manifest, compose, folders = result
        listed.append(app)
        wanted[f"{app_id}/umbrel-app.yml"] = manifest.encode("utf-8")
        wanted[f"{app_id}/docker-compose.yml"] = compose.encode("utf-8")
        wanted[f"{app_id}/icon.png"] = (args.feed / app / "icon.png").read_bytes()
        # Umbrel copies these folders into the app's data directory owned by
        # user 1000; left to Docker they would be created as root.
        for folder in folders:
            wanted[f"{app_id}/{folder}/.gitkeep"] = b""
        wanted[f".github/readme-icons/{app}.png"] = (
            args.feed / ".github" / "readme-icons" / f"{app}.png"
        ).read_bytes()
    wanted["README.md"] = readme(
        (ROOT / "README.md").read_text(encoding="utf-8"), args.feed, listed
    ).encode("utf-8")

    stale = []
    for name, content in wanted.items():
        target = ROOT / name
        if not target.exists() or target.read_bytes() != content:
            stale.append(name)
            if not args.check:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    present = [p for d in ROOT.glob(f"{STORE}-*") for p in d.rglob("*") if p.is_file()]
    present += [p for p in (ROOT / ".github" / "readme-icons").glob("*") if p.is_file()]
    removed = [p for p in sorted(p.relative_to(ROOT).as_posix() for p in present) if p not in wanted]
    stale += removed
    if not args.check:
        for leftover in removed:
            (ROOT / leftover).unlink()
        for folder in sorted(ROOT.glob(f"{STORE}-*/**"), reverse=True):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()

    if args.check and stale:
        print("out of date: " + ", ".join(stale), file=sys.stderr)
        print("run: python tools/generate.py <unraid-apps checkout>", file=sys.stderr)
        return 1
    for name in stale:
        print(f"{'removed' if name in removed else 'updated'} {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
