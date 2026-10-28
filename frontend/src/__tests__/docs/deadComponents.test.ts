import fs from "node:fs";
import path from "node:path";

/**
 * F-12 — a component that nothing imports is a lie in the UI, and one that stays in the tree
 * while the docs call it live is worse.
 *
 * Measured defect at HEAD: `AgentStatusGrid.tsx`, `MetricsCard.tsx` and `TopbarPopover.tsx` had
 * zero non-test importers; two were kept alive by their own unit specs, and README /
 * `docs/ACCEPTANCE_TESTING.md` still listed them as shipped surfaces.
 *
 * The fix is the durable absence pin: no file under `src/components` may exist without a
 * non-test importer. `AgentStatusGrid` and `MetricsCard` were retired with their specs; the one
 * survivor is allowlisted BY NAME with the request that owns its retirement, so the next dead
 * file fails here instead of accumulating.
 */

const ROOT = process.cwd();
const SRC = path.join(ROOT, "src");
const slash = (value: string) => value.split(path.sep).join("/");
const rel = (value: string) => slash(path.relative(ROOT, value));

/** Dead files that are NAMED here, with the request that owns them. Nothing else is allowed. */
const ALLOWED: Record<string, string> = {
  "src/components/layout/TopbarPopover.tsx":
    "outside this seat's territory — REQUESTS.md R2 asks the layout owner to delete it",
};

/** Files that still MENTION a retired component in prose, with the request that owns the edit. */
const PROSE_ALLOWED: Record<string, string> = {
  "src/components/dashboard/format.ts": "REQUESTS.md R3 — outside seat territory",
  "src/hooks/useAgentStatus.ts": "REQUESTS.md R3 — outside seat territory",
};

const RETIRED = ["AgentStatusGrid", "MetricsCard"];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules") continue;
      out.push(...walk(full));
      continue;
    }
    if (/\.(ts|tsx)$/.test(entry.name)) out.push(full);
  }
  return out;
}

const all = walk(SRC);
const sources = all.filter((file) => !slash(file).includes("/__tests__/"));
const components = sources.filter(
  (file) => slash(file).includes("/src/components/") && file.endsWith(".tsx"),
);

function importsOf(file: string): string[] {
  const text = fs.readFileSync(file, "utf8");
  const specs: string[] = [];
  for (const match of text.matchAll(/(?:from|import)\s+["']([^"']+)["']/g)) specs.push(match[1]);
  // A dynamically imported route/component still counts as reachable.
  for (const match of text.matchAll(/import\(\s*["']([^"']+)["']\s*\)/g)) specs.push(match[1]);
  return specs;
}

function importersOf(target: string): string[] {
  const bare = slash(target).replace(/\.tsx?$/, "");
  const base = path.basename(bare);
  return sources.filter((file) => {
    if (file === target) return false;
    return importsOf(file).some((spec) => {
      if (spec.startsWith(".")) {
        return slash(path.resolve(path.dirname(file), spec)) === bare;
      }
      const alias = `@/${slash(path.relative(SRC, bare))}`;
      return spec === alias || (spec.endsWith(`/${base}`) && spec.startsWith("@/"));
    });
  });
}

describe("F-12 — no component file is unreachable from the app tree", () => {
  it("every component under src/components has a non-test importer (or is named below)", () => {
    const dead = components
      .filter((file) => importersOf(file).length === 0)
      .map(rel)
      .filter((file) => !(file in ALLOWED));

    expect(dead).toEqual([]);
  });

  it("every allowlisted survivor states the request that owns its retirement", () => {
    for (const [file, reason] of Object.entries(ALLOWED)) {
      expect(fs.existsSync(path.join(ROOT, file))).toBe(true);
      expect(reason).toMatch(/REQUESTS\.md R\d/);
      // Its own header must already tell the truth about being unmounted.
      const source = fs.readFileSync(path.join(ROOT, file), "utf8");
      expect(source).toMatch(/not mounted|imported by nothing|zero non-test imports/i);
    }
  });

  it("the two retired components are gone from the tree and from the shipped source", () => {
    for (const name of RETIRED) {
      expect(fs.existsSync(path.join(ROOT, `src/components/dashboard/${name}.tsx`))).toBe(false);
      expect(fs.existsSync(path.join(ROOT, `src/__tests__/components/${name}.test.tsx`))).toBe(
        false,
      );
    }

    const mentions = sources
      .filter((file) => RETIRED.some((name) => fs.readFileSync(file, "utf8").includes(name)))
      .map(rel)
      .filter((file) => !(file in PROSE_ALLOWED));
    expect(mentions).toEqual([]);
  });
});
