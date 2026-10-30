import { test, expect } from "../../fixtures/frigate-test";

test.describe("Non-empty password policy @mobile", () => {
  test("creates a user with a short password even when its strength is weak", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/settings?page=users");
    await frigateApp.page
      .getByRole("button", { name: "Add User", exact: true })
      .click();
    await frigateApp.page
      .getByLabel("Username", { exact: true })
      .fill("short_user");
    await frigateApp.page.getByLabel("Password", { exact: true }).fill("x");
    await frigateApp.page
      .getByLabel("Confirm Password", { exact: true })
      .fill("x");
    await expect(
      frigateApp.page.getByText("Weak", { exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("At least 12 characters", { exact: true }),
    ).toHaveCount(0);
    const save = frigateApp.page.getByRole("button", {
      name: "Save",
      exact: true,
    });
    await expect(save).toBeEnabled();
    const request = frigateApp.page.waitForRequest(
      (request) =>
        request.url().endsWith("/api/users") && request.method() === "POST",
    );
    await save.click();
    expect((await request).postDataJSON()).toEqual({
      username: "short_user",
      password: "x",
      role: "viewer",
    });
  });

  test("still rejects empty passwords and mismatched confirmation", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/settings?page=users");
    await frigateApp.page
      .getByRole("button", { name: "Add User", exact: true })
      .click();
    await frigateApp.page
      .getByLabel("Username", { exact: true })
      .fill("short_user");
    const save = frigateApp.page.getByRole("button", {
      name: "Save",
      exact: true,
    });
    await expect(save).toBeDisabled();
    await frigateApp.page.getByLabel("Password", { exact: true }).fill("x");
    await frigateApp.page
      .getByLabel("Confirm Password", { exact: true })
      .fill("y");
    await expect(save).toBeDisabled();
    await frigateApp.page
      .getByLabel("Confirm Password", { exact: true })
      .fill("x");
    await expect(save).toBeEnabled();
    await frigateApp.page.getByLabel("Password", { exact: true }).clear();
    await expect(save).toBeDisabled();
  });

  test("sets a short password using the existing user-password dialog", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/settings?page=users");
    await frigateApp.page
      .getByRole("button", { name: /reset password/i })
      .click();
    const dialog = frigateApp.page.getByRole("dialog");
    const save = dialog.getByRole("button", { name: "Save", exact: true });
    await expect(save).toBeDisabled();
    await dialog.getByLabel("New Password", { exact: true }).fill("x");
    await dialog.getByLabel("Confirm Password", { exact: true }).fill("x");
    await expect(
      dialog.getByText("A non-empty password", { exact: true }),
    ).toBeVisible();
    await expect(save).toBeEnabled();
    const request = frigateApp.page.waitForRequest(
      "**/api/users/admin/password",
    );
    await save.click();
    expect((await request).postDataJSON()).toMatchObject({ password: "x" });
  });
});
