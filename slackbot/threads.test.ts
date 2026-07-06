import assert from "node:assert/strict";
import { test } from "node:test";

import { stableThreadId } from "./threads.js";

test("strips the per-turn uuid from a channel-thread id", () => {
  assert.equal(
    stableThreadId("slack-C0B49MEJ1HQ-1751587200.123456-1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"),
    "slack-C0B49MEJ1HQ-1751587200.123456",
  );
});

test("strips the uuid from a DM id (dm scope)", () => {
  assert.equal(
    stableThreadId("slack-D024BE91L-dm-1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"),
    "slack-D024BE91L-dm",
  );
});

test("same conversation, different turns -> same stable id", () => {
  const a = stableThreadId("slack-C1-100.5-11111111-2222-4333-8444-555555555555");
  const b = stableThreadId("slack-C1-100.5-99999999-8888-4777-a666-555555555544");
  assert.equal(a, b);
});

test("an id without a uuid suffix passes through untouched", () => {
  assert.equal(stableThreadId("slack-C1-100.5"), "slack-C1-100.5");
  assert.equal(stableThreadId("some-other-id"), "some-other-id");
});
