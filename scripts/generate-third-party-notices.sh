#!/usr/bin/env bash
#
# generate-third-party-notices.sh - generate the licensing artifacts from the declared sets.
#
# Usage:
#   scripts/generate-third-party-notices.sh [--core] [--plugin <name>] [--check]
#
#   --core           regenerate THIRD-PARTY-NOTICES.md (the default)
#   --plugin <name>  regenerate plugins/<name>/static/THIRD-PARTY-LICENSES for a plugin whose
#                    committed browser payload embeds third-party code: its own license from
#                    plugin.toml, its production npm dependencies and the license/copyright
#                    notice blocks found in the bundles under plugins/<name>/static/
#   --check          build the file into the scratch directory instead of the repository, compare
#                    it byte for byte with the committed copy and exit non-zero with a unified
#                    diff on drift. It writes nothing into the repository; the only file it
#                    creates is the scratch copy below.
#
# Inputs - these and nothing else:
#   * the declared sets: backend/requirements.txt and the `dependencies` of frontend/package.json
#     (plugin mode: the `dependencies` of plugins/<name>/frontend/package.json)
#   * the license each package declares about itself: the installed distribution metadata of this
#     repository's virtualenv (.venv) and <frontend>/node_modules/<package>/package.json
#   * the `image:` references of docker-compose.yml, for the container-image table
#   * plugin mode only: plugins/<name>/plugin.toml and the committed bundles under
#     plugins/<name>/static/
#
# The generator reads no registry and contacts no network, so it runs offline and two runs on
# unchanged inputs are byte-identical - which is what --check relies on. It fails closed rather
# than guessing: a declared package that is not installed, an installed version that contradicts
# an exact pin, a package without declared license metadata, a missing node_modules tree, a
# Compose image without a recorded license, a bundle with notice markers but no recorded marker
# baseline, and a bundle that dropped below its baseline all abort the run.
#
# Scratch space: ${PUBLISH_WORK_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/indramesh-publish}/notices
#
# Exit codes: 0 = the file was written, or is up to date in --check mode; 1 = refused (see above).
#
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly WORK_DIR="${PUBLISH_WORK_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/indramesh-publish}/notices"
readonly VENV_PY="$REPO_ROOT/.venv/bin/python"
readonly INSTALL_HINT='python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt'

MODE='core'
PLUGIN=''
MODE_CHECK=0

die() {
  printf 'ABORT: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/generate-third-party-notices.sh [--core] [--plugin <name>] [--check]

  --core           regenerate THIRD-PARTY-NOTICES.md (default)
  --plugin <name>  regenerate plugins/<name>/static/THIRD-PARTY-LICENSES
  --check          compare the committed file with the generated content; write nothing, print
                   a unified diff and exit non-zero when they differ
                   (the plugin mode also re-checks the bundle's license-marker baseline)

Environment:
  PUBLISH_WORK_DIR   scratch root (default: ${XDG_CACHE_HOME:-$HOME/.cache}/indramesh-publish)
                     - only its notices/ subdirectory is used, never the repository
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --core) MODE='core' ;;
    --plugin)
      [ $# -ge 2 ] || { printf 'ABORT: --plugin needs a plugin name\n' >&2; usage; exit 1; }
      MODE='plugin'; PLUGIN="$2"; shift ;;
    --check) MODE_CHECK=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ABORT: unknown argument: %s\n' "$1" >&2; usage; exit 1 ;;
  esac
  shift
done

if [ "$MODE" = 'plugin' ]; then
  case "$PLUGIN" in
    ''|.|..) die "unsuitable plugin name: '$PLUGIN'" ;;
    *[!A-Za-z0-9._-]*) die "unsuitable plugin name: '$PLUGIN'" ;;
  esac
  readonly TARGET_REL="plugins/$PLUGIN/static/THIRD-PARTY-LICENSES"
  readonly SCRATCH="$WORK_DIR/$PLUGIN-third-party-licenses.md"
else
  readonly TARGET_REL='THIRD-PARTY-NOTICES.md'
  readonly SCRATCH="$WORK_DIR/third-party-notices.md"
fi
readonly TARGET="$REPO_ROOT/$TARGET_REL"

[ -x "$VENV_PY" ] || die "no interpreter at ${VENV_PY#"$REPO_ROOT"/} - create the virtualenv first: $INSTALL_HINT"
"$VENV_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "the virtualenv interpreter is older than Python 3.11, which the generator needs to read plugin.toml"

mkdir -p "$WORK_DIR"

"$VENV_PY" - "$REPO_ROOT" "$MODE" "$PLUGIN" > "$SCRATCH" <<'PYTHON'
"""Render a licensing artifact from the declared dependency sets.

Reads the declared sets, resolves each package's license from the metadata that ships with the
package in this checkout (no registry, no network) and prints the complete file to stdout.
Aborts - with a message naming the package or the bundle - instead of emitting an incomplete
claim.
"""

import importlib.metadata
import json
import pathlib
import re
import sys
import tomllib

REPO = pathlib.Path(sys.argv[1]).resolve()
MODE = sys.argv[2]
PLUGIN = sys.argv[3]


def abort(message):
    sys.stderr.write("ABORT: %s\n" % message)
    raise SystemExit(1)


def relative(path):
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------------------
# declared sets
# --------------------------------------------------------------------------------------

# `name`, optional `[extras]`, then a version specifier: `==pin` or a range such as `>=1.2.0`.
REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[(?P<extras>[^\]]*)\])?"
    r"\s*(?P<spec>[<>=!~][^\s;#]*)?\s*$"
)


def declared_requirements(path):
    """Return [(declared name, specifier, line number)] for the non-comment lines of a file."""
    if not path.is_file():
        abort("missing dependency declaration: %s" % relative(path))
    declared = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        match = REQUIREMENT_RE.match(line)
        if not match or not match.group("spec"):
            abort(
                "%s:%d: cannot parse the requirement %r - expected "
                "`name==version` or `name<op>version`"
                % (relative(path), number, line)
            )
        declared.append((match.group("name"), match.group("spec"), number))
    if not declared:
        abort("%s declares no dependency" % relative(path))
    return declared


# --------------------------------------------------------------------------------------
# licenses, read from the packages' own installed metadata
# --------------------------------------------------------------------------------------

# OSI classifier labels and legacy `License:` labels mapped to the SPDX identifier they name. A
# label that is not listed here is reported verbatim - the generator never invents a license name.
LICENSE_LABELS = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
}


def normalize_license(label):
    label = label.strip()
    if label.startswith("(") and label.endswith(")"):
        label = label[1:-1].strip()
    return LICENSE_LABELS.get(label, label)


def python_license(dist, where):
    metadata = dist.metadata
    expression = (metadata.get("License-Expression") or "").strip()
    if expression:
        return normalize_license(expression)
    label = (metadata.get("License") or "").strip()
    # A `License:` field that carries a whole license text is not a license name; skip it.
    if label and "\n" not in label and len(label) <= 120:
        return normalize_license(label)
    for classifier in metadata.get_all("Classifier") or []:
        if classifier.startswith("License :: OSI Approved ::"):
            return normalize_license(classifier.rsplit(" :: ", 1)[-1])
    abort(
        "%s: the package declares no license in its distribution metadata - License-Expression, "
        "License and the OSI classifiers are all empty" % where
    )


def python_rows(requirements_path):
    """Return [(name, version cell, license)] for the declared runtime set."""
    rows = []
    for name, spec, number in declared_requirements(requirements_path):
        where = "%s:%d (%s)" % (relative(requirements_path), number, name)
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            abort(
                "the declared package %r is not installed in the virtualenv the generator runs "
                "in - install the declared set first" % where
            )
        installed = dist.version
        if spec.startswith("=="):
            pinned = spec[2:]
            if installed != pinned:
                abort(
                    "%s is installed as %s but declared as %s - the virtualenv does not match the "
                    "declared set, so the notices would claim the wrong version" % (where, installed, spec)
                )
            version_cell = installed
        else:
            # A range: report what is installed and keep the declared constraint visible.
            version_cell = "%s (declared: %s)" % (installed, spec)
        display = (dist.metadata.get("Name") or name).strip()
        rows.append((display, version_cell, python_license(dist, name)))
    rows.sort(key=lambda row: row[0].lower())
    return rows


def npm_rows(manifest_path, install_hint):
    """Return [(name, version cell, license)] for the installed production dependencies."""
    if not manifest_path.is_file():
        abort("missing dependency declaration: %s" % relative(manifest_path))
    declared = json.loads(manifest_path.read_text(encoding="utf-8")).get("dependencies") or {}
    if not declared:
        abort("%s declares no production dependency" % relative(manifest_path))
    modules_dir = manifest_path.parent / "node_modules"
    if not modules_dir.is_dir():
        abort("the installed npm tree %s is missing - run: %s" % (relative(modules_dir), install_hint))
    rows = []
    for name, spec in sorted(declared.items(), key=lambda item: item[0].lower()):
        package_json = modules_dir / name / "package.json"
        if not package_json.is_file():
            abort("the declared package %r is not installed in %s - run: %s" % (name, relative(modules_dir), install_hint))
        data = json.loads(package_json.read_text(encoding="utf-8"))
        label = (data.get("license") or "").strip()
        if not label and isinstance(data.get("licenses"), list):
            label = " OR ".join(
                str(entry.get("type", "")).strip() for entry in data["licenses"] if entry.get("type")
            )
        if not label:
            abort("the installed package %r declares no license in %s" % (name, relative(package_json)))
        version = str(data.get("version") or "").strip()
        if not version:
            abort("%s declares no version" % relative(package_json))
        exact = str(spec).lstrip("^~=")
        version_cell = version if exact == version else "%s (declared: %s)" % (version, spec)
        rows.append((name, version_cell, normalize_license(label)))
    return rows


# --------------------------------------------------------------------------------------
# container images, read from docker-compose.yml
# --------------------------------------------------------------------------------------

IMAGE_LINE_RE = re.compile(r"^[ \t]+image:[ \t]*([^ \t#]+)[ \t]*$", re.MULTILINE)

# License of every image repository docker-compose.yml references, keyed by the repository part of
# the image reference (no tag, no digest). Recorded when the image entered the Compose stack:
# postgres - the PostgreSQL project's own license (postgresql.org/about/licence);
# valkey/valkey, grafana/loki, grafana/alloy, grafana/grafana - the project's LICENSE file at the
#   tag the Compose file names; nginx - the LICENSE file at the release tag (two-clause BSD);
# opensearchproject/opensearch - the OpenSearch project's Apache-2.0 LICENSE at the tag the Compose
#   service's wrapper image pins (and the image's own org.opencontainers.image.licenses label);
# ghcr.io/tecnativa/docker-socket-proxy - the image's own license label and the upstream
#   repository's license metadata (Apache-2.0).
IMAGE_LICENSES = {
    "postgres": "PostgreSQL License (permissive, similar to MIT/BSD)",
    "valkey/valkey": "BSD-3-Clause",
    "opensearchproject/opensearch": "Apache-2.0",
    "nginx": "BSD-2-Clause",
    "grafana/loki": "AGPL-3.0",
    "grafana/alloy": "Apache-2.0",
    "ghcr.io/tecnativa/docker-socket-proxy": "Apache-2.0",
    "grafana/grafana": "AGPL-3.0",
}

# First-party images: `docker-compose.yml` names them with `image:` on a service that also declares
# `build:`, so they are built from THIS repository's own sources under its own license and are not
# third-party components — a third-party notices table must not list them. Every OTHER image
# reference must be licensed in IMAGE_LICENSES above; an unrecorded one aborts the run, so a new
# Compose image can never reach the published tree with a guessed license.
# The tuple is a membership test applied per `image:` line, so a repository listed before its
# Compose service exists is inert. The three stateful wrappers are all recorded as soon as the
# first of them lands, which keeps the generated file stable for the rest of the phase instead of
# being regenerated once per wrapper; the upstream components the Postgres and OpenSearch wrappers
# carry are named in OWN_IMAGES_SECTION below, because once a service builds its wrapper the
# upstream repository no longer reaches the table read from `docker-compose.yml` (the Valkey
# wrapper's row follows with the plan that makes its service build the wrapper).
LOCALLY_BUILT_IMAGE_REPOSITORIES = (
    "indramesh-backend",
    "indramesh-stateful-postgres",
    "indramesh-stateful-opensearch",
    "indramesh-stateful-valkey",
)


def image_rows(compose_path):
    """Return [(image reference, license)] for the images docker-compose.yml references."""
    if not compose_path.is_file():
        abort("missing %s - the container-image table has no input" % relative(compose_path))
    rows = []
    for reference in IMAGE_LINE_RE.findall(compose_path.read_text(encoding="utf-8")):
        repository = reference.split("@", 1)[0].split(":", 1)[0]
        if repository in LOCALLY_BUILT_IMAGE_REPOSITORIES:
            continue
        label = IMAGE_LICENSES.get(repository)
        if label is None:
            abort(
                "%s references the image %s and no license is recorded for the repository %r - "
                "verify the license and add it to IMAGE_LICENSES before regenerating"
                % (relative(compose_path), reference, repository)
            )
        rows.append((reference, label))
    if not rows:
        abort("%s references no image - the container-image table would be empty" % relative(compose_path))
    return rows


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------

HEADER = """# Third-Party Notices

IndraMesh Core is licensed under the [GNU Affero General Public License v3.0](LICENSE). It is built
on top of the open-source packages listed below; their full license texts ship with the packages
themselves (the backend virtualenv's `site-packages/*/LICENSE*`, `frontend/node_modules/*/LICENSE*`)
and with the container images, which are pulled from their own registries at deploy time.

This list covers the direct application dependencies declared in `backend/requirements.txt` and in
the `dependencies` section of `frontend/package.json`, plus the third-party container images that
`docker-compose.yml` references. It does not inventory transitive dependencies or operating-system
packages inside container images.

Third-party code also travels inside committed build outputs:

- `plugins/graphql-studio/static/graphql-studio-heavy.js` - the graphql-studio plugin's heavy editor
  payload: a self-contained browser bundle of Monaco, GraphiQL and graphql-js under MIT. The notice
  blocks it carries are reproduced in `plugins/graphql-studio/static/THIRD-PARTY-LICENSES`, which
  ships next to it in the plugin's repository.
- the per-plugin registration bundles under `plugins/*/static/*.js` - these embed no third-party
  license or copyright marker, because the host application supplies React and the other shared
  runtime modules; beyond each plugin's own `LICENSE` they need no notices file. The rule is the
  marker, not the plugin name: a bundle that starts carrying notices fails the generator's check.

The application dependencies are licensed under OSI-approved permissive licenses (MIT, BSD,
Apache-2.0, ISC) or LGPL, and are compatible with AGPL-3.0 for the way they are used here
(dynamically loaded or imported at runtime, not statically linked or modified).

This file is generated - do not edit it by hand. Regenerate it with
`scripts/generate-third-party-notices.sh` and verify it with
`scripts/generate-third-party-notices.sh --check`, which fails as soon as a declared dependency set
and this list disagree.
"""

BACKEND_INTRO = """## Backend (Python)

The direct runtime dependencies declared in `backend/requirements.txt`, with the version resolved
in this checkout and the license each package declares in its own distribution metadata. A version
followed by `(declared: ...)` is declared as a range rather than an exact pin, so the resolved
version can move without a change to the declared set.
"""

FRONTEND_INTRO = """## Frontend (JavaScript/npm)

The direct production dependencies declared in the `dependencies` section of
`frontend/package.json`, resolved in this checkout; the same `(declared: ...)` marker as above.
"""

INFRA_INTRO = """## Infrastructure (Docker images, not distributed with this repository)

These run as separate containers via `docker-compose.yml` and are pulled from their own registries
at deploy time; no code from them is redistributed from this repository, so no license obligation
of theirs attaches to it from their use. The table is read from the `image:` references of
`docker-compose.yml`, and the generator refuses an image it has no recorded license for - a new
Compose image cannot reach the published tree with a guessed license.
"""

OWN_IMAGES_SECTION = """## Own stateful images from this repository

`docker-compose.yml` and the Kubernetes installer build these images from the repository's own
sources (`deploy/k8s-images/stateful/`), so they are first-party and do not appear in the table
above. Each is a thin wrapper around an upstream image; the upstream components it carries are
named here, because once a service builds its wrapper the upstream `image:` reference no longer
reaches the table the generator reads.

`indramesh-stateful-postgres` — the upstream components compiled into it inside the image build,
from pinned upstream tags (they are not redistributed as separate packages):

| Component | Version (pinned) | License |
|---|---|---|
| PostgreSQL server | 18.4 (`postgres:18.4-alpine` base image) | PostgreSQL License (permissive, similar to MIT/BSD) |
| Apache AGE | 1.8.0 (official Apache source release for PostgreSQL 18; git tag `PG18/v1.8.0-rc0`) | Apache-2.0 |
| pgvector | 0.8.7 (git tag `v0.8.7`) | PostgreSQL License |

`indramesh-stateful-opensearch` — the wrapper only makes the security plugin's tooling
group-readable for an arbitrary-UID Kubernetes pod; the search engine itself is the upstream
image:

| Component | Version (pinned) | License |
|---|---|---|
| OpenSearch | 3.9.0 (`opensearchproject/opensearch:3.9.0` base image) | Apache-2.0 |
"""

K8S_SECTION = """## Kubernetes Deployment Infrastructure

The Kubernetes installer deploys or configures the following external components. They remain
separate services and are not incorporated into the IndraMesh Core application code.

| Component | License | Use in IndraMesh deployment |
|---|---|---|
| k3s | Apache-2.0 | Kubernetes distribution used by the installer |
| Helm | Apache-2.0 | Chart installation and release management |
| cert-manager | Apache-2.0 | TLS certificate management |
| kube-prometheus-stack | Apache-2.0 | Monitoring chart providing Prometheus-stack components |
| Prometheus, Prometheus Operator, kube-state-metrics, node-exporter | Apache-2.0 | Metrics collection and Kubernetes workload monitoring |
| Grafana | AGPL-3.0 | Monitoring dashboards; deployed as a separate service through kube-prometheus-stack |
| Traefik | MIT | k3s ingress controller used by the IngressRoute resources |
| Headlamp | Apache-2.0 | Optional Kubernetes management UI |

Grafana and Loki are AGPL-3.0: a deployment that modifies them and offers them to users over a
network must comply with that license. Their license does not change the license of the separate
IndraMesh Core application.
"""

TRADEMARK_SECTION = """## Trademark Notice

"Jira" and "Jira Assets" are trademarks of Atlassian Pty Ltd. IndraMesh Core is an independent
project that integrates with Jira Assets via its public API; it is **not affiliated with, endorsed
by, or sponsored by** Atlassian.
"""


PLUGIN_HEADER = """# Third-Party Licenses - {plugin}

The `{plugin}` plugin is licensed under {license} - see [`LICENSE`](LICENSE). This file
covers the third-party code that the plugin's committed browser payload embeds:

{bundles}

Regenerate it in the Core repository (the plugin's browser payload is built from that checkout)
with:

    {command}

The result is reproducible from the committed bundle alone; do not edit this file by hand. The
generator refuses a payload that drops below its recorded license-marker baseline, so a bundler
change that strips legal comments cannot silently remove these notices.
"""

PLUGIN_NPM_INTRO = """## Bundled production packages

The direct production dependencies declared in this plugin's `frontend/package.json`, resolved in
the build checkout; the same `(declared: ...)` marker as above.
"""

PLUGIN_BLOCK_INTRO = """## Bundled notice blocks

Every block comment in the payload that carries `@license` or `Copyright`, deduplicated and in
first-appearance order. Each block is reproduced verbatim from the committed bundle and is never
truncated; Monaco emits its notice inside a string literal, so its line breaks appear as literal
`\\n` escapes.
"""


def table(rows):
    lines = ["| Package | Version | License |", "|---|---|---|"]
    lines.extend("| %s | %s | %s |" % row for row in rows)
    return "\n".join(lines) + "\n"


def image_table(rows):
    lines = ["| Image | License |", "|---|---|"]
    lines.extend("| %s | %s |" % row for row in rows)
    return "\n".join(lines) + "\n"


def render_core():
    backend = table(python_rows(REPO / "backend" / "requirements.txt"))
    frontend = table(npm_rows(REPO / "frontend" / "package.json", "npm ci --prefix frontend"))
    images = image_table(image_rows(REPO / "docker-compose.yml"))
    return "".join(
        [
            HEADER,
            "\n",
            BACKEND_INTRO,
            "\n",
            backend,
            "\n",
            FRONTEND_INTRO,
            "\n",
            frontend,
            "\n",
            INFRA_INTRO,
            "\n",
            images,
            "\n",
            OWN_IMAGES_SECTION,
            "\n",
            K8S_SECTION,
            "\n",
            TRADEMARK_SECTION,
        ]
    )


# --------------------------------------------------------------------------------------
# plugin payloads: the notice blocks a committed bundle carries
# --------------------------------------------------------------------------------------

BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
MARKER_RE = re.compile(r"@license|Copyright")

# Recorded license/copyright marker-line baselines, keyed by the bundle's repository-relative
# path. Measured when the bundle entered the repository (graphql-studio-heavy.js: 97 marker
# lines, 3 distinct notice blocks, all of them Monaco/MIT). A rebuild that strips legal comments
# drops below its baseline and aborts instead of silently losing notices; a bundle that carries
# markers without a recorded baseline also aborts, so no payload reaches a mirror unnoticed.
MARKER_BASELINES = {
    "plugins/graphql-studio/static/graphql-studio-heavy.js": 97,
}


def bundle_notices(bundle):
    """Return (marker line count, [(byte offset, block)] deduplicated in first-appearance order)."""
    text = bundle.read_text(encoding="utf-8")
    marker_lines = sum(1 for line in text.split("\n") if MARKER_RE.search(line))
    blocks = []
    seen = set()
    for match in BLOCK_COMMENT_RE.finditer(text):
        block = match.group(0)
        if not MARKER_RE.search(block) or block in seen:
            continue
        seen.add(block)
        blocks.append((len(text[: match.start()].encode("utf-8")), block))
    return marker_lines, blocks


def covered_bundles(plugin_dir):
    """Return [(relative path, marker lines, blocks)] for the plugin's marked bundles."""
    static_dir = plugin_dir / "static"
    if not static_dir.is_dir():
        abort(
            "%s does not exist - the notices file has to sit inside the plugin directory so the "
            "plugin mirror carries it" % relative(static_dir)
        )
    covered = []
    for bundle in sorted(static_dir.glob("*.js")):
        marker_lines, blocks = bundle_notices(bundle)
        if not marker_lines:
            continue
        path = relative(bundle)
        baseline = MARKER_BASELINES.get(path)
        if baseline is None:
            abort(
                "%s carries %d license/copyright marker lines but no recorded baseline - measure "
                "it and record the value in MARKER_BASELINES" % (path, marker_lines)
            )
        if marker_lines < baseline:
            abort(
                "%s has %d license/copyright marker lines, below the recorded baseline of %d - a "
                "build that strips legal comments must not pass unnoticed" % (path, marker_lines, baseline)
            )
        covered.append((path, marker_lines, blocks))
    if not covered:
        abort(
            "no bundle under %s carries a license or copyright marker - this plugin needs no "
            "notices file; the rule is the marker, not the plugin name" % relative(static_dir)
        )
    return covered


def render_plugin():
    plugin_dir = REPO / "plugins" / PLUGIN
    toml_path = plugin_dir / "plugin.toml"
    manifest_path = plugin_dir / "frontend" / "package.json"
    if not plugin_dir.is_dir():
        abort("no plugin directory at %s" % relative(plugin_dir))
    if not manifest_path.is_file():
        abort(
            "%s declares no frontend package - the plugin mode needs the plugin's own "
            "frontend/package.json to read its declared dependencies" % relative(plugin_dir)
        )
    if not toml_path.is_file():
        abort("no plugin manifest at %s - the plugin's own license cannot be read" % relative(toml_path))
    declared_license = (tomllib.loads(toml_path.read_text(encoding="utf-8")).get("license") or "").strip()
    if not declared_license:
        abort("%s declares no license field" % relative(toml_path))

    covered = covered_bundles(plugin_dir)
    command = "scripts/generate-third-party-notices.sh --plugin %s" % PLUGIN
    bundles = "\n".join(
        "- `%s` - %d license/copyright marker lines, %d distinct notice blocks"
        % (
            str(pathlib.Path(path).relative_to(pathlib.Path("plugins") / PLUGIN)),
            marker_lines,
            len(blocks),
        )
        for path, marker_lines, blocks in covered
    )
    header = PLUGIN_HEADER.format(
        plugin=PLUGIN, license=declared_license, bundles=bundles, command=command
    )
    packages = table(npm_rows(manifest_path, "npm ci --prefix plugins/%s/frontend" % PLUGIN))

    parts = [header, "\n", PLUGIN_NPM_INTRO, "\n", packages, "\n", PLUGIN_BLOCK_INTRO]
    for path, marker_lines, blocks in covered:
        parts.append(
            "\n### %s\n\n%d license/copyright marker lines, %d distinct notice blocks, recorded "
            "baseline %d lines.\n"
            % (
                pathlib.Path(path).relative_to(pathlib.Path("plugins") / PLUGIN),
                marker_lines,
                len(blocks),
                MARKER_BASELINES[path],
            )
        )
        for offset, block in blocks:
            parts.append("\n**Byte offset %d**\n\n```text\n%s\n```\n" % (offset, block))
    return "".join(parts)


sys.stdout.write(render_plugin() if MODE == "plugin" else render_core())
PYTHON

if [ "$MODE_CHECK" -eq 1 ]; then
  [ -f "$TARGET" ] || die "--check: $TARGET_REL does not exist - generate it first"
  if cmp -s "$SCRATCH" "$TARGET"; then
    printf 'ok: %s is up to date\n' "$TARGET_REL"
  else
    printf 'FAIL: %s differs from the generated content (committed -> generated):\n' "$TARGET_REL" >&2
    diff -u --label "committed/$TARGET_REL" --label "generated/$TARGET_REL" "$TARGET" "$SCRATCH" || true
    exit 1
  fi
else
  cp -- "$SCRATCH" "$TARGET"
  printf 'wrote %s\n' "$TARGET_REL"
fi
