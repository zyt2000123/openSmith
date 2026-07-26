import assert from "node:assert/strict";
import test from "node:test";

import type { LlmConfig } from "./api.js";
import { buildLlmConfigInput, createSetupDraft, INITIAL_SETUP_FIELDS } from "./setup.js";
import type { SetupDraft } from "./store.js";

function draft(overrides: Partial<SetupDraft> = {}): SetupDraft {
  return {
    vendor: "OpenAI",
    provider: "openai",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4.1-mini",
    review_model: "",
    max_output_tokens: "",
    api_key: "",
    routes: "",
    models: "",
    interactive_api_key: "",
    gate_api_key: "",
    background_api_key: "",
    timeout_profiles: "",
    ...overrides,
  };
}

test("initial setup separates the supplier name from the compatible protocol", () => {
  assert.deepEqual(
    [...INITIAL_SETUP_FIELDS],
    ["vendor", "provider", "base_url", "api_key", "model", "review_model", "save"],
  );
});

test("setup draft restores the essential values from saved config", () => {
  const config: LlmConfig = {
    configured: true,
    has_api_key: true,
    vendor: "OpenAI",
    provider: "openai",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4.1-mini",
    max_output_tokens: 2048,
    routes: {
      gate: { model: "cheap-gate-model", max_output_tokens: 512, timeout_profile: "gate", has_api_key: true },
    },
    timeout_profiles: {
      gate: { read: 45, stream_read: 50 },
    },
    models: {},
  };

  const result = createSetupDraft(config);

  assert.deepEqual(JSON.parse(result.routes), {
    gate: { model: "cheap-gate-model", max_output_tokens: 512, timeout_profile: "gate" },
  });
  assert.equal(result.max_output_tokens, "2048");
  assert.equal(result.vendor, "OpenAI");
  assert.equal(result.routes.includes("api_key"), false);
  assert.equal(result.review_model, "cheap-gate-model");
  assert.deepEqual(JSON.parse(result.timeout_profiles), { gate: { read: 45, stream_read: 50 } });
});

test("setup saves only essential fields and clears legacy advanced settings", () => {
  const result = buildLlmConfigInput(draft({ review_model: "review-model" }));

  assert.deepEqual(result, {
    vendor: "OpenAI",
    provider: "openai",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4.1-mini",
    routes: { gate: { model: "review-model" } },
    timeout_profiles: {},
  });
});

test("setup clears the review route when no review model is configured", () => {
  const config: LlmConfig = {
    configured: true,
    has_api_key: true,
    vendor: "OpenAI",
    provider: "openai",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4.1-mini",
    max_output_tokens: null,
    routes: { gate: { model: "old-review-model", has_api_key: false } },
    timeout_profiles: {},
    models: {},
  };
  const setupDraft = createSetupDraft(config);
  setupDraft.review_model = "";

  const result = buildLlmConfigInput(setupDraft);

  assert.deepEqual(result.routes, { gate: { model: null } });
  assert.deepEqual(result.timeout_profiles, {});
});

test("setup preserves a route whose only stored value is its API key", () => {
  const config: LlmConfig = {
    configured: true,
    has_api_key: true,
    vendor: "OpenAI",
    provider: "openai",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4.1-mini",
    max_output_tokens: null,
    routes: { gate: { has_api_key: true } },
    timeout_profiles: {},
    models: {},
  };

  const setupDraft = createSetupDraft(config);
  const result = buildLlmConfigInput(setupDraft);

  assert.deepEqual(JSON.parse(setupDraft.routes), { gate: {} });
  assert.deepEqual(result.routes, { gate: {} });
});

test("setup does not echo the interactive model into the route override editor", () => {
  const config: LlmConfig = {
    configured: true,
    has_api_key: true,
    vendor: "OpenAI",
    provider: "openai",
    base_url: "https://api.openai.com/v1",
    model: "route-model",
    max_output_tokens: null,
    routes: { interactive: { model: "route-model", timeout_profile: "interactive", has_api_key: false } },
    timeout_profiles: {},
    models: {},
  };

  const setupDraft = createSetupDraft(config);
  const result = buildLlmConfigInput({ ...setupDraft, model: "chosen-model" });

  assert.deepEqual(JSON.parse(setupDraft.routes), { interactive: { timeout_profile: "interactive" } });
  assert.equal(setupDraft.model, "route-model");
  assert.equal(result.model, "chosen-model");
  assert.deepEqual(result.routes, { interactive: { timeout_profile: "interactive" } });
});

test("setup rejects a provider that only exists on Object.prototype", () => {
  for (const provider of ["constructor", "toString", "valueOf"]) {
    assert.throws(() => buildLlmConfigInput(draft({ provider })), /Compatible protocol/);
  }
});
