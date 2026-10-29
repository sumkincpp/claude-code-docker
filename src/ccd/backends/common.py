"""Helpers shared by the backends that build the project Dockerfile."""

import logging
from pathlib import Path

from ccd.config import BuildConfig
from ccd.features import version_variable
from ccd.plugin import BackendError, BuildRequest

logger = logging.getLogger(__name__)


def build_inputs(request: BuildRequest) -> tuple[Path, Path]:
    """The generated Dockerfile and its build context."""
    if request.dockerfile is None or request.context is None:
        raise BackendError("Nothing to build: the build request has no Dockerfile")
    return request.dockerfile, request.context


def to_docker_build_args(config: BuildConfig, extra_args: list[str]) -> list[str]:
    """Produce the full list of build flag tokens (accepted by `docker build` and `buildah bud`)."""
    result: list[str] = [*extra_args]

    if config.force:
        logger.debug("Force flag set, disabling Docker layer caching")
        result.append("--no-cache")

    logger.debug("Setting NPM_CLI_MIN_RELEASE_AGE_DAYS=%s", config.npm_min_release_age_days)
    result.extend(["--build-arg", f"NPM_CLI_MIN_RELEASE_AGE_DAYS={config.npm_min_release_age_days}"])

    if config.npm_audit_ignore_components:
        logger.debug("Setting NPM_AUDIT_IGNORE_COMPONENTS=%s", config.npm_audit_ignore_components)
        result.extend(["--build-arg", f"NPM_AUDIT_IGNORE_COMPONENTS={','.join(sorted(config.npm_audit_ignore_components))}"])
    if config.npm_audit_force_fix_components:
        logger.debug("Setting NPM_AUDIT_FORCE_FIX_COMPONENTS=%s", config.npm_audit_force_fix_components)
        result.extend(["--build-arg", f"NPM_AUDIT_FORCE_FIX_COMPONENTS={','.join(sorted(config.npm_audit_force_fix_components))}"])
    if config.npm_min_release_age_ignore_components:
        logger.debug("Setting NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS=%s", config.npm_min_release_age_ignore_components)
        result.extend(["--build-arg", f"NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS={','.join(sorted(config.npm_min_release_age_ignore_components))}"])

    for component, version in sorted(config.versions.items()):
        build_arg = version_variable(component)
        logger.debug("Setting %s=%s", build_arg, version)
        result.extend(["--build-arg", f"{build_arg}={version}"])

    return result
