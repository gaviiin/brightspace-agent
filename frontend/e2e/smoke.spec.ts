import { expect, test } from "@playwright/test";

// One smoke test against a real (seeded, offline) backend -- see
// `make e2e-ui` / scripts/e2e.py --seed-only --keep-running. Course list ->
// workspace -> graph canvas -> expand a topic -> select a material ->
// DetailPanel shows it. Not a re-test of unit-level behavior (GraphView.test,
// transform.test, etc. already cover that); this only proves the real app,
// wired to a real backend, actually renders and responds to clicks.

test("course list -> workspace -> expand topic -> select material shows its title", async ({ page }) => {
  await page.goto("/");

  const courseCard = page.getByRole("link", { name: /Intro to CS/ });
  await expect(courseCard).toBeVisible();
  await courseCard.click();

  await expect(page).toHaveURL(/\/courses\/\d+/);

  const topicNodes = page.locator(".react-flow__node-topic");
  const materialNodes = page.locator(".react-flow__node-material");

  // >= 3 topic nodes (the 3rd becoming visible is proof there are at least
  // that many -- Playwright's auto-retrying expect() is the natural way to
  // assert a lower bound without a racy manual .count() poll).
  await expect(topicNodes.nth(2)).toBeVisible();

  // No topic is expanded yet, so no material nodes are rendered at all
  // (see graph/transform.ts: a material node only exists if at least one
  // of its attached topics is expanded).
  await expect(materialNodes).toHaveCount(0);

  await topicNodes.first().click();

  // Expanding a topic makes its materials appear -- node count grows from 0.
  await expect(materialNodes.first()).toBeVisible();
  expect(await materialNodes.count()).toBeGreaterThan(0);

  const firstMaterial = materialNodes.first();
  // React Flow's own wrapper (.react-flow__node-material) has no `title`
  // attribute -- MaterialNode.tsx puts it on the div it renders inside
  // that wrapper.
  const materialTitle = await firstMaterial.locator("[title]").first().getAttribute("title");
  expect(materialTitle).toBeTruthy();

  await firstMaterial.click();

  const detailPanel = page.locator("aside").last();
  await expect(detailPanel.getByRole("heading", { level: 2 })).toHaveText(materialTitle ?? "");
});
