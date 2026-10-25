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
const RELEASE_AGE_DAYS = parseInt(process.env.PNPM_CLI_MIN_RELEASE_AGE_DAYS ?? "7", 10);
const RELEASE_AGE_MINUTES = RELEASE_AGE_DAYS * 24 * 60;
const PNPM_VERSION = execSync("pnpm --version", { encoding: "utf8" }).trim();
const PNPM_MAJOR = parseInt(PNPM_VERSION.split(".")[0], 10);
const AUDIT_IGNORE_CVES = (process.env.PNPM_AUDIT_IGNORE_CVES ?? "").split(",").map(s => s.trim()).filter(Boolean);
const AUDIT_FORCE_FIX_COMPONENTS = (process.env.PNPM_AUDIT_FORCE_FIX_COMPONENTS ?? "").split(",").map(s => s.trim()).filter(Boolean);
const MIN_RELEASE_AGE_IGNORE_COMPONENTS = (process.env.PNPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS ?? "").split(",").map(s => s.trim()).filter(Boolean);
const BIN_DIR = path.join(os.homedir(), ".local/bin");
const NOW = Date.now();
// When set, only install this one component (used for per-layer Docker caching).
const INSTALL_COMPONENT = process.env.INSTALL_COMPONENT ?? null;

// builtDependency: true  — package downloads a platform-native binary via postinstall;
//   we fetch its optionalDependencies from the registry and build a precise allowlist.
// allowAllBuilds: true   — package has transitive regular dependencies that need build
//   scripts (e.g. koffi, protobufjs); their names can't be enumerated without recursively
//   resolving the dep tree, so we use dangerously-allow-all-builds scoped to the install dir.
const ALL_COMPONENTS = [
  { component: "claude",    package: "@anthropic-ai/claude-code",        version: process.env.CLAUDE_VERSION    ?? "latest", binary: "claude",    enabled: process.env.WITH_CLAUDE    !== "0", builtDependency: true,  allowAllBuilds: false },
  { component: "codex",     package: "@openai/codex",                    version: process.env.CODEX_VERSION     ?? "latest", binary: "codex",     enabled: process.env.WITH_CODEX     !== "0", builtDependency: false, allowAllBuilds: false },
  { component: "gemini",    package: "@google/gemini-cli",               version: process.env.GEMINI_VERSION    ?? "latest", binary: "gemini",    enabled: process.env.WITH_GEMINI    !== "0", builtDependency: false, allowAllBuilds: true  },
  { component: "jules",     package: "@google/jules",                    version: process.env.JULES_VERSION     ?? "latest", binary: "jules",     enabled: process.env.WITH_JULES     === "1", builtDependency: false, allowAllBuilds: false },
  { component: "opencode",  package: "opencode-ai",                      version: process.env.OPENCODE_VERSION  ?? "latest", binary: "opencode",  enabled: process.env.WITH_OPENCODE  !== "0", builtDependency: true,  allowAllBuilds: false },
  { component: "copilot",   package: "@github/copilot",                  version: process.env.COPILOT_VERSION   ?? "latest", binary: "copilot",   enabled: process.env.WITH_COPILOT   !== "0", builtDependency: false, allowAllBuilds: false },
  { component: "pi",        package: "@earendil-works/pi-coding-agent",  version: process.env.PI_VERSION        ?? "latest", binary: "pi",        enabled: process.env.WITH_PI        !== "0", builtDependency: false, allowAllBuilds: true  },
];

if (INSTALL_COMPONENT !== null && !ALL_COMPONENTS.some(c => c.component === INSTALL_COMPONENT)) {
  console.error(`Unknown INSTALL_COMPONENT: "${INSTALL_COMPONENT}". Valid values: ${ALL_COMPONENTS.map(c => c.component).join(", ")}`);
  process.exit(1);
}

const COMPONENTS = INSTALL_COMPONENT !== null
  ? ALL_COMPONENTS.filter(c => c.component === INSTALL_COMPONENT)
  : ALL_COMPONENTS;

if (isNaN(RELEASE_AGE_DAYS) || RELEASE_AGE_DAYS < 0) {
  console.error("PNPM_CLI_MIN_RELEASE_AGE_DAYS must be a non-negative integer");
  process.exit(1);
}

fs.mkdirSync(BIN_DIR, { recursive: true });
fs.mkdirSync(INSTALL_ROOT, { recursive: true });
fs.mkdirSync(METADATA_ROOT, { recursive: true });
// Use append when running per-component (multiple RUN layers each write their slice).
// Use truncate for a full install run so the file starts clean.
if (INSTALL_COMPONENT === null) {
  fs.writeFileSync(SUMMARY_FILE, "");
  fs.writeFileSync(JSONL_FILE, "");
}

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

async function installCli({ component, package: packageName, version: versionSpec, binary, enabled, builtDependency, allowAllBuilds }) {
  if (!enabled) return;

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
  fs.writeFileSync(path.join(installDir, ".npmrc"), "fetch-timeout=600000\n");

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
      `  If the bin name differs, update the 'binary' field in COMPONENTS.`
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

for (const component of COMPONENTS) {
  await installCli(component);
}
