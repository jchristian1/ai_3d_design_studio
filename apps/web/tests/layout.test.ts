/**
 * Layout rules that, when broken, produce a blank screen rather than a test failure.
 *
 * jsdom has no layout engine, so these read the stylesheets. That is a blunt instrument and
 * deliberately narrow: each assertion exists because breaking it cost real time.
 *
 * The one that prompted this: the 3D panel's children are all absolutely positioned, so it
 * has no intrinsic width. As a plain flex item it collapsed to zero, and a perfectly good
 * model loaded into a 0-pixel canvas. The API was right, the GLB was right, the loader was
 * right, and the screen was empty.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

function stylesheet(name: string): string {
  return readFileSync(new URL(`../components/${name}`, import.meta.url), "utf8");
}

/** The declarations inside one class rule, with whitespace collapsed. */
function rule(css: string, selector: string): string {
  const index = css.indexOf(`${selector} {`);
  assert.notEqual(index, -1, `no rule for ${selector}`);
  const start = css.indexOf("{", index) + 1;
  const end = css.indexOf("}", start);
  return css.slice(start, end).replace(/\s+/g, " ").trim();
}

describe("the layout cannot collapse the 3D view", () => {
  it("the model panel states both of its dimensions", () => {
    // Everything inside it is position:absolute, so it has no intrinsic size. Without
    // this it depends on the parent's layout mode, which is how it reached zero width.
    const panel = rule(stylesheet("ModelViewer.module.css"), ".panel");
    assert.match(panel, /width:\s*100%/);
    assert.match(panel, /height:\s*100%/);
  });

  it("the stage stretches its child in both axes", () => {
    const stage = rule(stylesheet("workspace.module.css"), ".stage");
    assert.match(stage, /display:\s*grid/);
    assert.match(stage, /grid-template-columns:\s*minmax\(0, 1fr\)/);
    assert.match(stage, /grid-template-rows:\s*minmax\(0, 1fr\)/);
  });

  it("every scrolling region can shrink below its content", () => {
    // A grid child without min-height:0 refuses to shrink, so the whole page scrolls
    // instead of the panel — which is what makes a chat column feel broken.
    const css = stylesheet("workspace.module.css");
    for (const selector of [
      ".shell",
      ".body",
      ".chatSlot",
      ".chatColumn",
      ".stage",
      ".inspectorSlot",
      ".inspector",
      ".panelScroll",
      ".conversation",
    ]) {
      assert.match(
        rule(css, selector),
        /min-height:\s*0|overflow-y:\s*auto|overflow:\s*hidden|overflow:\s*auto/,
        `${selector} must be able to shrink or scroll`,
      );
    }
  });

  it("the three columns are a grid, with the chat first", () => {
    const css = stylesheet("workspace.module.css");
    const body = rule(css, ".body");
    assert.match(body, /display:\s*grid/);
    // chat | model | inspector — the chat column is the FIRST track, on the left.
    const columns = body.match(/grid-template-columns:\s*([^;]+);/)?.[1] ?? "";
    assert.match(columns, /^minmax\(20rem, 25rem\)/, columns);
    assert.match(columns, /minmax\(0, 1fr\)/);
  });

  it("collapsing a side column gives its space to the model", () => {
    const css = stylesheet("workspace.module.css");
    assert.match(rule(css, ".noChat .body"), /grid-template-columns:\s*minmax\(0, 1fr\)/);
    assert.match(
      rule(css, ".noInspector .body"),
      /grid-template-columns:\s*minmax\(20rem, 25rem\) minmax\(0, 1fr\)/,
    );
    assert.match(rule(css, ".noChat.noInspector .body"), /minmax\(0, 1fr\)/);
  });

  it("the composer stays at the bottom of the chat column", () => {
    const column = rule(stylesheet("workspace.module.css"), ".chatColumn");
    // files | transcript (the only flexible row) | composer
    assert.match(column, /grid-template-rows:\s*auto minmax\(0, 1fr\) auto/);
  });
});



describe("the viewer sizes itself even if the CSS lets it down", () => {
  it("takes its size from an ancestor when its own box is empty", async () => {
    // The safeguard that makes a blank canvas impossible from CSS alone. jsdom reports 0
    // for every element, so the sizes are set explicitly here — which is the situation the
    // helper exists for.
    const globalJsdom = (await import("global-jsdom")).default;
    const cleanup = globalJsdom(undefined, { pretendToBeVisual: true });
    try {
      const { measureForTest } = await import("../components/ModelViewer.tsx");

      const outer = document.createElement("div");
      const inner = document.createElement("div");
      outer.appendChild(inner);
      document.body.appendChild(outer);

      define(outer, 1200, 800);
      define(inner, 0, 0);
      assert.deepEqual(measureForTest(inner), { width: 1200, height: 800 });

      // Nothing has a size at all: a sane default, never zero.
      const orphan = document.createElement("div");
      define(orphan, 0, 0);
      assert.deepEqual(measureForTest(orphan), { width: 800, height: 600 });
    } finally {
      cleanup();
    }
  });
});

/** jsdom has no layout, so a test states the sizes it wants an element to report. */
function define(element: HTMLElement, width: number, height: number): void {
  Object.defineProperty(element, "clientWidth", { value: width, configurable: true });
  Object.defineProperty(element, "clientHeight", { value: height, configurable: true });
}
