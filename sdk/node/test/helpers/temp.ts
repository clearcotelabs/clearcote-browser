// Temp directories for tests, removed when the test file finishes. A suite run must leave nothing in the
// machine's temp directory: before this, every `npm test` left ~30 directories behind (20 from
// license-per-browser alone), on developer machines and CI runners alike.
import { afterAll } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const created: string[] = [];

/** `mkdtemp` under the OS temp directory; removed after the current test file. */
export function tempDir(prefix: string): string {
  return removeAfterFile(mkdtempSync(join(tmpdir(), prefix)));
}

/** Remove `path` (file or directory) after the current test file — for paths the code under test made. */
export function removeAfterFile(path: string): string {
  created.push(path);
  return path;
}

afterAll(() => {
  for (const p of created.splice(0)) rmSync(p, { recursive: true, force: true, maxRetries: 5 });
});
