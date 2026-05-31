#!/usr/bin/env node
import fs from "fs";
import path from "path";
import { execSync } from "child_process";

const INSTALL_ROOT = process.env.PNPM_CLI_INSTALL_ROOT ?? "/home/ubuntu/.local/share/ccd/pnpm-installs";
const METADATA_ROOT = process.env.PNPM_CLI_METADATA_ROOT ?? "/metadata";
const SUMMARY_FILE = path.join(METADATA_ROOT, "pnpm-cli-resolution.txt");
const JSONL_FILE = path.join(METADATA_ROOT, "pnpm-cli-resolution.jsonl");
const RELEASE_AGE_DAYS = parseInt(process.env.PNPM_CLI_MIN_RELEASE_AGE_DAYS ?? "7", 10);
const RELEASE_AGE_MINUTES = RELEASE_AGE_DAYS * 24 * 60;
const PNPM_VERSION = execSync("pnpm --version", { encoding: "utf8" }).trim();
const AUDIT_IGNORE_CVES = (process.env.PNPM_AUDIT_IGNORE_CVES ?? "").split(",").map(s => s.trim()).filter(Boolean);
const AUDIT_FORCE_FIX_COMPONENTS = (process.env.PNPM_AUDIT_FORCE_FIX_COMPONENTS ?? "").split(",").map(s => s.trim()).filter(Boolean);
const MIN_RELEASE_AGE_IGNORE_COMPONENTS = (process.env.PNPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS ?? "").split(",").map(s => s.trim()).filter(Boolean);
const BIN_DIR = "/home/ubuntu/.local/bin";
const NOW = Date.now();

const COMPONENTS = [
  { component: "claude",    package: "@anthropic-ai/claude-code",        version: process.env.CLAUDE_VERSION    ?? "latest", binary: "claude",    enabled: process.env.WITH_CLAUDE    !== "0", builtDependency: true  },
  { component: "codex",     package: "@openai/codex",                    version: process.env.CODEX_VERSION     ?? "latest", binary: "codex",     enabled: process.env.WITH_CODEX     !== "0", builtDependency: false },
  { component: "gemini",    package: "@google/gemini-cli",               version: process.env.GEMINI_VERSION    ?? "latest", binary: "gemini",    enabled: process.env.WITH_GEMINI    !== "0", builtDependency: false },
  { component: "jules",     package: "@google/jules",                    version: process.env.JULES_VERSION     ?? "latest", binary: "jules",     enabled: process.env.WITH_JULES     === "1", builtDependency: false },
  { component: "opencode",  package: "opencode-ai",                      version: process.env.OPENCODE_VERSION  ?? "latest", binary: "opencode",  enabled: process.env.WITH_OPENCODE  !== "0", builtDependency: false },
  { component: "copilot",   package: "@github/copilot",                  version: process.env.COPILOT_VERSION   ?? "latest", binary: "copilot",   enabled: process.env.WITH_COPILOT   !== "0", builtDependency: false },
  { component: "pi",        package: "@mariozechner/pi-coding-agent",    version: process.env.PI_VERSION        ?? "latest", binary: "pi",        enabled: process.env.WITH_PI        !== "0", builtDependency: false },
];

if (isNaN(RELEASE_AGE_DAYS) || RELEASE_AGE_DAYS < 0) {
  console.error("PNPM_CLI_MIN_RELEASE_AGE_DAYS must be a non-negative integer");
  process.exit(1);
}

fs.mkdirSync(BIN_DIR, { recursive: true });
fs.mkdirSync(INSTALL_ROOT, { recursive: true });
fs.mkdirSync(METADATA_ROOT, { recursive: true });
fs.writeFileSync(SUMMARY_FILE, "");
fs.writeFileSync(JSONL_FILE, "");

if (RELEASE_AGE_DAYS > 0) {
  console.log(`Applying release-age policy: ${RELEASE_AGE_DAYS} days (${RELEASE_AGE_MINUTES} minutes) via pnpm minimumReleaseAge`);
} else {
  console.log("Applying release-age policy: disabled");
}

async function fetchRegistryData(packageName) {
  const url = `https://registry.npmjs.org/${encodeURIComponent(packageName).replace("%40", "@")}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Registry fetch failed for ${packageName}: ${res.status}`);
  return res.json();
}

function resolveVersion(registryData, versionSpec, releaseAgeDays) {
  const times = registryData.time ?? {};
  const versions = registryData.versions ?? {};
  const cutoffEpoch = releaseAgeDays > 0 ? NOW - releaseAgeDays * 86400 * 1000 : null;
  const stablePattern = /^\d+\.\d+\.\d+$/;

  if (versionSpec !== "latest") {
    return versionSpec;
  }

  let selected = null;
  let selectedEpoch = null;

  for (const version of Object.keys(versions)) {
    if (!stablePattern.test(version)) continue;
    const publishedAt = times[version];
    if (!publishedAt) continue;
    const epoch = Date.parse(publishedAt);
    if (!Number.isFinite(epoch)) continue;
    if (cutoffEpoch !== null && epoch > cutoffEpoch) continue;
    if (selectedEpoch === null || epoch > selectedEpoch) {
      selected = version;
      selectedEpoch = epoch;
    }
  }

  return selected;
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

function buildWorkspaceYaml(releaseAgeMinutes, ignoreCves, onlyBuiltDependencies = []) {
  const lines = ["packages: []"];
  if (releaseAgeMinutes > 0) {
    lines.push(`minimumReleaseAge: ${releaseAgeMinutes}`);
  }
  // pnpm 11 requires onlyBuiltDependencies in pnpm-workspace.yaml, not just package.json
  if (onlyBuiltDependencies.length > 0) {
    lines.push("onlyBuiltDependencies:");
    for (const dep of onlyBuiltDependencies) {
      lines.push(`  - "${dep}"`);
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

async function installCli({ component, package: packageName, version: versionSpec, binary, enabled, builtDependency }) {
  if (!enabled) return;

  const forceFix = AUDIT_FORCE_FIX_COMPONENTS.includes(component);
  const installDir = path.join(INSTALL_ROOT, component);

  fs.rmSync(installDir, { recursive: true, force: true });
  fs.mkdirSync(installDir, { recursive: true });

  const pkgJson = {
    name: `ccd-pnpm-cli-${component}`,
    private: true,
    packageManager: `pnpm@${PNPM_VERSION}`,
    // Declare dependency upfront so pnpm install (not pnpm add) is used.
    // pnpm 11 blocks lifecycle scripts for packages added via `pnpm add` even when
    // onlyBuiltDependencies is set — pnpm install respects it correctly.
    dependencies: { [packageName]: versionSpec },
  };
  // pnpm v9+ skips lifecycle scripts by default (supply-chain hardening).
  // Packages that download a platform-native binary in their postinstall must be
  // explicitly allowlisted via onlyBuiltDependencies or the binary is never placed.
  if (builtDependency) {
    pkgJson.pnpm = { onlyBuiltDependencies: [packageName] };
  }
  fs.writeFileSync(path.join(installDir, "package.json"), JSON.stringify(pkgJson, null, 2) + "\n");
  // Large platform-specific binaries (e.g. claude-code-linux-x64) can exceed pnpm's
  // default 60s fetch timeout — set a generous limit per install directory.
  fs.writeFileSync(path.join(installDir, ".npmrc"), "fetch-timeout=600000\n");

  const releaseAgeMinutes = MIN_RELEASE_AGE_IGNORE_COMPONENTS.includes(component) ? 0 : RELEASE_AGE_MINUTES;
  if (releaseAgeMinutes === 0 && RELEASE_AGE_MINUTES > 0) {
    console.log(`[${component}] release-age policy disabled (exempted component)`);
  }
  fs.writeFileSync(
    path.join(installDir, "pnpm-workspace.yaml"),
    buildWorkspaceYaml(releaseAgeMinutes, AUDIT_IGNORE_CVES, builtDependency ? [packageName] : [])
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

  const registryData = await fetchRegistryData(packageName);
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
