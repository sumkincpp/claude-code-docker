#!/usr/bin/env node
import fs from "fs";
import os from "os";
import path from "path";
import { execSync } from "child_process";
import { fetchRegistryData, resolveVersion } from "./resolve-version.mjs";

const INSTALL_ROOT = process.env.PNPM_CLI_INSTALL_ROOT ?? path.join(os.homedir(), ".local/share/ccd/pnpm-installs");
const METADATA_ROOT = process.env.PNPM_CLI_METADATA_ROOT ?? "/metadata";
const SUMMARY_FILE = path.join(METADATA_ROOT, "pnpm-cli-resolution.txt");
const JSONL_FILE = path.join(METADATA_ROOT, "pnpm-cli-resolution.jsonl");
const RELEASE_AGE_DAYS = parseInt(process.env.NPM_CLI_MIN_RELEASE_AGE_DAYS ?? "7", 10);
const RELEASE_AGE_MINUTES = RELEASE_AGE_DAYS * 24 * 60;
const PNPM_VERSION = execSync("pnpm --version", { encoding: "utf8" }).trim();
const PNPM_MAJOR = parseInt(PNPM_VERSION.split(".")[0], 10);
const AUDIT_IGNORE_CVES = (process.env.PNPM_AUDIT_IGNORE_CVES ?? "").split(",").map(s => s.trim()).filter(Boolean);
const AUDIT_FORCE_FIX_COMPONENTS = (process.env.NPM_AUDIT_FORCE_FIX_COMPONENTS ?? "").split(",").map(s => s.trim()).filter(Boolean);
const MIN_RELEASE_AGE_IGNORE_COMPONENTS = (process.env.NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS ?? "").split(",").map(s => s.trim()).filter(Boolean);
const BIN_DIR = path.join(os.homedir(), ".local/bin");
const PNPM_STORE_DIR = process.env.PNPM_STORE_DIR ?? path.join(os.homedir(), ".pnpm-store");
const NOW = Date.now();

// Usage: install-clis.mjs <component> <package> --binary <name> [--version <spec>] [--built-dependency] [--allow-all-builds]
//
// built-dependency: the package downloads a platform-native binary via postinstall;
//   we fetch its optionalDependencies from the registry and build a precise allowlist.
// allow-all-builds: the package has transitive regular dependencies that need build
//   scripts (e.g. koffi, protobufjs); their names can't be enumerated without recursively
//   resolving the dep tree, so we use dangerously-allow-all-builds scoped to the install dir.
function parseArgs(argv) {
  const [component, packageName, ...rest] = argv;
  const options = { version: "latest", binary: null, builtDependency: false, allowAllBuilds: false };
  for (let i = 0; i < rest.length; i++) {
    if (rest[i] === "--binary") options.binary = rest[++i];
    else if (rest[i] === "--version") options.version = rest[++i] || "latest";
    else if (rest[i] === "--built-dependency") options.builtDependency = true;
    else if (rest[i] === "--allow-all-builds") options.allowAllBuilds = true;
    else throw new Error(`Unknown argument: ${rest[i]}`);
  }
  if (!component || !packageName || !options.binary) {
    console.error("Usage: install-clis.mjs <component> <package> --binary <name> [--version <spec>] [--built-dependency] [--allow-all-builds]");
    process.exit(1);
  }
  return { component, package: packageName, ...options };
}

const COMPONENT = parseArgs(process.argv.slice(2));

if (isNaN(RELEASE_AGE_DAYS) || RELEASE_AGE_DAYS < 0) {
  console.error("NPM_CLI_MIN_RELEASE_AGE_DAYS must be a non-negative integer");
  process.exit(1);
}

fs.mkdirSync(BIN_DIR, { recursive: true });
fs.mkdirSync(INSTALL_ROOT, { recursive: true });
fs.mkdirSync(METADATA_ROOT, { recursive: true });
if (RELEASE_AGE_DAYS > 0) {
  console.log(`Applying release-age policy: ${RELEASE_AGE_DAYS} days (${RELEASE_AGE_MINUTES} minutes) via pnpm minimumReleaseAge`);
} else {
  console.log("Applying release-age policy: disabled");
}

function findNextVersionAfter(registryData, publishedAfter) {
  const times = registryData.time ?? {};
  const stablePattern = /^\d+\.\d+\.\d+$/;
  const afterEpoch = Date.parse(publishedAfter);
  let nextVersion = null;
  let nextEpoch = null;

  for (const [version, publishedAt] of Object.entries(times)) {
    if (version === "created" || version === "modified") continue;
    if (!stablePattern.test(version)) continue;
    const epoch = Date.parse(publishedAt);
    if (!Number.isFinite(epoch) || epoch <= afterEpoch) continue;
    if (nextEpoch === null || epoch < nextEpoch) {
      nextVersion = version;
      nextEpoch = epoch;
    }
  }

  return nextVersion ? { version: nextVersion, publishedAt: new Date(nextEpoch).toISOString() } : null;
}

function formatWaitTime(remainingMs) {
  const remainingSeconds = Math.floor(remainingMs / 1000);
  if (remainingSeconds <= 0) return "eligible now";
  const days = Math.floor(remainingSeconds / 86400);
  const hours = Math.floor((remainingSeconds % 86400) / 3600);
  const minutes = Math.floor((remainingSeconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

// packages: ["."] declares the install directory as the workspace root so that
// workspace-level settings (minimumReleaseAge, allowBuilds) apply to its dependencies.
// packages: [] (explicitly empty) is ambiguous across pnpm versions and caused
// onlyBuiltDependencies to be silently ignored in several pnpm 10.x releases.
function buildWorkspaceYaml(releaseAgeMinutes, ignoreCves, allowedBuilds = [], dangerouslyAllowAllBuilds = false) {
  const lines = ['packages:', '  - "."'];
  // pnpm 11+ ignores `store-dir` in .npmrc and the PNPM_STORE_DIR variable; storeDir must be set here.
  // Without it the store lands in ~/.local/share/pnpm and the Docker/buildah cache mount is never used.
  lines.push(`storeDir: ${JSON.stringify(PNPM_STORE_DIR)}`);
  if (releaseAgeMinutes > 0) {
    lines.push(`minimumReleaseAge: ${releaseAgeMinutes}`);
  }
  if (dangerouslyAllowAllBuilds) {
    // dangerouslyAllowAllBuilds is a workspace-level setting; .npmrc does not support it.
    // Used for packages whose transitive build-script deps can't be enumerated ahead of time.
    lines.push("dangerouslyAllowAllBuilds: true");
  } else if (allowedBuilds.length > 0) {
    if (PNPM_MAJOR >= 11) {
      // pnpm 11+: onlyBuiltDependencies renamed to allowBuilds (boolean map)
      lines.push("allowBuilds:");
      for (const pkg of allowedBuilds) {
        lines.push(`  "${pkg}": true`);
      }
    } else {
      // pnpm 9/10: allowlist via onlyBuiltDependencies array
      lines.push("onlyBuiltDependencies:");
      for (const pkg of allowedBuilds) {
        lines.push(`  - "${pkg}"`);
      }
    }
  }
  if (ignoreCves.length > 0) {
    lines.push("auditConfig:");
    lines.push("  ignoreCves:");
    for (const cve of ignoreCves) {
      lines.push(`    - ${cve}`);
    }
  }
  return lines.join("\n") + "\n";
}

function appendSummary(text) {
  fs.appendFileSync(SUMMARY_FILE, text);
  process.stdout.write(text);
}

function appendJsonl(record) {
  fs.appendFileSync(JSONL_FILE, JSON.stringify(record) + "\n");
}

function run(cmd, cwd) {
  execSync(cmd, { cwd, stdio: "inherit" });
}

async function installCli({ component, package: packageName, version: versionSpec, binary, builtDependency, allowAllBuilds }) {
  const forceFix = AUDIT_FORCE_FIX_COMPONENTS.includes(component);
  const installDir = path.join(INSTALL_ROOT, component);

  fs.rmSync(installDir, { recursive: true, force: true });
  fs.mkdirSync(installDir, { recursive: true });

  // pnpm v9+ skips lifecycle scripts by default (supply-chain hardening).
  // For packages that ship a platform-native binary via a postinstall script, we must
  // allowlist both the top-level package AND its optional native-binary dependencies
  // (e.g. @anthropic-ai/claude-code-linux-arm64-musl), since those also run postinstall.
  // We fetch registry metadata first to read the exact optionalDependencies of the
  // resolved version — this works for any naming convention, not just <pkg>-<os>-<arch>.
  let registryData = null;
  let allowedBuilds = [];

  if (builtDependency) {
    registryData = await fetchRegistryData(packageName);
    const cooldownDays = MIN_RELEASE_AGE_IGNORE_COMPONENTS.includes(component) ? 0 : RELEASE_AGE_DAYS;
    const resolvedSpec = resolveVersion(registryData, versionSpec, cooldownDays) ?? versionSpec;
    const optionalDeps = Object.keys(registryData.versions?.[resolvedSpec]?.optionalDependencies ?? {});
    allowedBuilds = [packageName, ...optionalDeps];
    console.log(`[${component}] allowedBuilds (for logging): ${allowedBuilds.join(", ")}`);
  }

  const pkgJson = {
    name: `ccd-pnpm-cli-${component}`,
    private: true,
    packageManager: `pnpm@${PNPM_VERSION}`,
    // Declare dependency upfront so pnpm install (not pnpm add) is used.
    // pnpm 11 blocks lifecycle scripts for packages added via `pnpm add` even when
    // onlyBuiltDependencies is set — pnpm install respects it correctly.
    dependencies: { [packageName]: versionSpec },
  };
  fs.writeFileSync(path.join(installDir, "package.json"), JSON.stringify(pkgJson, null, 2) + "\n");
  // Large platform-specific binaries (e.g. claude-code-linux-x64) can exceed pnpm's
  // default 60s fetch timeout — set a generous limit per install directory.
  fs.writeFileSync(path.join(installDir, ".npmrc"), `fetch-timeout=600000\nstore-dir=${PNPM_STORE_DIR}\n`);

  const releaseAgeMinutes = MIN_RELEASE_AGE_IGNORE_COMPONENTS.includes(component) ? 0 : RELEASE_AGE_MINUTES;
  if (releaseAgeMinutes === 0 && RELEASE_AGE_MINUTES > 0) {
    console.log(`[${component}] release-age policy disabled (exempted component)`);
  }
  // allowAllBuilds: packages with transitive regular deps that have build scripts
  // (e.g. pi → koffi, protobufjs) — names can't be enumerated ahead of time so we use
  // dangerouslyAllowAllBuilds, which is a workspace-level setting (not supported in .npmrc).
  fs.writeFileSync(
    path.join(installDir, "pnpm-workspace.yaml"),
    buildWorkspaceYaml(releaseAgeMinutes, AUDIT_IGNORE_CVES, allowedBuilds, allowAllBuilds)
  );

  run("pnpm install", installDir);

  const binDir = path.join(installDir, "node_modules", ".bin");
  const availableBins = fs.existsSync(binDir) ? fs.readdirSync(binDir) : [];
  console.log(`[${component}] bins available after install: ${availableBins.join(", ") || "(none)"}`);

  const binShim = path.join(binDir, binary);
  if (!fs.existsSync(binShim)) {
    throw new Error(
      `[${component}] expected binary shim not found: ${binShim}\n` +
      `  package: ${packageName}\n` +
      `  available bins: ${availableBins.join(", ") || "(none)"}\n` +
      `  If the bin name differs, fix the --binary option.`
    );
  }

  // Reuse registry data fetched before install; fetch now for non-builtDependency packages.
  if (!registryData) registryData = await fetchRegistryData(packageName);
  const times = registryData.time ?? {};
  const latestVersion = registryData["dist-tags"]?.latest ?? null;
  const latestPublishTime = latestVersion ? (times[latestVersion] ?? null) : null;

  const installedPkg = JSON.parse(fs.readFileSync(path.join(installDir, "node_modules", packageName, "package.json"), "utf8"));
  const resolvedVersion = installedPkg.version;
  const resolvedPublishTime = times[resolvedVersion] ?? null;

  let nextEligibleVersion = null;
  let nextEligiblePublishTime = null;
  let waitForNext = "not gated";

  if (RELEASE_AGE_DAYS > 0 && resolvedPublishTime) {
    const next = findNextVersionAfter(registryData, resolvedPublishTime);
    if (next) {
      nextEligibleVersion = next.version;
      nextEligiblePublishTime = next.publishedAt;
      const nextEligibleEpoch = Date.parse(next.publishedAt) + RELEASE_AGE_DAYS * 86400 * 1000;
      waitForNext = formatWaitTime(nextEligibleEpoch - NOW);
    } else {
      nextEligibleVersion = latestVersion;
      nextEligiblePublishTime = latestPublishTime;
      waitForNext = "already on newest known release";
    }
  } else {
    nextEligibleVersion = latestVersion;
    nextEligiblePublishTime = latestPublishTime;
  }

  appendSummary(
    `Installing ${component}:\n` +
    `  requested: ${packageName}@${versionSpec}\n` +
    `  latest: ${latestVersion}${latestPublishTime ? ` (published ${latestPublishTime})` : ""}\n` +
    `  selected: ${resolvedVersion}${resolvedPublishTime ? ` (published ${resolvedPublishTime})` : ""}\n` +
    `  next eligible: ${nextEligibleVersion}${nextEligiblePublishTime ? ` (published ${nextEligiblePublishTime})` : ""}\n` +
    `  wait until selection advances: ${waitForNext}\n` +
    `  pnpm minimumReleaseAge: ${RELEASE_AGE_MINUTES}m\n` +
    `  pnpm audit force fix enabled: ${forceFix}\n\n`
  );

  appendJsonl({
    generated_at: new Date().toISOString(),
    component,
    package_name: packageName,
    version_spec: versionSpec,
    release_age_days: RELEASE_AGE_DAYS,
    release_age_minutes: RELEASE_AGE_MINUTES,
    latest_version: latestVersion,
    latest_publish_time: latestPublishTime,
    selected_version: resolvedVersion,
    selected_publish_time: resolvedPublishTime,
    next_eligible_version: nextEligibleVersion,
    next_eligible_publish_time: nextEligiblePublishTime,
    wait_until_selection_advances: waitForNext,
    force_fix_audit: forceFix,
  });

  if (forceFix) {
    console.log(`Running pnpm audit --fix for ${component}`);
    run("pnpm audit --fix", installDir);
    run("pnpm install", installDir);
  }

  run("pnpm audit", installDir);
  const auditResult = "passed";

  appendSummary(
    `  final installed: ${resolvedVersion}${resolvedPublishTime ? ` (published ${resolvedPublishTime})` : ""}\n` +
    `  final audit result: ${auditResult}\n\n`
  );

  appendJsonl({
    generated_at: new Date().toISOString(),
    component,
    final_installed_version: resolvedVersion,
    final_installed_publish_time: resolvedPublishTime,
    audit_result: auditResult,
  });

  // pnpm .bin shims use `dirname "$0"` to resolve paths into the virtual store.
  // A symlink would set $0 to the /bin path, breaking the relative resolution.
  // Use a wrapper script that invokes the shim via its absolute path instead.
  const binLink = path.join(BIN_DIR, binary);
  if (fs.existsSync(binLink)) fs.unlinkSync(binLink);
  fs.writeFileSync(binLink, `#!/bin/sh\nexec "${binShim}" "$@"\n`);
  fs.chmodSync(binLink, 0o755);
}

await installCli(COMPONENT);
