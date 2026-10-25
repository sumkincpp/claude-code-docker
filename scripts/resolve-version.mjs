#!/usr/bin/env node
// Usage: resolve-version.mjs <package> <version-spec> <cooldown-days>
// Prints the resolved version to stdout.
// If version-spec is "latest", picks the newest stable release older than cooldown-days.
// Otherwise, prints version-spec unchanged (fixed version passthrough).
//
// Also importable: export fetchRegistryData, resolveVersion for use in other scripts.

import { fileURLToPath } from "url";

const STABLE = /^\d+\.\d+\.\d+$/;

export async function fetchRegistryData(packageName) {
  const url = `https://registry.npmjs.org/${encodeURIComponent(packageName).replace("%40", "@")}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Registry fetch failed for ${packageName}: ${res.status}`);
  return res.json();
}

export function resolveVersion(registryData, versionSpec, cooldownDays) {
  if (versionSpec !== "latest") return versionSpec;

  const times = registryData.time ?? {};
  const versions = registryData.versions ?? {};
  const cutoff = cooldownDays > 0 ? Date.now() - cooldownDays * 86400 * 1000 : null;

  let selected = null;
  let selectedEpoch = null;

  for (const version of Object.keys(versions)) {
    if (!STABLE.test(version)) continue;
    const publishedAt = times[version];
    if (!publishedAt) continue;
    const epoch = Date.parse(publishedAt);
    if (!Number.isFinite(epoch)) continue;
    if (cutoff !== null && epoch > cutoff) continue;
    if (selectedEpoch === null || epoch > selectedEpoch) {
      selected = version;
      selectedEpoch = epoch;
    }
  }

  return selected;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [,, packageName, versionSpec = "latest", cooldownDaysStr = "7"] = process.argv;

  if (!packageName) {
    console.error("Usage: resolve-version.mjs <package> <version-spec> <cooldown-days>");
    process.exit(1);
  }

  if (versionSpec !== "latest") {
    console.error(`[resolve-version] ${packageName}: fixed version ${versionSpec}, skipping registry lookup`);
    process.stdout.write(versionSpec + "\n");
    process.exit(0);
  }

  const cooldownDays = parseInt(cooldownDaysStr, 10);
  if (isNaN(cooldownDays) || cooldownDays < 0) {
    console.error("cooldown-days must be a non-negative integer");
    process.exit(1);
  }

  const data = await fetchRegistryData(packageName);
  const selected = resolveVersion(data, versionSpec, cooldownDays);

  if (!selected) {
    console.error(`No stable version of ${packageName} older than ${cooldownDays} days found`);
    process.exit(1);
  }

  console.error(`[resolve-version] ${packageName}: latest=${data["dist-tags"]?.latest}, resolved=${selected} (cooldown=${cooldownDays}d)`);
  process.stdout.write(selected + "\n");
}
