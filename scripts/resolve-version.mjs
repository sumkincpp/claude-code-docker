#!/usr/bin/env node
// Usage: resolve-version.mjs <package> <version-spec> <cooldown-days>
// Prints the resolved version to stdout.
// If version-spec is "latest", picks the newest stable release older than cooldown-days.
// Otherwise, prints version-spec unchanged (fixed version passthrough).

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

const url = `https://registry.npmjs.org/${encodeURIComponent(packageName).replace("%40", "@")}`;
const res = await fetch(url);
if (!res.ok) {
  console.error(`Registry fetch failed for ${packageName}: ${res.status}`);
  process.exit(1);
}
const data = await res.json();

const times = data.time ?? {};
const versions = data.versions ?? {};
const cutoff = cooldownDays > 0 ? Date.now() - cooldownDays * 86400 * 1000 : null;
const stablePattern = /^\d+\.\d+\.\d+$/;

let selected = null;
let selectedEpoch = null;

for (const version of Object.keys(versions)) {
  if (!stablePattern.test(version)) continue;
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

if (!selected) {
  console.error(`No stable version of ${packageName} older than ${cooldownDays} days found`);
  process.exit(1);
}

console.error(`[resolve-version] ${packageName}: latest=${data["dist-tags"]?.latest}, resolved=${selected} (cooldown=${cooldownDays}d)`);
process.stdout.write(selected + "\n");
