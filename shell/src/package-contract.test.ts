import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

type PackageManifest = {
  engines?: { node?: string };
  files?: string[];
  scripts: Record<string, string>;
  dependencies: Record<string, string>;
};

test("the published Shell contract matches its runtime and generated artifacts", () => {
  const manifest = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as PackageManifest;

  assert.equal(manifest.engines?.node, ">=22");
  assert.deepEqual(manifest.files, ["bin", "dist"]);
  assert.match(manifest.scripts.build, /^npm run clean && /);
  assert.equal(manifest.dependencies["ink-text-input"], undefined);
});
