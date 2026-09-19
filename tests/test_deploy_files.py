"""Static checks of the container files (SPEC §10.1): the Dockerfile, .dockerignore and the
deploy/ Compose examples, plus the README's server-mode documentation (issue #10).

The running image is checked by ``tools/container_smoke.sh`` in the CI ``container`` job; these
tests hold what a running container cannot show: the build context, the base pin, the Compose
settings and the proxy trust.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from pathlib import Path

import pytest
import yaml

from gowui.server_mode import ServerConfig

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
PASSWORD_COMPOSE = ROOT / "deploy" / "compose.password.yaml"
SSO_COMPOSE = ROOT / "deploy" / "compose.sso.yaml"
COMPOSE_FILES = [PASSWORD_COMPOSE, SSO_COMPOSE]

#: The only paths the build context holds and the builder copies (§10.1 "Build context").
CONTEXT_PATHS = {"pyproject.toml", "README.md", "gowui"}
SECRET_LIKE = re.compile(r"PASSW|SECRET|TOKEN|KEY|CREDENTIAL|PRIVATE", re.IGNORECASE)


def spec_variables() -> set[str]:
    """The variable names of the §10 table, read from SPEC.md."""
    text = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    section = text[text.index("## 10. Configuration"):text.index("### 10.1 Container")]
    names = set(re.findall(r"^\| `(GOWUI_[A-Z_]+)` \|", section, flags=re.MULTILINE))
    assert len(names) == 9, names
    return names


def instructions() -> list[tuple[str, str]]:
    """The Dockerfile's instructions as (KEYWORD, arguments), continuation lines joined."""
    joined = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    result = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        result.append((keyword.upper(), rest.strip()))
    return result


def compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def environment(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {key: "" if value is None else str(value) for key, value in env.items()}


# --- Compose examples (AC3) --------------------------------------------------------------------

@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_compose_environment_is_within_section_10_and_valid(path):
    env = environment(compose(path)["services"]["gowui"])
    assert env, "the example sets its configuration"
    assert set(env) <= spec_variables()
    ServerConfig.from_env(env)  # raises ConfigError on anything §7.9 refuses


def test_password_compose_uses_passwords_on_loopback():
    service = compose(PASSWORD_COMPOSE)["services"]["gowui"]
    assert environment(service)["GOWUI_AUTH"] == "local"
    assert all(str(port).startswith("127.0.0.1:") for port in service["ports"])


def test_sso_compose_uses_the_header_and_publishes_no_gowui_port():
    service = compose(SSO_COMPOSE)["services"]["gowui"]
    assert environment(service)["GOWUI_AUTH"] == "header"
    assert "ports" not in service


def _labels(service: dict) -> dict[str, str]:
    labels = service.get("labels") or {}
    if isinstance(labels, list):
        labels = dict(item.split("=", 1) for item in labels)
    return {key: str(value) for key, value in labels.items()}


def test_sso_header_strip_middleware_comes_first():
    service = compose(SSO_COMPOSE)["services"]["gowui"]
    header = environment(service).get("GOWUI_AUTH_HEADER", "X-authentik-username")
    labels = _labels(service)
    chains = [value for key, value in labels.items()
              if re.fullmatch(r"traefik\.http\.routers\.[^.]+\.middlewares", key)]
    assert len(chains) == 1, chains
    chain = [name.strip() for name in chains[0].split(",")]
    assert len(chain) >= 2, "the strip middleware precedes forward auth"
    first = chain[0].split("@", 1)[0]
    blanked = {key.rsplit(".", 1)[1].lower(): value for key, value in labels.items()
               if key.startswith(f"traefik.http.middlewares.{first}.headers.customrequestheaders.")}
    assert blanked == {header.lower(): ""}
    later = [name.split("@", 1)[0] for name in chain[1:]]
    assert any(f"traefik.http.middlewares.{name}.forwardauth.address" in labels for name in later)


def test_sso_trusts_only_traefik_inside_the_edge_network():
    document = compose(SSO_COMPOSE)
    proxies = environment(document["services"]["gowui"])["GOWUI_TRUSTED_PROXIES"]
    network = ipaddress.ip_network(proxies, strict=True)
    assert network.prefixlen == network.max_prefixlen == 32

    edge = document["networks"]["gowui-edge"]
    subnets = [ipaddress.ip_network(entry["subnet"]) for entry in edge["ipam"]["config"]]
    assert len(subnets) == 1 and subnets[0].prefixlen >= 24, "a small fixed subnet"
    assert network.subnet_of(subnets[0])

    traefik = document["services"]["traefik"]
    assert traefik["networks"]["gowui-edge"]["ipv4_address"] == str(network.network_address)
    joined = [name for name, service in document["services"].items()
              if "gowui-edge" in (service.get("networks") or {})]
    assert sorted(joined) == ["gowui", "traefik"]
    assert _labels(document["services"]["gowui"])["traefik.docker.network"] == edge["name"]


# --- README (AC4) ------------------------------------------------------------------------------

def test_readme_documents_modes_variables_and_accounts():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in sorted(spec_variables()):
        assert f"`{name}`" in text, name
    for phrase in ("gowui local", "gowui serve", "gowui user add", "gowui user passwd",
                   "gowui user remove", "gowui user list", "docker exec"):
        assert phrase in text, phrase


# --- Build context and Dockerfile --------------------------------------------------------------

def test_dockerignore_is_an_allowlist():
    lines = [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert lines[0] == "*"
    included = {line[1:].rstrip("/") for line in lines if line.startswith("!")}
    assert included == CONTEXT_PATHS
    last_include = max(i for i, line in enumerate(lines) if line.startswith("!"))
    assert "**/__pycache__" in lines[last_include + 1:], "re-excluded after the re-includes"


def test_builder_copies_exactly_the_allowlisted_paths():
    sources = set()
    for keyword, rest in instructions():
        if keyword not in ("COPY", "ADD"):
            continue
        assert keyword == "COPY", "no ADD"
        words = shlex.split(rest)
        if any(word.startswith("--from=") for word in words):
            continue
        words = [word for word in words if not word.startswith("--")]
        sources.update(word.rstrip("/").removeprefix("./") for word in words[:-1])
    assert sources == CONTEXT_PATHS


def test_every_from_is_pinned_by_digest():
    froms = [rest for keyword, rest in instructions() if keyword == "FROM"]
    assert froms
    for rest in froms:
        image = [word for word in rest.split() if not word.startswith("--")][0]
        assert re.fullmatch(r"python:3\.13-slim@sha256:[0-9a-f]{64}", image), image


def test_no_secret_or_proxy_setting_in_env_or_arg():
    names = []
    for keyword, rest in instructions():
        if keyword == "ENV":
            words = shlex.split(rest)
            if words and "=" not in words[0]:  # legacy `ENV NAME value`
                names.append(words[0])
            else:
                names.extend(word.split("=", 1)[0] for word in words)
        elif keyword == "ARG":
            names.append(rest.split("=", 1)[0].strip())
    assert "GOWUI_DB" in names
    for name in names:
        assert not SECRET_LIKE.search(name), name
        assert name != "FORWARDED_ALLOW_IPS" and not name.startswith("UVICORN_"), name
        assert not name.startswith("GOWUI_") or name == "GOWUI_DB", name
