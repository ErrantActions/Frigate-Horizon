import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";
import { test, expect } from "../../fixtures/frigate-test";
import { configFactory } from "../../fixtures/mock-data/config";

const SETTINGS = "/settings?page=cameraOnvif&camera=front_door";
const schema = JSON.parse(
  readFileSync(
    resolve(
      dirname(fileURLToPath(import.meta.url)),
      "../../fixtures/mock-data/config-schema.json",
    ),
    "utf-8",
  ),
);

async function install(page: Page) {
  const config = configFactory({
    cameras: { front_door: { onvif: { host: "camera.invalid" } } },
  });
  for (const path of ["**/api/config", "**/api/config/raw_paths"]) {
    await page.route(path, (route) => route.fulfill({ json: config }));
  }
  await page.route("**/api/config/schema.json", (route) =>
    route.fulfill({ json: schema }),
  );
}

test.describe("ONVIF capability-driven controls @mobile", () => {
  test("reported H.265 stays selected and H.264 is only an advertised user choice, never an automatic write", async ({ frigateApp }) => {
    await install(frigateApp.page);
    let writes = 0;
    const options = {
      resolutions: [{ width: 3840, height: 2160 }], frame_rate: null,
      bitrate_kbps: { minimum: 512, maximum: 8192 },
      quality: { minimum: 1, maximum: 6 },
      gop_length: { minimum: 1, maximum: 120 }, encoding_interval: null,
    };
    await frigateApp.page.route("**/api/onvif/cameras/front_door/encoder/media2*", (route) => {
      if (route.request().method() !== "GET") writes++;
      const profile = new URL(route.request().url()).searchParams.get("profile_token");
      return route.fulfill({ json: {
        profiles: [{ token: "main2", name: "Main H265", encoder_token: "video2", video_source_token: "sensor" }],
        profile_token: profile,
        encoder: profile ? {
          token: "video2", encoding: "H265", codec_profile: "Main", revision: "a".repeat(64),
          editable: true, service: "media2", frame_rates: [10, 15],
          persistence_required: true, shared_profiles: ["main2"],
          resolution: { width: 3840, height: 2160 }, frame_rate: 10,
          bitrate_kbps: 4096, quality: 4, gop_length: 30, encoding_interval: null,
          options, codec_options: { H265: options, H264: options },
          codec_frame_rates: { H265: [10, 15], H264: [10, 15] },
          codec_profiles: { H265: ["Main"], H264: ["Main", "High"] },
        } : null,
      } });
    });
    await frigateApp.goto(SETTINGS);
    const panel = frigateApp.page.locator("section").filter({ has: frigateApp.page.getByRole("heading", { name: "Camera video encoder", exact: true }) });
    await expect(panel).toBeVisible({ timeout: 15000 });
    await panel.getByRole("combobox", { name: "Explicit ONVIF media service", exact: true }).selectOption("media2");
    await panel.getByRole("button", { name: "Read encoder media profiles", exact: true }).click();
    await panel.getByRole("combobox", { name: "Encoder media profile", exact: true }).selectOption("main2");
    const codec = panel.getByRole("combobox", { name: "Advertised video codec", exact: true });
    await expect(codec).toHaveValue("H265");
    expect(await codec.locator("option").allTextContents()).toEqual(["H265", "H264"]);
    expect(writes).toBe(0);
  });

  test("audio options are explicit, reported, and require consent before one temporary write", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    let reads = 0;
    let writes = 0;
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/audio-encoder*",
      async (route) => {
        const profile =
          new URL(route.request().url()).searchParams.get("profile_token") ??
          (route.request().method() === "PUT" ? "main" : null);
        const response = {
          profiles: [
            {
              token: "main",
              name: "Main",
              encoder_token: "audio",
              audio_source_token: "mic",
            },
          ],
          profile_token: profile,
          state: profile ? "ready" : "choose_profile",
          encoder: profile
            ? {
                token: "audio",
                audio_source_token: "mic",
                encoding: "AAC",
                bitrate_kbps: 64,
                sample_rate_khz: 16,
                options: [
                  {
                    encoding: "AAC",
                    bitrates_kbps: [64, 128],
                    sample_rates_khz: [16],
                  },
                ],
                shared_profiles: ["main", "sub"],
                editable: true,
                revision: "a".repeat(64),
              }
            : null,
        };
        if (route.request().method() === "PUT") {
          writes++;
          expect(route.request().postDataJSON()).toMatchObject({
            service: "media",
            profile_token: "main",
            changes: { bitrate_kbps: 128 },
            expected_revision: "a".repeat(64),
            confirm_device_change: true,
            force_persistence: false,
          });
          response.profile_token = "main";
          response.state = "ready";
          response.encoder = { ...response.encoder!, bitrate_kbps: 128 };
        } else reads++;
        await route.fulfill({ json: response });
      },
    );
    await frigateApp.goto(SETTINGS);
    const panel = frigateApp.page.locator("section").filter({
      has: frigateApp.page.getByRole("heading", {
        name: "Camera audio encoder",
        exact: true,
      }),
    });
    await expect(panel).toBeVisible({ timeout: 15000 });
    expect(reads).toBe(0);
    await panel
      .getByRole("button", { name: "Read audio encoder profiles", exact: true })
      .click();
    await panel
      .getByLabel("Audio encoder media profile", { exact: true })
      .selectOption("main");
    await expect(
      panel.getByLabel("Audio sample rate (kHz)", { exact: true }),
    ).toHaveValue("16");
    expect(
      await panel
        .getByLabel("Audio sample rate (kHz)", { exact: true })
        .locator("option")
        .allTextContents(),
    ).not.toContain("48");
    await panel
      .getByLabel("Audio bitrate (kbps)", { exact: true })
      .selectOption("128");
    const apply = panel.getByRole("button", {
      name: "Apply confirmed audio encoder changes",
      exact: true,
    });
    await expect(apply).toBeDisabled();
    await panel
      .getByLabel("I authorize one physical audio encoder change", {
        exact: false,
      })
      .check();
    await apply.click();
    await expect.poll(() => writes).toBe(1);
  });

  test("fixed-camera focus absence is terminal, not a PTZ failure or a spinner", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    let reads = 0;
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/focus*",
      (route) => {
        reads++;
        return route.fulfill({
          json: {
            profiles: [
              { token: "main", name: "Main", video_source_token: "sensor" },
            ],
            profile_token: "main",
            state: "unsupported",
            focus: null,
          },
        });
      },
    );
    await frigateApp.goto(SETTINGS);
    const load = frigateApp.page.getByRole("button", {
      name: "Read reported focus controls",
      exact: true,
    });
    await expect(load).toBeVisible({ timeout: 15000 });
    expect(reads).toBe(0);
    await load.click();
    await expect(
      frigateApp.page.getByText(
        "The camera did not advertise absolute focus movement.",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(load).toBeEnabled();
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Apply confirmed focus move",
        exact: true,
      }),
    ).toHaveCount(0);
  });

  test("media profile discovery does not require PTZ or fabricate an audio source", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/profiles/media",
      (route) =>
        route.fulfill({
          json: {
            state: "ready",
            service: "media",
            profiles: [
              {
                token: "main",
                name: "Fixed Lens",
                video_source_token: "sensor",
                video_encoder_token: "video",
                audio_source_token: null,
                audio_encoder_token: null,
                ptz_configuration_token: null,
              },
            ],
          },
        }),
    );
    await frigateApp.goto(SETTINGS);
    await frigateApp.page
      .getByRole("button", { name: "Read media profile bindings", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText("Fixed Lens (main)", { exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("PTZ configuration: Not advertised", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("Audio source: Not advertised", {
        exact: true,
      }),
    ).toBeVisible();
  });

  test("advanced catalog distinguishes client bindings, schema operations, and typed writes", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    let catalogs = 0;
    let operations = 0;
    const requestSchema = {
      type: "object",
      properties: { Name: { type: "string" } },
      required: ["Name"],
      additionalProperties: false,
    };
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/administration",
      (route) => {
        catalogs++;
        return route.fulfill({
          json: {
            revision: "a".repeat(64),
            complete: false,
            services: [
              {
                namespace: "http://www.onvif.org/ver10/device/wsdl",
                service: "devicemgmt",
                advertised: true,
                binding: "available",
                operations: [
                  {
                    name: "SetHostname",
                    effect: "mutating",
                    typed_confirmation: true,
                    schema: requestSchema,
                    readiness: "ready",
                    device_support: "unverified",
                    binary_input: false,
                    binary_output: false,
                  },
                  {
                    name: "GetSystemBackup",
                    effect: "read",
                    typed_confirmation: false,
                    schema: { type: "object", properties: {} },
                    readiness: "file_transfer_required",
                    device_support: "unverified",
                    binary_input: false,
                    binary_output: true,
                  },
                ],
              },
              {
                namespace: "http://www.onvif.org/ver20/new-service/wsdl",
                service: null,
                advertised: true,
                binding: "unavailable",
                operations: [],
              },
            ],
          },
        });
      },
    );
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/administration/devicemgmt/SetHostname",
      (route) => {
        operations++;
        expect(route.request().postDataJSON()).toMatchObject({
          catalog_revision: "a".repeat(64),
          parameters: { Name: "reviewed-hostname" },
          confirm_device_change: true,
          typed_confirmation: "SetHostname",
        });
        return route.fulfill({
          json: { state: "acknowledged_unverified", result: null },
        });
      },
    );
    await frigateApp.goto(SETTINGS);
    const panel = frigateApp.page.locator("section").filter({
      has: frigateApp.page.getByRole("heading", {
        name: "Advanced ONVIF device administration",
        exact: true,
      }),
    });
    await expect(panel).toBeVisible({ timeout: 15000 });
    expect(catalogs).toBe(0);
    await panel
      .getByRole("button", {
        name: "Read advertised service and operation catalog",
        exact: true,
      })
      .click();
    await expect(
      panel.getByText(
        "This is a client limitation, not a device-unsupported result.",
        { exact: false },
      ),
    ).toBeVisible();
    const choose = panel.getByLabel("Schema-defined service operation", {
      exact: true,
    });
    await choose.selectOption({
      label: "devicemgmt: GetSystemBackup (Explicit read)",
    });
    await expect(
      panel.getByText(
        "This operation requires protected binary upload or download handling.",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(
      panel.getByRole("button", {
        name: "Read this operation once",
        exact: true,
      }),
    ).toHaveCount(0);
    await choose.selectOption({
      label: "devicemgmt: SetHostname (Device change)",
    });
    await panel
      .getByRole("textbox", { name: /^Name/ })
      .fill("reviewed-hostname");
    const apply = panel.getByRole("button", {
      name: "Submit confirmed device operation once",
      exact: true,
    });
    await expect(apply).toBeDisabled();
    await panel
      .getByLabel("I reviewed these WSDL fields", { exact: false })
      .check();
    await expect(apply).toBeDisabled();
    await panel
      .getByLabel("Type SetHostname exactly to confirm", { exact: true })
      .fill("SetHostname");
    await apply.click();
    await expect(
      panel.getByText(
        "The camera acknowledged this operation, but its outcome has not been independently verified.",
        { exact: false },
      ),
    ).toBeVisible();
    expect(operations).toBe(1);
  });
});
