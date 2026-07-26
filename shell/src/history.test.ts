import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { loadHistory, saveHistory } from "./history.js";

test("the latest history save is immediately durable and wins over earlier saves", () => {
  const directory = mkdtempSync(path.join(tmpdir(), "smith-history-"));
  const file = path.join(directory, "shell_history.json");

  try {
    saveHistory(["first"], file);
    saveHistory(["second"], file);

    assert.deepEqual(loadHistory(file), ["second"]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
