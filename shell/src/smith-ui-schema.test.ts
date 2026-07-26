import assert from "node:assert/strict";
import { mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { parseSmithUiPayload } from "./smith-ui-schema.js";

const headingSpec = {
  root: "summary",
  elements: {
    summary: {
      type: "Heading",
      props: { text: "Deployment", level: "h1" },
      children: [],
    },
  },
};

test("smith-ui payload parser keeps only a bounded declarative component tree", () => {
  assert.deepEqual(parseSmithUiPayload({ version: 1, spec: headingSpec, images: [] }), {
    version: 1,
    spec: headingSpec,
    images: [],
  });
});

test("smith-ui payload parser rejects remote image sources and non-presentation components", () => {
  assert.equal(
    parseSmithUiPayload({
      version: 1,
      spec: {
        root: "input",
        elements: { input: { type: "TextInput", props: {}, children: [] } },
      },
      images: [],
    }),
    null,
  );
  assert.equal(
    parseSmithUiPayload({
      version: 1,
      spec: headingSpec,
      images: [{ path: "https://example.test/chart.png", alt: "chart" }],
    }),
    null,
  );
  assert.equal(
    parseSmithUiPayload({
      version: 1,
      spec: {
        root: "link",
        elements: { link: { type: "Link", props: { url: "https://example.test" }, children: [] } },
      },
      images: [],
    }),
    null,
  );
});

test("smith-ui payload parser rejects an image larger than five MiB", () => {
  const projectRoot = mkdtempSync(path.join(tmpdir(), "smith-ui-image-"));
  const previousProjectRoot = process.env.SMITH_PROJECT_CWD;
  process.env.SMITH_PROJECT_CWD = projectRoot;
  writeFileSync(path.join(projectRoot, "large.png"), Buffer.alloc(5 * 1024 * 1024 + 1));

  try {
    assert.equal(
      parseSmithUiPayload({
        version: 1,
        spec: headingSpec,
        images: [{ path: "large.png", alt: "large chart" }],
      }),
      null,
    );
  } finally {
    if (previousProjectRoot === undefined) delete process.env.SMITH_PROJECT_CWD;
    else process.env.SMITH_PROJECT_CWD = previousProjectRoot;
    rmSync(projectRoot, { recursive: true, force: true });
  }
});

test("smith-ui payload parser rejects an image header with an unsafe decoded size", () => {
  const projectRoot = mkdtempSync(path.join(tmpdir(), "smith-ui-image-"));
  const previousProjectRoot = process.env.SMITH_PROJECT_CWD;
  process.env.SMITH_PROJECT_CWD = projectRoot;
  const pngHeader = Buffer.alloc(24);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(pngHeader);
  pngHeader.write("IHDR", 12, "ascii");
  pngHeader.writeUInt32BE(10_000, 16);
  pngHeader.writeUInt32BE(10_000, 20);
  writeFileSync(path.join(projectRoot, "bomb.png"), pngHeader);

  try {
    assert.equal(
      parseSmithUiPayload({
        version: 1,
        spec: headingSpec,
        images: [{ path: "bomb.png", alt: "unsafe chart" }],
      }),
      null,
    );
  } finally {
    if (previousProjectRoot === undefined) delete process.env.SMITH_PROJECT_CWD;
    else process.env.SMITH_PROJECT_CWD = previousProjectRoot;
    rmSync(projectRoot, { recursive: true, force: true });
  }
});

test("smith-ui payload parser rejects a project path that symlinks outside the project", () => {
  const projectRoot = mkdtempSync(path.join(tmpdir(), "smith-ui-project-"));
  const externalRoot = mkdtempSync(path.join(tmpdir(), "smith-ui-external-"));
  const previousProjectRoot = process.env.SMITH_PROJECT_CWD;
  process.env.SMITH_PROJECT_CWD = projectRoot;
  const pngHeader = Buffer.alloc(24);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(pngHeader);
  pngHeader.write("IHDR", 12, "ascii");
  pngHeader.writeUInt32BE(1, 16);
  pngHeader.writeUInt32BE(1, 20);
  const externalImage = path.join(externalRoot, "outside.png");
  writeFileSync(externalImage, pngHeader);
  symlinkSync(externalImage, path.join(projectRoot, "linked.png"));

  try {
    assert.equal(
      parseSmithUiPayload({
        version: 1,
        spec: headingSpec,
        images: [{ path: "linked.png", alt: "outside chart" }],
      }),
      null,
    );
  } finally {
    if (previousProjectRoot === undefined) delete process.env.SMITH_PROJECT_CWD;
    else process.env.SMITH_PROJECT_CWD = previousProjectRoot;
    rmSync(projectRoot, { recursive: true, force: true });
    rmSync(externalRoot, { recursive: true, force: true });
  }
});

test("smith-ui payload parser accepts the compact header of a safe VP8L image", () => {
  const projectRoot = mkdtempSync(path.join(tmpdir(), "smith-ui-image-"));
  const previousProjectRoot = process.env.SMITH_PROJECT_CWD;
  process.env.SMITH_PROJECT_CWD = projectRoot;
  const webpHeader = Buffer.alloc(25);
  webpHeader.write("RIFF", 0, "ascii");
  webpHeader.write("WEBP", 8, "ascii");
  webpHeader.write("VP8L", 12, "ascii");
  webpHeader[20] = 0x2f;
  writeFileSync(path.join(projectRoot, "pixel.webp"), webpHeader);

  try {
    const parsed = parseSmithUiPayload({
      version: 1,
      spec: headingSpec,
      images: [{ path: "pixel.webp", alt: "one pixel" }],
    });

    assert.equal(parsed?.images[0]?.alt, "one pixel");
  } finally {
    if (previousProjectRoot === undefined) delete process.env.SMITH_PROJECT_CWD;
    else process.env.SMITH_PROJECT_CWD = previousProjectRoot;
    rmSync(projectRoot, { recursive: true, force: true });
  }
});
