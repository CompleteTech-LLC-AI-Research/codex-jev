"""Validation for the JEV compatibility manifest and integration profiles.

The manifest is the single machine-readable statement of what the integrated
Codex build is made of: the pinned host commit, the immutable revisions of the
supporting components, the ordered patches, the shared interface versions, the
feature switches, and the separate credential and remote-consent controls.

Every rule in this module fails closed with a stable error code so a clean
checkout rejects unsupported combinations explicitly instead of building a
configuration that was never validated.

Only the Python standard library is used: integration scripts must run without
pip or npm installation.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

MANIFEST_VERSION = 1
PROFILE_VERSION = 1

TOP_LEVEL_KEYS = {
    "manifest_version",
    "integration",
    "host",
    "interfaces",
    "patches",
    "components",
    "features",
    "events",
    "credentials",
    "ownership",
}

REQUIRED_TOP_LEVEL_KEYS = TOP_LEVEL_KEYS - {"ownership"}

COMPONENT_KINDS = {"host", "patch", "python", "python-native"}

COMPONENT_REQUIRED_KEYS = {
    "id",
    "kind",
    "repository",
    "revision",
    "license",
    "python_requirement",
    "provides",
    "requires_interfaces",
    "owns",
}

FEATURE_REQUIRED_KEYS = {"default", "components", "requires"}
FEATURE_OPTIONAL_KEYS = {
    "invariant",
    "build_time_only",
    "requires_patches",
    "requires_consent",
    "requires_budget",
}

PATCH_REQUIRED_KEYS = {
    "id",
    "order",
    "component",
    "file",
    "sha256",
    "applies_to_host_base",
    "targets",
    "behavior",
    "disable",
}

PROFILE_KEYS = {"profile_version", "id", "description", "features"}

REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ManifestError(Exception):
    """Raised when a manifest, profile, or file cannot be read at all."""


def repository_root() -> Path:
    """Return the integration host checkout root containing this script."""
    return Path(__file__).resolve().parents[2]


def default_manifest_path() -> Path:
    return repository_root() / "jev" / "compatibility-manifest.json"


def load_json(path, what):
    path = Path(path)
    if not path.is_file():
        raise ManifestError(f"{what} not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestError(f"{what} is not valid JSON: {path}: {error}") from error


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_owned_paths(component):
    return [path for path in component.get("owns", []) if isinstance(path, str)]


def _path_overlaps(left, right):
    left = left.rstrip("/")
    right = right.rstrip("/")
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _component_owns(component, target):
    return any(_path_overlaps(owned, target) for owned in iter_owned_paths(component))


def _feature_defaults(manifest):
    return {
        name: bool(spec.get("default"))
        for name, spec in manifest.get("features", {}).items()
    }


def _effective_features(manifest, profile):
    effective = _feature_defaults(manifest)
    overrides = profile.get("features", {}) if profile else {}
    for name, enabled in overrides.items():
        effective[name] = bool(enabled)
    return effective


def effective_features(manifest, profile=None):
    """Public wrapper: the feature switches a profile actually produces.

    Integration scripts and tests read the effective set instead of the raw
    profile so a profile that omits a default-on feature is still described
    correctly.
    """
    return _effective_features(manifest, profile)


def validate_manifest(manifest, repo_root=None, profile=None):
    """Return every validation error as ``CODE: message`` strings."""
    repo_root = Path(repo_root) if repo_root else repository_root()
    errors = []
    errors += _validate_structure(manifest)
    errors += _validate_host(manifest)
    errors += _validate_interfaces(manifest)
    errors += _validate_components(manifest)
    errors += _validate_ownership(manifest)
    errors += _validate_patches(manifest, repo_root)
    errors += _validate_features(manifest)
    errors += _validate_credentials(manifest)
    errors += _validate_default_profile(manifest)
    if profile is not None:
        errors += _validate_profile(manifest, profile)
    return errors


def _validate_structure(manifest):
    errors = []
    if not isinstance(manifest, dict):
        return ["E_MANIFEST_SCHEMA: manifest must be a JSON object"]
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        errors.append(
            "E_MANIFEST_VERSION: manifest_version must be "
            f"{MANIFEST_VERSION}, found {manifest.get('manifest_version')!r}"
        )
    missing = sorted(REQUIRED_TOP_LEVEL_KEYS - set(manifest))
    if missing:
        errors.append(
            f"E_MANIFEST_SCHEMA: missing top-level keys: {', '.join(missing)}"
        )
    unknown = sorted(set(manifest) - TOP_LEVEL_KEYS)
    if unknown:
        errors.append(
            f"E_MANIFEST_SCHEMA: unsupported top-level keys: {', '.join(unknown)}"
        )
    return errors


def _validate_host(manifest):
    errors = []
    host = manifest.get("host", {})
    if not isinstance(host, dict):
        return ["E_HOST_SCHEMA: host must be a JSON object"]
    pinned = host.get("base_commit")
    if not isinstance(pinned, str) or not REVISION_RE.match(pinned):
        errors.append(
            f"E_HOST_PIN: host.base_commit must be a 40-character lowercase hex commit, found {pinned!r}"
        )
    if not host.get("rust_toolchain"):
        errors.append(
            "E_HOST_SCHEMA: host.rust_toolchain must record the pinned Rust toolchain"
        )
    if not host.get("python_requirement"):
        errors.append(
            "E_HOST_SCHEMA: host.python_requirement must record the required interpreter"
        )
    platforms = host.get("platforms", {})
    if not platforms.get("supported"):
        errors.append(
            "E_HOST_SCHEMA: host.platforms.supported must list supported platforms"
        )
    if platforms.get("reference") not in (platforms.get("supported") or []):
        errors.append(
            "E_HOST_SCHEMA: host.platforms.reference must be one of host.platforms.supported"
        )
    if not host.get("runtime_requirements"):
        errors.append("E_HOST_SCHEMA: host.runtime_requirements must be recorded")
    integration = manifest.get("integration", {})
    if not integration.get("excluded_repositories"):
        errors.append(
            "E_HOST_SCHEMA: integration.excluded_repositories must name excluded repositories"
        )
    return errors


def _validate_interfaces(manifest):
    errors = []
    interfaces = manifest.get("interfaces", {})
    if not isinstance(interfaces, dict) or not interfaces:
        return ["E_INTERFACE_SCHEMA: interfaces must be a non-empty JSON object"]
    for name, version in interfaces.items():
        if not isinstance(version, int) or version < 1:
            errors.append(
                f"E_INTERFACE_VERSION: interface {name} must declare a positive integer version"
            )
    return errors


def _validate_components(manifest):
    errors = []
    components = manifest.get("components")
    if not isinstance(components, list) or not components:
        return ["E_COMPONENT_SCHEMA: components must be a non-empty JSON array"]
    excluded = set(manifest.get("integration", {}).get("excluded_repositories", []))
    seen_ids = {}
    seen_repositories = {}
    interfaces = manifest.get("interfaces", {})
    for component in components:
        if not isinstance(component, dict):
            errors.append("E_COMPONENT_SCHEMA: every component must be a JSON object")
            continue
        component_id = component.get("id", "<missing id>")
        missing = sorted(COMPONENT_REQUIRED_KEYS - set(component))
        if missing:
            errors.append(
                f"E_COMPONENT_SCHEMA: component {component_id} is missing keys: {', '.join(missing)}"
            )
            continue
        if component["kind"] not in COMPONENT_KINDS:
            errors.append(
                f"E_COMPONENT_KIND: component {component_id} has unsupported kind {component['kind']!r}"
            )
        repository = component["repository"]
        if not isinstance(repository, str) or not REPOSITORY_RE.match(repository):
            errors.append(
                f"E_COMPONENT_SCHEMA: component {component_id} has invalid repository {repository!r}"
            )
        elif repository in excluded:
            errors.append(
                f"E_EXCLUDED_COMPONENT: component {component_id} points at excluded repository {repository}"
            )
        revision = component["revision"]
        if not isinstance(revision, str) or not REVISION_RE.match(revision):
            errors.append(
                f"E_COMPONENT_REVISION: component {component_id} must pin an immutable 40-character revision, found {revision!r}"
            )
        if not component.get("owns"):
            errors.append(
                f"E_COMPONENT_SCHEMA: component {component_id} must declare owned paths"
            )
        for interface, version in (component.get("requires_interfaces") or {}).items():
            if interface not in interfaces:
                errors.append(
                    f"E_INTERFACE_VERSION: component {component_id} requires unknown interface {interface}"
                )
            elif interfaces[interface] != version:
                errors.append(
                    f"E_INTERFACE_VERSION: component {component_id} requires {interface} version {version} "
                    f"but the manifest declares {interfaces[interface]}"
                )
        for interface in component.get("provides") or []:
            if interface not in interfaces and interface not in {
                "capture",
                "retrieval",
                "dedup_stage",
                "incident_envelope",
                "plaintext_collab_patch",
            }:
                errors.append(
                    f"E_INTERFACE_VERSION: component {component_id} provides undeclared interface {interface}"
                )
        if component_id in seen_ids:
            errors.append(
                f"E_DUPLICATE_COMPONENT: component id {component_id} is declared more than once"
            )
        seen_ids[component_id] = component
        if isinstance(repository, str) and repository in seen_repositories:
            errors.append(
                f"E_DUPLICATE_COMPONENT: repository {repository} is owned by both "
                f"{seen_repositories[repository]} and {component_id}"
            )
        if isinstance(repository, str):
            seen_repositories[repository] = component_id
    if "codex-jev" not in seen_ids:
        errors.append(
            "E_COMPONENT_SCHEMA: components must declare the codex-jev host component"
        )
    return errors


def _validate_ownership(manifest):
    errors = []
    components = [c for c in manifest.get("components", []) if isinstance(c, dict)]
    by_repository = {}
    for component in components:
        for owned in iter_owned_paths(component):
            peers = by_repository.setdefault(component.get("repository"), [])
            for other_id, other_path in peers:
                if _path_overlaps(owned, other_path):
                    errors.append(
                        f"E_OWNERSHIP_OVERLAP: {component.get('id')} owns {owned} which overlaps "
                        f"{other_path} owned by {other_id}"
                    )
            peers.append((component.get("id"), owned))
    errors += _validate_interface_owners(manifest, components)
    host = next((c for c in components if c.get("id") == "codex-jev"), None)
    if host is not None and not _component_owns(
        host, "jev/compatibility-manifest.json"
    ):
        errors.append(
            "E_OWNERSHIP_HOST: the host component must own the jev/ integration tree"
        )
    return errors


def _validate_interface_owners(manifest, components):
    errors = []
    interfaces = manifest.get("interfaces", {})
    owners = manifest.get("ownership", {}).get("interface_owners")
    if not isinstance(owners, dict) or not owners:
        return [
            "E_OWNERSHIP_INTERFACE: ownership.interface_owners must assign one owner per shared interface"
        ]
    by_id = {component.get("id"): component for component in components}
    for interface in interfaces:
        owner = owners.get(interface)
        if owner is None:
            errors.append(
                f"E_OWNERSHIP_INTERFACE: shared interface {interface} has no declared owner"
            )
            continue
        if owner not in by_id:
            errors.append(
                f"E_OWNERSHIP_INTERFACE: interface {interface} is owned by unknown component {owner}"
            )
            continue
        if interface not in (by_id[owner].get("provides") or []):
            errors.append(
                f"E_OWNERSHIP_INTERFACE: {owner} owns interface {interface} but does not provide it"
            )
    for interface in owners:
        if interface not in interfaces:
            errors.append(
                f"E_OWNERSHIP_INTERFACE: interface_owners names undeclared interface {interface}"
            )
    return errors


def _validate_patches(manifest, repo_root):
    errors = []
    patches = manifest.get("patches")
    if not isinstance(patches, list):
        return ["E_PATCH_SCHEMA: patches must be a JSON array"]
    components = {
        c.get("id"): c for c in manifest.get("components", []) if isinstance(c, dict)
    }
    host = components.get("codex-jev")
    base_commit = manifest.get("host", {}).get("base_commit")
    orders = set()
    for patch in patches:
        if not isinstance(patch, dict):
            errors.append("E_PATCH_SCHEMA: every patch must be a JSON object")
            continue
        patch_id = patch.get("id", "<missing id>")
        missing = sorted(PATCH_REQUIRED_KEYS - set(patch))
        if missing:
            errors.append(
                f"E_PATCH_SCHEMA: patch {patch_id} is missing keys: {', '.join(missing)}"
            )
            continue
        if patch["order"] in orders:
            errors.append(
                f"E_PATCH_ORDER: patch {patch_id} reuses order {patch['order']}; patch order must be unique"
            )
        orders.add(patch["order"])
        if patch["component"] not in components:
            errors.append(
                f"E_PATCH_COMPONENT: patch {patch_id} names unknown component {patch['component']}"
            )
        if patch["applies_to_host_base"] != base_commit:
            errors.append(
                f"E_PATCH_BASE: patch {patch_id} applies to {patch['applies_to_host_base']} "
                f"but the host is pinned at {base_commit}"
            )
        if not SHA256_RE.match(str(patch["sha256"])):
            errors.append(
                f"E_PATCH_HASH: patch {patch_id} must record a lowercase sha256 digest"
            )
        if host is not None:
            for target in patch["targets"]:
                if not _component_owns(host, target):
                    errors.append(
                        f"E_PATCH_OWNERSHIP: patch {patch_id} targets {target}, which the host component does not own"
                    )
        patch_path = repo_root / patch["file"]
        if not patch_path.is_file():
            errors.append(f"E_PATCH_HASH: patch file is missing: {patch['file']}")
            continue
        actual = sha256_file(patch_path)
        if actual != patch["sha256"]:
            errors.append(
                f"E_PATCH_HASH: patch {patch_id} digest mismatch: manifest {patch['sha256']}, file {actual}"
            )
    return errors


def _validate_features(manifest):
    errors = []
    features = manifest.get("features")
    if not isinstance(features, dict) or not features:
        return ["E_FEATURE_SCHEMA: features must be a non-empty JSON object"]
    components = {
        c.get("id") for c in manifest.get("components", []) if isinstance(c, dict)
    }
    patch_ids = {
        p.get("id") for p in manifest.get("patches", []) if isinstance(p, dict)
    }
    credentials = manifest.get("credentials", {})
    for name, spec in features.items():
        if not isinstance(spec, dict):
            errors.append(f"E_FEATURE_SCHEMA: feature {name} must be a JSON object")
            continue
        missing = sorted(FEATURE_REQUIRED_KEYS - set(spec))
        if missing:
            errors.append(
                f"E_FEATURE_SCHEMA: feature {name} is missing keys: {', '.join(missing)}"
            )
            continue
        unknown = sorted(set(spec) - FEATURE_REQUIRED_KEYS - FEATURE_OPTIONAL_KEYS)
        if unknown:
            errors.append(
                f"E_FEATURE_SCHEMA: feature {name} has unsupported keys: {', '.join(unknown)}"
            )
        if not isinstance(spec["default"], bool):
            errors.append(f"E_FEATURE_SCHEMA: feature {name} default must be a boolean")
        if not isinstance(spec["requires"], list):
            errors.append(f"E_FEATURE_SCHEMA: feature {name} requires must be a list")
        for required in spec["requires"]:
            if required not in features:
                errors.append(
                    f"E_FEATURE_UNKNOWN_REQUIREMENT: feature {name} requires unknown feature {required}"
                )
            if required == name:
                errors.append(
                    f"E_FEATURE_UNKNOWN_REQUIREMENT: feature {name} requires itself"
                )
        for component in spec["components"]:
            if component not in components:
                errors.append(
                    f"E_FEATURE_UNKNOWN_COMPONENT: feature {name} names unknown component {component}"
                )
        for patch_id in spec.get("requires_patches", []):
            if patch_id not in patch_ids:
                errors.append(
                    f"E_FEATURE_UNKNOWN_PATCH: feature {name} requires unknown patch {patch_id}"
                )
        if spec.get("build_time_only") and not spec.get("requires_patches"):
            errors.append(
                f"E_FEATURE_BUILD_TIME_PATCH: build-time feature {name} must name the patches that realize it"
            )
        for key in ("requires_consent", "requires_budget"):
            credential = spec.get(key)
            if credential is not None and credential not in credentials:
                errors.append(
                    f"E_FEATURE_UNKNOWN_CREDENTIAL: feature {name} {key} names unknown credential {credential}"
                )
    return errors


def _validate_credentials(manifest):
    errors = []
    credentials = manifest.get("credentials")
    if not isinstance(credentials, dict) or not credentials:
        return ["E_CREDENTIAL_SCHEMA: credentials must be a non-empty JSON object"]
    for name, spec in credentials.items():
        if not isinstance(spec, dict):
            errors.append(
                f"E_CREDENTIAL_SCHEMA: credential {name} must be a JSON object"
            )
            continue
        if not isinstance(spec.get("consent"), (bool, str)):
            errors.append(
                f"E_CREDENTIAL_SCHEMA: credential {name} must record a consent value"
            )
        budget = spec.get("budget_usd_max")
        if budget is not None and (not isinstance(budget, (int, float)) or budget <= 0):
            errors.append(
                f"E_CREDENTIAL_BUDGET: credential {name} budget_usd_max must be a positive number or null"
            )
    return errors


def _check_remote_authorization(manifest, effective):
    errors = []
    for name, spec in manifest.get("features", {}).items():
        if not effective.get(name):
            continue
        consent_key = spec.get("requires_consent")
        budget_key = spec.get("requires_budget")
        if consent_key is None and budget_key is None:
            continue
        credential = manifest.get("credentials", {}).get(consent_key or budget_key, {})
        if consent_key and credential.get("consent") is not True:
            errors.append(
                f"E_REMOTE_INFERENCE_UNAUTHORIZED: feature {name} needs explicit consent "
                f"({consent_key}.consent must be true)"
            )
        if budget_key:
            budget = credential.get("budget_usd_max")
            if not isinstance(budget, (int, float)) or budget <= 0:
                errors.append(
                    f"E_REMOTE_INFERENCE_UNAUTHORIZED: feature {name} needs a positive budget "
                    f"({budget_key}.budget_usd_max)"
                )
    return errors


def _check_feature_closure(manifest, effective, code):
    errors = []
    for name, spec in manifest.get("features", {}).items():
        if not effective.get(name):
            continue
        for required in spec.get("requires", []):
            if not effective.get(required):
                errors.append(
                    f"{code}: feature {name} is enabled while required feature {required} is disabled"
                )
    return errors


def _validate_default_profile(manifest):
    effective = _feature_defaults(manifest)
    errors = _check_feature_closure(manifest, effective, "E_FEATURE_DEFAULT_CONFLICT")
    errors += _check_remote_authorization(manifest, effective)
    return errors


def _validate_profile(manifest, profile):
    errors = []
    if not isinstance(profile, dict):
        return ["E_PROFILE_SCHEMA: profile must be a JSON object"]
    if profile.get("profile_version") != PROFILE_VERSION:
        errors.append(
            f"E_PROFILE_VERSION: profile_version must be {PROFILE_VERSION}, "
            f"found {profile.get('profile_version')!r}"
        )
    missing = sorted({"profile_version", "id", "features"} - set(profile))
    if missing:
        errors.append(
            f"E_PROFILE_SCHEMA: profile is missing keys: {', '.join(missing)}"
        )
    unknown = sorted(set(profile) - PROFILE_KEYS)
    if unknown:
        errors.append(
            f"E_PROFILE_SCHEMA: unsupported profile keys: {', '.join(unknown)}"
        )
    features = profile.get("features", {})
    if not isinstance(features, dict):
        return errors + ["E_PROFILE_SCHEMA: profile features must be a JSON object"]
    for name, enabled in features.items():
        if name not in manifest.get("features", {}):
            errors.append(
                f"E_PROFILE_UNKNOWN_FEATURE: profile sets unknown feature {name}"
            )
        if not isinstance(enabled, bool):
            errors.append(f"E_PROFILE_SCHEMA: profile feature {name} must be a boolean")
    effective = _effective_features(manifest, profile)
    errors += _check_feature_closure(manifest, effective, "E_PROFILE_DEPENDENCY")
    errors += _check_remote_authorization(manifest, effective)
    return errors


def check_patch_state(manifest, repo_root, expected):
    """Check that the working tree matches ``expected`` (``applied`` or ``absent``)."""
    errors = []
    for patch in sorted(
        manifest.get("patches", []), key=lambda item: item.get("order", 0)
    ):
        patch_file = Path(repo_root) / patch["file"]
        if not patch_file.is_file():
            errors.append(f"E_PATCH_HASH: patch file is missing: {patch['file']}")
            continue
        args = ["git", "apply", "--check"]
        if expected == "applied":
            args.append("--reverse")
        args.append(str(patch_file))
        result = subprocess.run(
            args, cwd=repo_root, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            detail = detail[0] if detail else "git apply --check failed"
            errors.append(
                f"E_PATCH_STATE: patch {patch['id']} is not {expected} in this checkout: {detail}"
            )
    return errors


def check_checkout(manifest, repo_root):
    """Check that the checkout HEAD is the pinned host commit."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ["E_CHECKOUT_MISMATCH: unable to resolve HEAD in this checkout"]
    head = result.stdout.strip()
    base = manifest.get("host", {}).get("base_commit")
    if head != base:
        return [
            f"E_CHECKOUT_MISMATCH: checkout HEAD is {head} but the manifest pins host base {base}"
        ]
    return []


def resolve_component_revisions(components_root):
    """Return ``{component id: revision}`` for locally cloned component checkouts."""
    revisions = {}
    components_root = Path(components_root)
    for entry in sorted(components_root.iterdir()) if components_root.is_dir() else []:
        if not (entry / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=entry,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            revisions[entry.name] = result.stdout.strip()
    return revisions


def check_component_revisions(manifest, components_root):
    """Check local component checkouts against the pinned revisions."""
    errors = []
    revisions = resolve_component_revisions(components_root)
    for component in manifest.get("components", []):
        component_id = component.get("id")
        if component_id == "codex-jev" or component_id not in revisions:
            continue
        if revisions[component_id] != component.get("revision"):
            errors.append(
                f"E_COMPONENT_REVISION: local checkout of {component_id} is at "
                f"{revisions[component_id]} but the manifest pins {component.get('revision')}"
            )
    return errors
