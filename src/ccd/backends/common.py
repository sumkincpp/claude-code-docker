"""Helpers shared by the backends that build the project Dockerfile."""

import logging

from ccd.config import FEATURE_BUILD_ARGS, VERSION_BUILD_ARGS, BuildConfig

logger = logging.getLogger(__name__)


def to_docker_build_args(config: BuildConfig, extra_args: list[str]) -> list[str]:
    """Produce the full list of build flag tokens (accepted by `docker build` and `buildah bud`)."""
    result: list[str] = [*extra_args]

    if config.force:
        logger.debug("Force flag set, disabling Docker layer caching")
        result.append("--no-cache")

    flags = config.feature_flags()
    if flags is not None:
        for feature, build_arg in sorted(FEATURE_BUILD_ARGS.items()):
            result.extend(["--build-arg", f"{build_arg}={'1' if flags[feature] else '0'}"])

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

    for component, build_arg in sorted(VERSION_BUILD_ARGS.items()):
        if component in config.versions:
            logger.debug("Setting %s=%s", build_arg, config.versions[component])
            result.extend(["--build-arg", f"{build_arg}={config.versions[component]}"])

    return result
