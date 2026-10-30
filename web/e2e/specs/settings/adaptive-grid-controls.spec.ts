import { readFileSync } from "node:fs";
import { test, expect } from "../../fixtures/frigate-test";

const schema = JSON.parse(
  readFileSync(
    new URL("../../fixtures/mock-data/config-schema.json", import.meta.url),
    "utf-8",
  ),
);
schema.$defs.AdaptiveMotionConfig = {
  type: "object",
  properties: {
    enabled: { type: "boolean", default: false, title: "Adaptive motion" },
    mode: {
      type: "string",
      enum: ["shadow", "active"],
      default: "shadow",
      title: "Adaptive mode",
    },
    grid_height: {
      type: "integer",
      minimum: 4,
      maximum: 500,
      default: 500,
      title: "Grid squares vertically",
    },
    short_edge: {
      type: "integer",
      minimum: 4,
      maximum: 500,
      default: 500,
      title: "Grid cells on shorter edge",
      readOnly: true,
    },
    columns: {
      type: "integer",
      default: 500,
      title: "Grid columns",
      readOnly: true,
    },
    rows: { type: "integer", default: 500, title: "Grid rows", readOnly: true },
  },
};
schema.$defs.MotionConfig.properties.adaptive = {
  $ref: "#/$defs/AdaptiveMotionConfig",
  title: "Adaptive motion grid",
};

test.describe("Adaptive grid controls @mobile", () => {
  test("only asks for vertical grid squares, never computed dimensions or pixel height", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({
      configSchema: schema,
      configRaw:
        "mqtt:\n  enabled: false\ncameras:\n  front_door:\n    motion:\n      adaptive:\n        enabled: true\n        grid_height: 500\n",
    });
    await frigateApp.goto("/settings?page=cameraMotion&camera=front_door");
    await expect(
      frigateApp.page.getByText("Grid squares vertically", { exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("Grid columns", { exact: true }),
    ).toHaveCount(0);
    await expect(
      frigateApp.page.getByText("Grid rows", { exact: true }),
    ).toHaveCount(0);
    await expect(
      frigateApp.page.getByText("Grid cells on shorter edge", { exact: true }),
    ).toHaveCount(0);
    await expect(
      frigateApp.page.getByText("Frame height", { exact: true }),
    ).toHaveCount(0);
    const advanced = frigateApp.page.getByRole("button", {
      name: /^Advanced(?: Settings)? \(/,
    });
    if (await advanced.count()) {
      await advanced.first().click();
      await expect(
        frigateApp.page.getByText("Frame height", { exact: true }),
      ).toHaveCount(0);
      await expect(
        frigateApp.page.getByText("Grid columns", { exact: true }),
      ).toHaveCount(0);
      await expect(
        frigateApp.page.getByText("Grid rows", { exact: true }),
      ).toHaveCount(0);
    }
  });
});
