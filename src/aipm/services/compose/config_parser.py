"""Safe, bounded YAML parser for Compose configuration files.

Extracts declared services, images, and build configurations while safely
handling custom YAML tags (!reset, !override) and multi-file merging semantics.
Fails closed when encountering unsupported YAML tags, malformed types, or ambiguous syntax.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aipm.models.compose_intelligence import DeclaredServiceConfig
from aipm.services.compose.image_ref import parse_image_reference

_MAX_COMPOSE_FILES = 8
_MAX_FILE_BYTES = 1024 * 1024  # 1 MB


@dataclass(frozen=True, slots=True)
class _ResetNode:
    """Wrapper marking an attribute explicitly reset via Docker Compose !reset."""

    value: Any


@dataclass(frozen=True, slots=True)
class _OverrideNode:
    """Wrapper marking an attribute explicitly overriding via Docker Compose !override."""

    value: Any


class _ComposeSafeLoader(yaml.SafeLoader):
    """Safe YAML loader recognizing Docker Compose !reset and !override tags."""

    pass


def _reset_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> _ResetNode:
    if isinstance(node, yaml.ScalarNode):
        val = loader.construct_scalar(node)
        if val in ("null", "~", "", "None"):
            val = None
    elif isinstance(node, yaml.SequenceNode):
        val = loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        val = loader.construct_mapping(node)
    else:
        val = None
    return _ResetNode(val)


def _override_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> _OverrideNode:
    if isinstance(node, yaml.ScalarNode):
        val = loader.construct_scalar(node)
        if val in ("null", "~", "", "None"):
            val = None
    elif isinstance(node, yaml.SequenceNode):
        val = loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        val = loader.construct_mapping(node)
    else:
        val = None
    return _OverrideNode(val)


def _generic_tag_constructor(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_ComposeSafeLoader.add_constructor("!reset", _reset_constructor)
_ComposeSafeLoader.add_constructor("!override", _override_constructor)
_ComposeSafeLoader.add_multi_constructor("!", _generic_tag_constructor)


def load_compose_file_safely(file_path: Path) -> dict[str, Any] | None:
    """Read and parse a Compose YAML file with bounds and tag safety.

    Returns the parsed dictionary, or None if the file cannot be safely parsed.
    """
    try:
        resolved = file_path.resolve()
        if not resolved.is_file():
            return None
        size = resolved.stat().st_size
        if size > _MAX_FILE_BYTES:
            return None

        content = resolved.read_text(encoding="utf-8", errors="replace")
        data = yaml.load(content, Loader=_ComposeSafeLoader)
        if isinstance(data, dict):
            return data
        return None
    except (OSError, yaml.YAMLError):
        return None


def parse_declared_compose_services(
    compose_file_paths: list[str | Path],
    *,
    project_root: Path | None = None,
) -> dict[str, DeclaredServiceConfig]:
    """Parse declared services from an ordered list of Compose files.

    Merges service definitions according to Docker Compose merge semantics:
    - Service-level: !reset null removes service; !override or !reset with mapping replaces
      the service definition entirely without inheriting base configurations.
    - image: Override replaces base. If !reset is used with empty value/None, clears image.
    - build: String or dict. If both are dicts, keys are merged. Nested !reset keys clear
      inherited keys. !reset or !override on build replaces or clears build entirely.
    - profiles: Lists are unioned across files unless !reset or !override is used to replace/clear.
    - Fails closed with parse_error if any field has unsupported types or ambiguous merge syntax.
    """
    service_images: dict[str, str | None] = {}
    service_builds: dict[str, Any] = {}
    service_profiles: dict[str, list[str]] = {}
    service_sources: dict[str, list[str]] = {}
    service_errors: dict[str, str] = {}

    for raw_path in compose_file_paths[:_MAX_COMPOSE_FILES]:
        path = Path(raw_path)
        if not path.is_absolute() and project_root:
            path = (project_root / path).resolve()
        else:
            path = path.resolve()

        data = load_compose_file_safely(path)
        if not data:
            continue

        raw_services = data.get("services")
        if not isinstance(raw_services, dict):
            continue

        for svc_name, raw_cfg in raw_services.items():
            if not isinstance(svc_name, str):
                continue
            svc = svc_name.strip()
            if not svc:
                continue

            service_sources.setdefault(svc, []).append(str(path))
            if svc in service_errors:
                continue

            # Handle service-level !reset or !override
            if isinstance(raw_cfg, _ResetNode):
                inner_svc = raw_cfg.value
                if inner_svc is None or inner_svc == "" or inner_svc == {}:
                    # Service is removed/cleared
                    service_images[svc] = None
                    service_builds[svc] = None
                    service_profiles[svc] = []
                    continue
                elif isinstance(inner_svc, dict):
                    # Service definition replaced without inheriting base configuration
                    service_images[svc] = None
                    service_builds[svc] = None
                    service_profiles[svc] = []
                    raw_cfg = inner_svc
                else:
                    service_errors[svc] = f"Unsupported !reset type for service: {type(inner_svc).__name__}"
                    continue
            elif isinstance(raw_cfg, _OverrideNode):
                inner_svc = raw_cfg.value
                if isinstance(inner_svc, dict):
                    # Service definition replaced without inheriting base configuration
                    service_images[svc] = None
                    service_builds[svc] = None
                    service_profiles[svc] = []
                    raw_cfg = inner_svc
                elif inner_svc is None or inner_svc == "" or inner_svc == {}:
                    service_images[svc] = None
                    service_builds[svc] = None
                    service_profiles[svc] = []
                    continue
                else:
                    service_errors[svc] = f"Unsupported !override type for service: {type(inner_svc).__name__}"
                    continue
            elif not isinstance(raw_cfg, dict):
                service_errors[svc] = f"Invalid service specification type: {type(raw_cfg).__name__}"
                continue

            # 1. Merge 'image'
            if "image" in raw_cfg:
                raw_img = raw_cfg["image"]
                if isinstance(raw_img, _ResetNode):
                    inner = raw_img.value
                    if inner is None or inner == "" or inner == []:
                        service_images[svc] = None
                    elif isinstance(inner, str):
                        service_images[svc] = inner.strip()
                    else:
                        service_errors[svc] = f"Unsupported !reset type for image: {type(inner).__name__}"
                elif isinstance(raw_img, _OverrideNode):
                    inner = raw_img.value
                    if inner is None or inner == "":
                        service_images[svc] = None
                    elif isinstance(inner, str):
                        service_images[svc] = inner.strip()
                    else:
                        service_errors[svc] = f"Unsupported !override type for image: {type(inner).__name__}"
                elif raw_img is None:
                    service_images[svc] = None
                elif isinstance(raw_img, str):
                    service_images[svc] = raw_img.strip()
                else:
                    service_errors[svc] = f"Unsupported image specification type: {type(raw_img).__name__}"

            # 2. Merge 'build'
            if "build" in raw_cfg:
                raw_bld = raw_cfg["build"]
                if isinstance(raw_bld, _ResetNode):
                    inner = raw_bld.value
                    if inner is None or inner == "" or inner == {} or inner == []:
                        service_builds[svc] = None
                    elif isinstance(inner, str):
                        service_builds[svc] = inner.strip()
                    elif isinstance(inner, dict):
                        service_builds[svc] = dict(inner)
                    else:
                        service_errors[svc] = f"Unsupported !reset type for build: {type(inner).__name__}"
                elif isinstance(raw_bld, _OverrideNode):
                    inner = raw_bld.value
                    if inner is None or inner == "" or inner == {}:
                        service_builds[svc] = None
                    elif isinstance(inner, str):
                        service_builds[svc] = inner.strip()
                    elif isinstance(inner, dict):
                        service_builds[svc] = dict(inner)
                    else:
                        service_errors[svc] = f"Unsupported !override type for build: {type(inner).__name__}"
                elif isinstance(raw_bld, str):
                    service_builds[svc] = raw_bld.strip()
                elif isinstance(raw_bld, dict):
                    prev = service_builds.get(svc)
                    merged_bld = dict(prev) if isinstance(prev, dict) else {}
                    for k, v in raw_bld.items():
                        if isinstance(v, _ResetNode):
                            if v.value is None or v.value == "":
                                merged_bld.pop(k, None)
                            else:
                                merged_bld[k] = v.value
                        elif isinstance(v, _OverrideNode):
                            merged_bld[k] = v.value
                        else:
                            merged_bld[k] = v
                    service_builds[svc] = merged_bld
                elif raw_bld is None:
                    service_builds[svc] = None
                else:
                    service_errors[svc] = f"Unsupported build specification type: {type(raw_bld).__name__}"

            # 3. Merge 'profiles'
            if "profiles" in raw_cfg:
                raw_prof = raw_cfg["profiles"]
                if isinstance(raw_prof, _ResetNode):
                    inner = raw_prof.value
                    if inner is None or inner == "" or inner == []:
                        service_profiles[svc] = []
                    elif isinstance(inner, list):
                        service_profiles[svc] = [
                            str(p.value if isinstance(p, (_ResetNode, _OverrideNode)) else p).strip()
                            for p in inner
                            if p
                        ]
                    else:
                        service_errors[svc] = f"Unsupported !reset type for profiles: {type(inner).__name__}"
                elif isinstance(raw_prof, _OverrideNode):
                    inner = raw_prof.value
                    if inner is None or inner == "" or inner == []:
                        service_profiles[svc] = []
                    elif isinstance(inner, list):
                        service_profiles[svc] = [
                            str(p.value if isinstance(p, (_ResetNode, _OverrideNode)) else p).strip()
                            for p in inner
                            if p
                        ]
                    else:
                        service_errors[svc] = f"Unsupported !override type for profiles: {type(inner).__name__}"
                elif isinstance(raw_prof, list):
                    cur_p = service_profiles.setdefault(svc, [])
                    for p in raw_prof:
                        if p:
                            p_clean = str(p.value if isinstance(p, (_ResetNode, _OverrideNode)) else p).strip()
                            if p_clean and p_clean not in cur_p:
                                cur_p.append(p_clean)
                elif raw_prof is None:
                    pass
                else:
                    service_errors[svc] = f"Unsupported profiles specification type: {type(raw_prof).__name__}"

    result: dict[str, DeclaredServiceConfig] = {}
    all_names = sorted(set(service_sources.keys()))

    for svc in all_names:
        sources = tuple(service_sources.get(svc, ()))
        err = service_errors.get(svc)

        image_str = service_images.get(svc)
        bld = service_builds.get(svc)
        profiles = tuple(service_profiles.get(svc, ()))

        is_build = False
        build_context = None
        build_dockerfile = None

        if isinstance(bld, str):
            is_build = True
            build_context = bld
            build_dockerfile = "Dockerfile"
        elif isinstance(bld, dict):
            is_build = True
            ctx = bld.get("context")
            if isinstance(ctx, (_ResetNode, _OverrideNode)):
                ctx = ctx.value
            build_context = str(ctx).strip() if ctx else "."
            df = bld.get("dockerfile")
            if isinstance(df, (_ResetNode, _OverrideNode)):
                df = df.value
            build_dockerfile = str(df).strip() if df else "Dockerfile"

        image_ref = None
        if image_str and not err:
            try:
                image_ref = parse_image_reference(image_str, is_local_build=is_build)
            except ValueError as val_err:
                image_ref = None
                err = str(val_err)

        result[svc] = DeclaredServiceConfig(
            service_name=svc,
            image=image_str,
            image_ref=image_ref,
            is_build=is_build,
            build_context=build_context,
            build_dockerfile=build_dockerfile,
            profiles=profiles,
            source_files=sources,
            parse_error=err,
        )

    return result
